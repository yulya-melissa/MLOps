"""Единственное объявление очередей конвейера.

Источник, воркер и API обязаны объявлять очереди с ОДИНАКОВЫМИ аргументами,
иначе RabbitMQ вернёт PRECONDITION_FAILED. Поэтому описание живёт в одном месте.
"""

from functools import lru_cache

from faststream.rabbit import QueueType, RabbitQueue

from src.config import get_settings


@lru_cache
def get_tasks_queue() -> RabbitQueue:
    """Ограниченная quorum-очередь задач.

    - `x-max-length` — предел числа готовых к выдаче сообщений;
    - `x-overflow=reject-publish` — при переполнении брокер отвечает nack на
      publisher confirm: это и есть сигнал перегрузки для источника и API;
    - `x-delivery-limit` — страховка брокера: после стольких возвратов сообщение
      уходит в DLQ, даже если воркер сам не успел его отклонить;
    - `x-dead-letter-*` — мёртвые сообщения попадают через default exchange
      сразу в DLQ, без ручной публикации (окна потери нет).
    """
    settings = get_settings()
    return RabbitQueue(
        settings.tasks_queue,
        durable=True,
        queue_type=QueueType.QUORUM,
        arguments={
            "x-max-length": settings.queue_max_length,
            "x-overflow": "reject-publish",
            "x-delivery-limit": settings.worker_max_attempts,
            "x-dead-letter-exchange": "",
            "x-dead-letter-routing-key": settings.tasks_dlq_queue,
        },
    )


@lru_cache
def get_dlq_queue() -> RabbitQueue:
    """Очередь недоставленных сообщений: без лимита, чтобы она сама не отклоняла публикации."""
    return RabbitQueue(
        get_settings().tasks_dlq_queue,
        durable=True,
        queue_type=QueueType.QUORUM,
    )
