"""
Модульные тесты для API задач (граница системы, 503, Retry-After, фейковый репозиторий и паблишер)
"""

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.api.tasks import router as tasks_router
from src.config import get_settings
from src.pipeline.publisher import QueueFullError


class InMemoryTaskRepoForTest:
    """Фейковый репозиторий для изоляции тестов API от PostgreSQL."""

    def __init__(self):
        self.tasks = {}

    async def create_task(self, task_id: str, image_path: str, source: str = "api") -> bool:
        if task_id in self.tasks:
            return False
        self.tasks[task_id] = {"task_id": task_id, "image_path": image_path, "status": "PENDING"}
        return True

    async def get_task(self, task_id: str):
        return self.tasks.get(task_id)

    async def list_tasks(self, limit=50, status=None):
        res = list(self.tasks.values())
        if status:
            res = [t for t in res if t["status"] == status]
        return res[:limit]


@pytest.fixture
async def api_client_with_mocks():
    """Поднимает изолированное FastAPI приложение с моками паблишера и репозитория."""
    app = FastAPI()
    app.include_tasks_router = app.include_router(tasks_router)

    settings = get_settings()
    mock_publisher = AsyncMock()
    mock_repo = InMemoryTaskRepoForTest()

    app.state.settings = settings
    app.state.publisher = mock_publisher
    app.state.task_repo = mock_repo

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as ac:
        yield ac, mock_publisher, mock_repo


@pytest.mark.asyncio
async def test_create_task_success(api_client_with_mocks):
    client, publisher, repo = api_client_with_mocks

    # Отправляем валидный файл изображения
    files = {"file": ("test.jpg", b"fake-image-bytes", "image/jpeg")}
    response = await client.post("/api/v1/tasks", files=files)

    assert response.status_code == 202
    data = response.json()
    assert "task_id" in data
    assert data["status"] == "PENDING"
    assert "poll_url" in data

    # Проверяем, что задача успешно добавлена в репозиторий
    task_id = data["task_id"]
    assert task_id in repo.tasks


@pytest.mark.asyncio
async def test_create_task_overload_503(api_client_with_mocks):
    client, publisher, repo = api_client_with_mocks

    # Имитируем переполнение очереди брокера
    publisher.publish.side_effect = QueueFullError("Queue is full")

    files = {"file": ("test.jpg", b"fake-image-bytes", "image/jpeg")}
    response = await client.post("/api/v1/tasks", files=files)

    assert response.status_code == 503
    assert "Retry-After" in response.headers
    data = response.json()
    assert "detail" in data

    # Проверяем критерий 4.2: при 503 задача НЕ записана в репозиторий (нет PENDING)
    assert len(repo.tasks) == 0


@pytest.mark.asyncio
async def test_get_task_not_found(api_client_with_mocks):
    client, _, _ = api_client_with_mocks
    response = await client.get("/api/v1/tasks/non-existent-id")
    assert response.status_code == 404
