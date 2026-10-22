"""Тесты надёжности воркера: успех, ретраи, DLQ, идемпотентность.

Тестируется настоящий код воркера (`src.worker.app._process`), а не его копия.
Брокер и модель замоканы, репозиторий — `InMemoryTaskRepository`.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.pipeline.messages import TaskMessage
from src.services.tasks import InMemoryTaskRepository, TaskStatus
from src.worker import app as worker

MSG = TaskMessage(task_id="task-123", image_path="img/1.jpg")


def make_message(headers: dict | None = None) -> AsyncMock:
    message = AsyncMock()
    message.headers = headers
    return message


@pytest.fixture
def repo(monkeypatch) -> InMemoryTaskRepository:
    repo = InMemoryTaskRepository()
    monkeypatch.setattr(worker, "_repo", repo)
    # Без реальных пауз между ретраями
    monkeypatch.setattr(worker.settings, "worker_retry_delay_s", 0.0)
    return repo


@pytest.fixture
def predict(monkeypatch) -> AsyncMock:
    mock = AsyncMock(return_value=[{"class_name": "fire"}])
    monkeypatch.setattr(worker.engine, "predict", mock)
    return mock


async def test_success_marks_done_and_acks(repo, predict):
    message = make_message()

    await worker._process(MSG, message, MagicMock())

    task = await repo.get_task(MSG.task_id)
    assert task is not None
    assert task.status is TaskStatus.DONE
    assert task.result == [{"class_name": "fire"}]
    message.ack.assert_awaited_once()
    message.reject.assert_not_awaited()
    assert not worker._inflight  # задача снята с учёта in-flight


async def test_redelivery_of_done_task_is_idempotent(repo, predict):
    """Повторная доставка уже DONE задачи: инференса нет, строка одна, сообщение ack'нуто."""
    await worker._process(MSG, make_message(), MagicMock())
    assert predict.await_count == 1

    second = make_message()
    await worker._process(MSG, second, MagicMock())

    assert predict.await_count == 1  # лишней работы нет
    second.ack.assert_awaited_once()
    assert len(await repo.list_tasks()) == 1  # одна строка на task_id
    task = await repo.get_task(MSG.task_id)
    assert task is not None
    assert task.status is TaskStatus.DONE
    assert task.attempts == 1  # повторная доставка не наращивает попытки


async def test_first_failure_requeues_with_reject(repo, predict):
    predict.side_effect = RuntimeError("boom")
    message = make_message(headers={})  # первая доставка: заголовка x-delivery-count нет

    await worker._process(MSG, message, MagicMock())

    message.reject.assert_awaited_once_with(requeue=True)
    message.ack.assert_not_awaited()
    task = await repo.get_task(MSG.task_id)
    assert task is not None
    assert task.status is TaskStatus.PROCESSING  # ещё не FAILED: будут повторы


async def test_last_attempt_goes_to_dlq(repo, predict):
    predict.side_effect = RuntimeError("boom")
    last = worker.settings.worker_max_attempts - 1  # x-delivery-count на последней попытке
    message = make_message(headers={"x-delivery-count": last})

    await worker._process(MSG, message, MagicMock())

    message.reject.assert_awaited_once_with(requeue=False)  # брокер уведёт в DLQ
    message.ack.assert_not_awaited()
    task = await repo.get_task(MSG.task_id)
    assert task is not None
    assert task.status is TaskStatus.FAILED
    assert task.error == "boom"


async def test_result_written_before_ack(repo, predict):
    """5.1: к моменту ack результат уже в БД."""
    seen: dict[str, TaskStatus] = {}
    message = make_message()

    async def check_status_on_ack() -> None:
        task = await repo.get_task(MSG.task_id)
        assert task is not None
        seen["status"] = task.status

    message.ack.side_effect = check_status_on_ack

    await worker._process(MSG, message, MagicMock())

    assert seen["status"] is TaskStatus.DONE
