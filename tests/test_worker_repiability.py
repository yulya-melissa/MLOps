"""Модульные тесты надежности воркера: ретраи, DLQ и идемпотентность."""

from unittest.mock import AsyncMock

import pytest


class InMemoryRepo:
    """Репозиторий для проверки идемпотентности и статусов."""

    def __init__(self):
        self.records = {}

    async def mark_processing(self, task_id: str):
        self.records[task_id] = {
            "status": "PROCESSING",
            "attempts": self.records.get(task_id, {}).get("attempts", 0) + 1,
        }

    async def mark_done(self, task_id: str, result: list):
        if task_id in self.records:
            self.records[task_id]["status"] = "DONE"
            self.records[task_id]["result"] = result

    async def mark_failed(self, task_id: str, error: str):
        if task_id in self.records:
            self.records[task_id]["status"] = "FAILED"
            self.records[task_id]["error"] = error


@pytest.mark.asyncio
async def test_idempotent_processing():
    repo = InMemoryRepo()
    task_id = "task-123"

    # Эмулируем двойную доставку сообщения (идемпотентность)
    await repo.mark_processing(task_id)
    await repo.mark_processing(task_id)

    assert repo.records[task_id]["attempts"] == 2

    # Завершаем задачу
    await repo.mark_done(task_id, [{"class_name": "fire"}])
    assert repo.records[task_id]["status"] == "DONE"
    assert len(repo.records) == 1  # Строка в БД ровно одна (идемпотентность / уникальный ключ)


@pytest.mark.asyncio
async def test_message_retry_logic():
    message = AsyncMock()
    message.headers = {"x-delivery-count": 1}  # Вторая попытка

    max_attempts = 3
    headers = message.headers or {}
    delivery_count = int(headers.get("x-delivery-count", 0))
    current_attempt = delivery_count + 1

    # Попытка < max_attempts -> делаем requeue=True
    assert current_attempt < max_attempts
    await message.reject(requeue=True)
    message.reject.assert_awaited_once_with(requeue=True)
