"""Интеграционный тест: переполнение ограниченной очереди на реальном RabbitMQ.

RabbitMQ поднимается через Testcontainers, поэтому нужен запущенный Docker.
Если Docker недоступен, тест пропускается.
Публикация идёт через настоящий `TaskPublisher` (тот же код, что у API), очередь
объявляется по настоящей топологии (`x-max-length` + `x-overflow=reject-publish`).
"""

import pytest
from testcontainers.community.rabbitmq import RabbitMqContainer

from src.config import get_settings
from src.pipeline.client import TaskPublisher
from src.pipeline.messages import TaskMessage
from src.pipeline.publisher import QueueFullError
from src.pipeline.topology import get_dlq_queue, get_tasks_queue

LIMIT = 5  # маленький лимит очереди, чтобы переполнение наступало быстро


@pytest.fixture(scope="module")
def rabbit_url():
    try:
        # Конструктор тоже обращается к Docker, поэтому он внутри try.
        container = RabbitMqContainer("rabbitmq:4-management-alpine")  # ветка как в compose
        container.start()
    except Exception as exc:  # например, не запущен Docker
        pytest.skip(f"Docker недоступен: {exc}")
    try:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(container.port)
        yield f"amqp://{container.username}:{container.password}@{host}:{port}/"
    finally:
        container.stop()


@pytest.fixture
def small_queue(monkeypatch):
    """Лимит очереди = LIMIT. Топология кешируется, поэтому сбрасываем кеш до и после."""
    settings = get_settings()
    monkeypatch.setattr(settings, "queue_max_length", LIMIT)
    get_tasks_queue.cache_clear()
    get_dlq_queue.cache_clear()
    yield settings
    get_tasks_queue.cache_clear()
    get_dlq_queue.cache_clear()


async def test_queue_overflow_rejects_publish(rabbit_url, small_queue):
    """Без потребителя очередь заполняется, и брокер отклоняет лишние публикации."""
    publisher = TaskPublisher(small_queue.model_copy(update={"rabbit_url": rabbit_url}))
    total = LIMIT * 3
    accepted = rejected = 0

    try:
        for i in range(total):
            try:
                await publisher.publish(TaskMessage(task_id=f"task-{i}", image_path="img.jpg"))
                accepted += 1
            except QueueFullError:
                rejected += 1
    finally:
        await publisher.close()

    assert rejected > 0, "брокер должен был отклонить публикации (nack) при переполнении"
    # Quorum-очередь может превысить лимит ровно на одно сообщение (документированный overshoot).
    assert LIMIT <= accepted <= LIMIT + 1
    # Каждая публикация либо принята, либо явно отклонена: тихих потерь нет.
    assert accepted + rejected == total
