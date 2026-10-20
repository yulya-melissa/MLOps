"""Единственное объявление очереди задач.

Источник, воркер (и позже API) обязаны объявлять очередь с ОДИНАКОВЫМИ аргументами,
иначе RabbitMQ вернёт PRECONDITION_FAILED. Поэтому описание живёт в одном месте.
"""

from functools import lru_cache

from faststream.rabbit import QueueType, RabbitQueue

from src.config import get_settings


@lru_cache
def get_tasks_queue() -> RabbitQueue:
    """Ограниченная quorum-очередь.

    - `x-max-length` — предел числа готовых к выдаче сообщений;
    - `x-overflow=reject-publish` — при переполнении брокер отвечает nack на
      publisher confirm: это и есть сигнал перегрузки для источника.
    """
    settings = get_settings()
    return RabbitQueue(
        settings.tasks_queue,
        durable=True,
        queue_type=QueueType.QUORUM,
        arguments={
            "x-max-length": settings.queue_max_length,
            "x-overflow": "reject-publish",
        },
    )
