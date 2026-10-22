"""FastStream-воркер: обработка задач с ack_policy (MANUAL), повторами (requeue=True) и DLQ."""

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from faststream import AckPolicy, FastStream, Logger
from faststream.rabbit import Channel, RabbitBroker, StreamMessage

from src.config import get_settings
from src.db import close_db, get_pool, init_db
from src.pipeline.messages import TaskMessage
from src.pipeline.topology import get_tasks_queue
from src.services import tasks as tasks_repo
from src.worker.inference import InferenceEngine

log = logging.getLogger(__name__)
settings = get_settings()

broker = RabbitBroker(settings.rabbit_url, graceful_timeout=settings.worker_shutdown_timeout_s)
engine = InferenceEngine(
    tracking_uri=settings.mlflow_tracking_uri,
    model_uri=settings.model_uri,
    concurrency=settings.inference_concurrency,
    delay_s=settings.inference_delay_s,
)

# Трекер активных задач для реализации graceful shutdown
_inflight: set[asyncio.Task[None]] = set()


@asynccontextmanager
async def lifespan() -> AsyncIterator[None]:
    """Инициализация пула БД, загрузка модели и graceful shutdown."""
    if settings.prefetch_count < settings.inference_concurrency:
        log.warning(
            "prefetch_count=%d меньше конкурентности inference=%d: воркер будет простаивать",
            settings.prefetch_count,
            settings.inference_concurrency,
        )

    # Инициализация БД и проверка схемы
    await init_db()
    pool = get_pool()
    await tasks_repo.ensure_schema(pool)

    # Загрузка MLflow-модели один раз при старте воркера (критерий 1.2)
    await engine.load()
    try:
        yield
    finally:
        log.info(
            "Завершение работы воркера: ожидание завершения in-flight задач (%d)...", len(_inflight)
        )
        if _inflight:
            _, pending = await asyncio.wait(_inflight, timeout=settings.worker_shutdown_timeout_s)
            if pending:
                log.warning(
                    "Не все задачи завершились вовремя до таймаута: %d зависло", len(pending)
                )

        engine.shutdown()
        await close_db()
        log.info("Воркер успешно остановлен.")


app = FastStream(broker, lifespan=lifespan)


async def _handle_failure(msg: TaskMessage, message: StreamMessage, exc: Exception) -> None:
    """Обработка ошибок выполнения: повторные попытки через reject(requeue=True) или DLQ."""
    pool = get_pool()
    # В quorum-очередях число попыток доставок сохраняется в заголовке x-delivery-count
    headers = message.headers or {}
    delivery_count = int(headers.get("x-delivery-count", 0))
    current_attempt = delivery_count + 1

    log.error(
        "Ошибка при обработке task=%s (попытка %d/%d): %s",
        msg.task_id,
        current_attempt,
        settings.worker_max_attempts,
        exc,
    )

    if current_attempt < settings.worker_max_attempts:
        await asyncio.sleep(settings.worker_retry_delay_s)
        # reject(ре requeue=True) увеличивает x-delivery-count и возвращает в очередь
        await message.reject(requeue=True)
    else:
        log.error(
            "Исчерпаны все попытки (%d) для task=%s. Перенаправление в DLQ.",
            settings.worker_max_attempts,
            msg.task_id,
        )
        await tasks_repo.mark_failed(pool, msg.task_id, str(exc))
        # reject(requeue=False) отправляет сообщение в DLQ через настроенный DLX брокера
        await message.reject(requeue=False)


@broker.subscriber(
    get_tasks_queue(),
    channel=Channel(prefetch_count=settings.prefetch_count),
    ack_policy=AckPolicy.MANUAL,
)
async def handle_task(msg: TaskMessage, message: StreamMessage, logger: Logger) -> None:
    """Обработать задачу: ручной ack/nack, запись в БД и идемпотентность."""
    task = asyncio.current_task()
    if task:
        _inflight.add(task)

    pool = get_pool()
    try:
        # 1. Перевод задачи в статус PROCESSING (идемпотентный upsert)
        await tasks_repo.mark_processing(pool, msg.task_id)

        # 2. Выполнение inference в пуле потоков (без блокировки event loop)
        image_path = (
            str(Path(msg.image_path))
            if Path(msg.image_path).is_absolute()
            else str(Path(settings.data_dir) / msg.image_path)
        )
        started = time.perf_counter()
        detections = await engine.predict(image_path)
        elapsed_ms = (time.perf_counter() - started) * 1000

        # 3. Запись результатов в БД ДО отправки ack (предотвращение потери данных при падении)
        await tasks_repo.mark_done(pool, msg.task_id, detections)
        logger.info("task=%s objects=%d took=%.0f ms", msg.task_id, len(detections), elapsed_ms)

    except Exception as exc:
        await _handle_failure(msg, message, exc)
        return
    finally:
        if task:
            _inflight.discard(task)

    # 4. Штатное подтверждение сообщения брокеру только после успешной фиксации в БД
    await message.ack()
