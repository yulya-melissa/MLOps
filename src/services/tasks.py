"""Репозиторий задач: единственное место, где приложение работает с таблицей tasks."""

import json
from dataclasses import dataclass, fields, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from asyncpg import Pool, Record

# Результат inference — список детекций (pandas.DataFrame.to_dict("records")).
Detections = list[dict[str, object]]


class TaskStatus(StrEnum):
    """Жизненный цикл задачи в БД."""

    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    DONE = "DONE"
    FAILED = "FAILED"


@dataclass(frozen=True)
class Task:
    """Строка таблицы tasks."""

    task_id: str
    status: TaskStatus
    source: str
    image_path: str
    result: Detections | None
    error: str | None
    attempts: int
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None


class TaskRepository(Protocol):
    """Контракт хранилища задач.

    Идемпотентность обеспечивается на уровне хранилища: `task_id` — PRIMARY KEY,
    а все переходы статуса — `INSERT ... ON CONFLICT DO UPDATE`. Схемы
    «проверить, потом вставить» нет, поэтому гонка между двумя доставками
    одного сообщения невозможна (5.3).
    """

    async def create_task(self, task_id: str, image_path: str, source: str = "api") -> bool:
        """Создать задачу в статусе PENDING. False — task_id уже существует."""
        ...

    async def mark_processing(self, task_id: str, image_path: str) -> bool:
        """Начать попытку обработки (attempts += 1).

        False — задача уже в статусе DONE: это повторная доставка, обрабатывать нечего.
        """
        ...

    async def mark_done(self, task_id: str, result: Detections) -> None:
        """Зафиксировать успешный результат."""
        ...

    async def mark_failed(self, task_id: str, error: str) -> None:
        """Зафиксировать окончательную ошибку обработки."""
        ...

    async def get_task(self, task_id: str) -> Task | None:
        """Вернуть задачу по id или None."""
        ...

    async def list_tasks(self, limit: int = 50, status: TaskStatus | None = None) -> list[Task]:
        """Вернуть последние задачи (новые первыми), опционально фильтруя по статусу."""
        ...


# --- Postgres ----------------------------------------------------------------

_COLUMNS = ", ".join(f.name for f in fields(Task))


def _affected(command_tag: str) -> int:
    """asyncpg возвращает тег команды вида 'INSERT 0 1' или 'UPDATE 1'."""
    return int(command_tag.rsplit(" ", 1)[-1])


def _to_task(row: Record) -> Task:
    data = dict(row)
    data["status"] = TaskStatus(data["status"])
    if data["result"] is not None:  # asyncpg отдаёт jsonb строкой
        data["result"] = json.loads(data["result"])
    return Task(**data)


class PgTaskRepository:
    """Репозиторий поверх Postgres."""

    def __init__(self, pool: Pool) -> None:
        self._pool = pool

    async def create_task(self, task_id: str, image_path: str, source: str = "api") -> bool:
        """Создать задачу в статусе PENDING.

        `ON CONFLICT DO NOTHING` — повторная публикация того же task_id не
        создаёт вторую строку (идемпотентность на уровне уникального ключа).
        """
        tag = await self._pool.execute(
            """
            INSERT INTO tasks (task_id, image_path, source)
            VALUES ($1, $2, $3)
            ON CONFLICT (task_id) DO NOTHING
            """,
            task_id,
            image_path,
            source,
        )
        return _affected(tag) == 1

    async def mark_processing(self, task_id: str, image_path: str) -> bool:
        """Начать попытку обработки.

        Upsert: воркер может получить сообщение раньше, чем API успеет создать
        строку. WHERE не даёт откатить уже завершённую задачу обратно в PROCESSING.
        """
        tag = await self._pool.execute(
            """
            INSERT INTO tasks (task_id, image_path, source, status, attempts)
            VALUES ($1, $2, 'worker', 'PROCESSING', 1)
            ON CONFLICT (task_id) DO UPDATE
            SET status = 'PROCESSING',
                attempts = tasks.attempts + 1,
                updated_at = now()
            WHERE tasks.status <> 'DONE'
            """,
            task_id,
            image_path,
        )
        return _affected(tag) == 1

    async def mark_done(self, task_id: str, result: Detections) -> None:
        """Зафиксировать успешный результат (запись идёт до ack сообщения)."""
        await self._pool.execute(
            """
            UPDATE tasks
            SET status = 'DONE', result = $2::jsonb, error = NULL,
                updated_at = now(), finished_at = now()
            WHERE task_id = $1
            """,
            task_id,
            json.dumps(result, default=str),
        )

    async def mark_failed(self, task_id: str, error: str) -> None:
        """Зафиксировать окончательную ошибку обработки."""
        await self._pool.execute(
            """
            UPDATE tasks
            SET status = 'FAILED', error = $2,
                updated_at = now(), finished_at = now()
            WHERE task_id = $1
            """,
            task_id,
            error,
        )

    async def get_task(self, task_id: str) -> Task | None:
        row = await self._pool.fetchrow(f"SELECT {_COLUMNS} FROM tasks WHERE task_id = $1", task_id)
        return _to_task(row) if row else None

    async def list_tasks(self, limit: int = 50, status: TaskStatus | None = None) -> list[Task]:
        rows = await self._pool.fetch(
            f"""
            SELECT {_COLUMNS} FROM tasks
            WHERE ($2::text IS NULL OR status = $2)
            ORDER BY created_at DESC
            LIMIT $1
            """,
            limit,
            status.value if status else None,
        )
        return [_to_task(row) for row in rows]


# --- In-memory (юнит-тесты без Postgres) --------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


class InMemoryTaskRepository:
    """Повторяет семантику PgTaskRepository. Безопасна в одном event loop:
    внутри методов нет await, поэтому они атомарны."""

    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}

    def _new(
        self,
        task_id: str,
        image_path: str,
        source: str,
        status: TaskStatus,
        attempts: int,
    ) -> Task:
        now = _now()
        task = Task(task_id, status, source, image_path, None, None, attempts, now, now, None)
        self._tasks[task_id] = task
        return task

    def _replace(self, task_id: str, **changes: object) -> None:
        """Обновить задачу. Как UPDATE в SQL: отсутствующей строки — no-op."""
        task = self._tasks.get(task_id)
        if task is None:
            return
        self._tasks[task_id] = replace(task, updated_at=_now(), **changes)

    async def create_task(self, task_id: str, image_path: str, source: str = "api") -> bool:
        if task_id in self._tasks:
            return False
        self._new(task_id, image_path, source, TaskStatus.PENDING, attempts=0)
        return True

    async def mark_processing(self, task_id: str, image_path: str) -> bool:
        task = self._tasks.get(task_id)
        if task is None:
            self._new(task_id, image_path, "worker", TaskStatus.PROCESSING, attempts=1)
            return True
        if task.status is TaskStatus.DONE:
            return False
        self._replace(task_id, status=TaskStatus.PROCESSING, attempts=task.attempts + 1)
        return True

    async def mark_done(self, task_id: str, result: Detections) -> None:
        self._replace(
            task_id,
            status=TaskStatus.DONE,
            result=result,
            error=None,
            finished_at=_now(),
        )

    async def mark_failed(self, task_id: str, error: str) -> None:
        self._replace(task_id, status=TaskStatus.FAILED, error=error, finished_at=_now())

    async def get_task(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id)

    async def list_tasks(self, limit: int = 50, status: TaskStatus | None = None) -> list[Task]:
        tasks = [t for t in self._tasks.values() if status is None or t.status == status]
        tasks.sort(key=lambda t: t.created_at, reverse=True)
        return tasks[:limit]
