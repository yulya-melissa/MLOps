"""API задач: приём изображения, статус и список.

Это граница системы: внешний клиент вне контура управления, поэтому при перегрузке
запрос отклоняется сразу (503 + Retry-After), а не ждёт места в очереди.
"""

import asyncio
import logging
import uuid
from pathlib import Path, PurePath
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from src.config import Settings
from src.pipeline.client import TaskPublisher
from src.pipeline.messages import TaskMessage
from src.pipeline.publisher import BrokerUnavailableError, QueueFullError
from src.services.tasks import Task, TaskRepository, TaskStatus

router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])
log = logging.getLogger(__name__)

ALLOWED_CONTENT_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}

_OVERLOADED = {
    503: {
        "description": (
            "Конвейер перегружен или RabbitMQ недоступен. Задача не принята: "
            "повторите запрос через число секунд из заголовка Retry-After."
        )
    }
}


class TaskAccepted(BaseModel):
    task_id: str
    status: TaskStatus = TaskStatus.PENDING
    poll_url: str


class ByPathRequest(BaseModel):
    image_path: str


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _repo(request: Request) -> TaskRepository:
    return request.app.state.task_repo


def _publisher(request: Request) -> TaskPublisher:
    return request.app.state.publisher


def _save_upload(upload_dir: str, name: str, content: bytes) -> Path:
    path = Path(upload_dir).resolve() / name
    path.write_bytes(content)
    return path


SettingsDep = Annotated[Settings, Depends(_settings)]
RepoDep = Annotated[TaskRepository, Depends(_repo)]
PublisherDep = Annotated[TaskPublisher, Depends(_publisher)]


async def _submit(
    task_id: str,
    image_path: str,
    *,
    publisher: TaskPublisher,
    repo: TaskRepository,
    settings: Settings,
    uploaded_file: Path | None = None,
) -> TaskAccepted | JSONResponse:
    """Опубликовать задачу и только после подтверждения брокера записать её в БД.

    Порядок «publish → insert» гарантирует, что после 503 в БД нет ни одной строки
    (критерий 4.2): нечего откатывать.
    """
    try:
        await publisher.publish(TaskMessage(task_id=task_id, image_path=image_path))
    except (QueueFullError, BrokerUnavailableError) as exc:
        if uploaded_file is not None:
            await asyncio.to_thread(uploaded_file.unlink, missing_ok=True)
        log.warning("Задача %s отклонена (503): %s", task_id, exc)
        retry_after = settings.api_retry_after_s
        return JSONResponse(
            status_code=503,
            headers={"Retry-After": str(retry_after)},
            content={
                "detail": f"Сервис перегружен. Повторите запрос через {retry_after} с.",
                "retry_after": retry_after,
                "task_id": task_id,
            },
        )

    try:
        await repo.create_task(task_id, image_path)
    except Exception:
        # Задача уже в очереди, а воркер создаёт строку сам (upsert),
        # поэтому клиенту честно отвечаем 202.
        log.exception("Не удалось создать запись о задаче %s", task_id)

    return TaskAccepted(task_id=task_id, poll_url=f"{router.prefix}/{task_id}")


@router.post(
    "",
    response_model=TaskAccepted,
    status_code=202,
    responses=_OVERLOADED,
    summary="Отправить изображение на обработку",
)
async def create_task(
    file: Annotated[UploadFile, File(description="Изображение JPEG, PNG или WEBP")],
    publisher: PublisherDep,
    repo: RepoDep,
    settings: SettingsDep,
) -> TaskAccepted | JSONResponse:
    """Ставит изображение в очередь и сразу возвращает `task_id`.

    Результат обработки забирайте по `poll_url`.
    """
    suffix = ALLOWED_CONTENT_TYPES.get(file.content_type or "")
    if suffix is None:
        raise HTTPException(status_code=415, detail="Поддерживаются только JPEG, PNG и WEBP.")

    try:
        content = await file.read()
    finally:
        await file.close()
    if not content:
        raise HTTPException(status_code=400, detail="Файл пустой.")

    task_id = uuid.uuid4().hex
    path = await asyncio.to_thread(_save_upload, settings.upload_dir, f"{task_id}{suffix}", content)

    return await _submit(
        task_id,
        str(path),
        publisher=publisher,
        repo=repo,
        settings=settings,
        uploaded_file=path,
    )


@router.post(
    "/by-path",
    response_model=TaskAccepted,
    status_code=202,
    responses=_OVERLOADED,
    summary="Отправить на обработку файл из каталога данных (для нагрузочных тестов)",
)
async def create_task_by_path(
    body: ByPathRequest,
    publisher: PublisherDep,
    repo: RepoDep,
    settings: SettingsDep,
) -> TaskAccepted | JSONResponse:
    """Лёгкий вариант без загрузки файла: `image_path` — путь внутри каталога данных."""
    path = PurePath(body.image_path)
    if path.anchor or ".." in path.parts:
        raise HTTPException(
            status_code=422,
            detail="image_path должен быть относительным путём внутри каталога данных.",
        )
    return await _submit(
        uuid.uuid4().hex,
        body.image_path,
        publisher=publisher,
        repo=repo,
        settings=settings,
    )


@router.get("/{task_id}", response_model=Task, summary="Статус и результат задачи")
async def get_task(task_id: str, repo: RepoDep) -> Task:
    task = await repo.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Задача не найдена.")
    return task


@router.get("", response_model=list[Task], summary="Последние задачи")
async def list_tasks(
    repo: RepoDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    status: TaskStatus | None = None,
) -> list[Task]:
    return await repo.list_tasks(limit=limit, status=status.value if status else None)
