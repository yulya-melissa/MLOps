"""
Модульные тесты API задач (граница системы: 503, Retry-After, откат файла, нет PENDING).
Паблишер и репозиторий — фейки, RabbitMQ и Postgres не нужны.
"""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.api.tasks import router as tasks_router
from src.config import get_settings
from src.pipeline.publisher import BrokerUnavailableError, QueueFullError


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
async def api_client_with_mocks(tmp_path: Path):
    """Изолированное приложение с моками паблишера и репозитория.

    Загрузки пишутся во временный каталог, а не в ./uploads проекта.
    """
    app = FastAPI()
    app.include_router(tasks_router)

    mock_publisher = AsyncMock()
    mock_repo = InMemoryTaskRepoForTest()

    app.state.settings = get_settings().model_copy(update={"upload_dir": str(tmp_path)})
    app.state.publisher = mock_publisher
    app.state.task_repo = mock_repo

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as ac:
        yield ac, mock_publisher, mock_repo, tmp_path


FILES = {"file": ("test.jpg", b"fake-image-bytes", "image/jpeg")}


async def test_create_task_success(api_client_with_mocks):
    client, publisher, repo, upload_dir = api_client_with_mocks

    response = await client.post("/api/v1/tasks", files=FILES)

    assert response.status_code == 202
    data = response.json()
    assert "task_id" in data
    assert data["status"] == "PENDING"
    assert "poll_url" in data

    assert data["task_id"] in repo.tasks
    publisher.publish.assert_awaited_once()
    assert len(list(upload_dir.iterdir())) == 1  # файл сохранён для воркера


async def test_create_task_overload_503(api_client_with_mocks):
    client, publisher, repo, upload_dir = api_client_with_mocks
    publisher.publish.side_effect = QueueFullError("Queue is full")

    response = await client.post("/api/v1/tasks", files=FILES)

    assert response.status_code == 503
    assert response.headers["Retry-After"] == str(get_settings().api_retry_after_s)
    assert "detail" in response.json()

    # 4.2: при 503 в репозитории нет ни одной задачи (нет PENDING)
    assert len(repo.tasks) == 0
    # временный файл удалён
    assert list(upload_dir.iterdir()) == []


async def test_broker_unavailable_or_timeout_503(api_client_with_mocks):
    """Недоступный брокер или таймаут публикации тоже дают 503 + Retry-After."""
    client, publisher, repo, upload_dir = api_client_with_mocks
    publisher.publish.side_effect = BrokerUnavailableError("RabbitMQ не ответил")

    response = await client.post("/api/v1/tasks", files=FILES)

    assert response.status_code == 503
    assert "Retry-After" in response.headers
    assert len(repo.tasks) == 0
    assert list(upload_dir.iterdir()) == []


async def test_by_path_overload_503(api_client_with_mocks):
    client, publisher, repo, _ = api_client_with_mocks
    publisher.publish.side_effect = QueueFullError("Queue is full")

    response = await client.post("/api/v1/tasks/by-path", json={"image_path": "images/1.jpg"})

    assert response.status_code == 503
    assert "Retry-After" in response.headers
    assert len(repo.tasks) == 0


async def test_by_path_rejects_path_traversal(api_client_with_mocks):
    client, publisher, _, _ = api_client_with_mocks

    response = await client.post("/api/v1/tasks/by-path", json={"image_path": "../etc/passwd"})

    assert response.status_code == 422
    publisher.publish.assert_not_awaited()


async def test_unsupported_content_type_415(api_client_with_mocks):
    client, publisher, _, _ = api_client_with_mocks

    response = await client.post("/api/v1/tasks", files={"file": ("a.txt", b"hello", "text/plain")})

    assert response.status_code == 415
    publisher.publish.assert_not_awaited()


async def test_get_task_not_found(api_client_with_mocks):
    client, _, _, _ = api_client_with_mocks
    response = await client.get("/api/v1/tasks/non-existent-id")
    assert response.status_code == 404
