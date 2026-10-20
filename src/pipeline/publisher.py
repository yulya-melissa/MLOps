"""Публикация задач: одна попытка, сигнал перегрузки — исключение."""

from aio_pika.exceptions import DeliveryError
from faststream.rabbit import RabbitBroker

from src.pipeline.messages import TaskMessage
from src.pipeline.topology import get_tasks_queue


class QueueFullError(Exception):
    """Брокер отклонил публикацию (nack): очередь заполнена."""


async def publish_task(broker: RabbitBroker, msg: TaskMessage) -> None:
    """Опубликовать задачу и дождаться publisher confirm.

    Исключение `QueueFullError` — единственный сигнал перегрузки. Что с ним делать,
    решает вызывающий: источник ждёт и повторяет, API вернёт 503.
    """
    try:
        await broker.publish(msg, queue=get_tasks_queue(), persist=True)
    except DeliveryError as exc:
        raise QueueFullError("очередь задач заполнена") from exc
