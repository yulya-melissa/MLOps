"""FastStream-воркер: берёт задачи из очереди и выполняет inference.

Запуск: `faststream run src.worker.app:app`
"""

import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from faststream import FastStream, Logger
from faststream.rabbit import Channel, RabbitBroker

from src.config import get_settings
from src.pipeline.messages import TaskMessage
from src.pipeline.topology import get_tasks_queue
from src.worker.inference import InferenceEngine

log = logging.getLogger(__name__)
settings = get_settings()

broker = RabbitBroker(settings.rabbit_url)
engine = InferenceEngine(
    tracking_uri=settings.mlflow_tracking_uri,
    model_uri=settings.model_uri,
    concurrency=settings.inference_concurrency,
    delay_s=settings.inference_delay_s,
)


@asynccontextmanager
async def lifespan() -> AsyncIterator[None]:
    """Загрузить модель до начала потребления сообщений, закрыть пул при остановке."""
    if settings.prefetch_count < settings.inference_concurrency:
        log.warning(
            "prefetch_count=%d меньше конкурентности inference=%d: воркер будет простаивать",
            settings.prefetch_count,
            settings.inference_concurrency,
        )
    await engine.load()
    try:
        yield
    finally:
        engine.shutdown()


app = FastStream(broker, lifespan=lifespan)


# prefetch_count ограничивает число неподтверждённых сообщений у этого воркера:
# остальное ждёт в очереди RabbitMQ (и упирается в её лимит), а не копится в памяти.
@broker.subscriber(
    get_tasks_queue(),
    channel=Channel(prefetch_count=settings.prefetch_count),
)
async def handle_task(msg: TaskMessage, logger: Logger) -> None:
    """Обработать одну задачу: inference модели над картинкой."""
    started = time.perf_counter()
    detections = await engine.predict(str(Path(settings.data_dir) / msg.image_path))
    elapsed_ms = (time.perf_counter() - started) * 1000
    logger.info("task=%s objects=%d took=%.0f ms", msg.task_id, len(detections), elapsed_ms)
