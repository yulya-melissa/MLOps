"""Клиент публикации для API: ленивое подключение к RabbitMQ."""

import asyncio
import logging
from contextlib import suppress

from aio_pika.exceptions import AMQPError
from faststream.rabbit import RabbitBroker

from src.config import Settings
from src.pipeline.messages import TaskMessage
from src.pipeline.publisher import BrokerUnavailableError, publish_task
from src.pipeline.topology import get_dlq_queue, get_tasks_queue

log = logging.getLogger(__name__)


class TaskPublisher:
    """Публикует задачи из API.

    Подключение ленивое: API стартует, даже если RabbitMQ лежит, а первый запрос
    после его появления подключается сам. Подключение и публикация ограничены
    одним дедлайном `api_publish_timeout_s`: запрос никогда не «висит»
    (иначе раздел 4 обнуляется).
    """

    def __init__(self, settings: Settings) -> None:
        self._url = settings.rabbit_url
        self._timeout_s = settings.api_publish_timeout_s
        self._broker: RabbitBroker | None = None
        self._connect_lock = asyncio.Lock()

    async def publish(self, msg: TaskMessage) -> None:
        """Опубликовать задачу.

        Raises:
            QueueFullError: очередь заполнена, брокер ответил nack.
            BrokerUnavailable: брокер недоступен или не ответил за дедлайн.
        """
        try:
            async with asyncio.timeout(self._timeout_s):
                broker = await self._get_broker()
                await publish_task(broker, msg)
        except TimeoutError as exc:
            raise BrokerUnavailableError("RabbitMQ не ответил за отведённое время") from exc
        except (AMQPError, OSError) as exc:  # ошибки подключения
            raise BrokerUnavailableError("RabbitMQ недоступен") from exc

    async def close(self) -> None:
        if self._broker is not None:
            await self._broker.stop()
            self._broker = None

    async def _get_broker(self) -> RabbitBroker:
        if self._broker is not None:
            return self._broker
        async with self._connect_lock:
            if self._broker is None:
                self._broker = await self._connect()
            return self._broker

    async def _connect(self) -> RabbitBroker:
        broker = RabbitBroker(self._url)
        try:
            await broker.start()
            # DLQ объявляем первой: без неё мёртвые сообщения были бы потеряны.
            await broker.declare_queue(get_dlq_queue())
            await broker.declare_queue(get_tasks_queue())
        except BaseException:
            with suppress(Exception):
                await broker.stop()
            raise
        log.info("API подключился к RabbitMQ")
        return broker
