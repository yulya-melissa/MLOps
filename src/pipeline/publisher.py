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

    `correlation_id` — это стандартное поле AMQP, которое RabbitMQ передаёт
    без модификаций. Воркер извлекает его и кладёт в contextvars, поэтому
    task_id автоматически попадает во все логи обработчика.
    """
    try:
        await broker.publish(
            msg,
            queue=get_tasks_queue(),
            persist=True,
            correlation_id=msg.task_id,
        )
    except DeliveryError as exc:
        raise QueueFullError("очередь задач заполнена") from exc
    except (AMQPError, OSError) as exc:
        raise BrokerUnavailableError("RabbitMQ недоступен") from exc