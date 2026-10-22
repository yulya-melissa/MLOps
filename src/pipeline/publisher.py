"""Публикация задач: одна попытка, сигнал перегрузки — исключение."""

from aio_pika.exceptions import AMQPError, DeliveryError
from faststream.rabbit import RabbitBroker

from src.pipeline.messages import TaskMessage
from src.pipeline.topology import get_tasks_queue


class QueueFullError(Exception):
    """Брокер отклонил публикацию (nack): очередь заполнена."""


class BrokerUnavailableError(Exception):
    """Брокер недоступен или не ответил вовремя: задача не опубликована."""


async def publish_task(broker: RabbitBroker, msg: TaskMessage) -> None:
    """Опубликовать задачу и дождаться publisher confirm.

    `QueueFullError` — сигнал перегрузки, `BrokerUnavailable` — брокер недоступен.
    Что с ними делать, решает вызывающий: источник ждёт и повторяет, API вернёт 503.
    """
    try:
        await broker.publish(msg, queue=get_tasks_queue(), persist=True)
    except DeliveryError as exc:  # подкласс AMQPError, поэтому стоит первым
        raise QueueFullError("очередь задач заполнена") from exc
    except (AMQPError, OSError) as exc:
        raise BrokerUnavailableError("RabbitMQ недоступен") from exc
