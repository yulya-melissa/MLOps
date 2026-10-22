"""
FastStream-воркер: обработка задач с ack_policy (MANUAL), повторами (requeue=True) и DLQ
"""

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from logging import Logger as StdLogger
from pathlib import Path

from faststream import AckPolicy, FastStream, Logger
from faststream.rabbit import Channel, RabbitBroker, RabbitMessage

from src import db
from src.config import get_settings
from src.pipeline.messages import TaskMessage
from src.pipeline.topology import get_dlq_queue, get_tasks_queue
from src.services.tasks import PgTaskRepository, TaskRepository
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

# Репозиторий создаётся в lifespan (нужен пул соединений); в тестах подменяется.
_repo: TaskRepository | None = None

# Трекер активных задач для graceful shutdown (критерий 5.4)
_inflight: set[asyncio.Task[None]] = set()


def _get_repo() -> TaskRepository:
    if _repo is None:
        raise RuntimeError("репозиторий не инициализирован (lifespan не запускался)")
    return _repo


@asynccontextmanager
async def lifespan() -> AsyncIterator[None]:
    """Инициализация пула БД, загрузка модели и graceful shutdown."""
    global _repo

    if settings.prefetch_count < settings.inference_concurrency:
        log.warning(
            "prefetch_count=%d меньше конкурентности inference=%d: воркер будет простаивать",
            settings.prefetch_count,
            settings.inference_concurrency,
        )

    pool = await db.create_db_pool(settings)
    try:
        await db.ensure_schema(pool)
        _repo = PgTaskRepository(pool)

        # Загрузка MLflow-модели один раз при старте воркера (критерий 1.2)
        await engine.load()
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
        await db.close_db_pool(pool)
        _repo = None
        log.info("Воркер успешно остановлен.")


app = FastStream(broker, lifespan=lifespan)


@app.after_startup
async def _declare_dlq() -> None:
    """Объявить DLQ в воркере: иначе, если API не запускался, брокер молча выбросит
    мёртвые сообщения (dead-letter в несуществующую очередь = потеря)."""
    await broker.declare_queue(get_dlq_queue())


async def _handle_failure(msg: TaskMessage, message: RabbitMessage, exc: Exception) -> None:
    """Обработка ошибок: повтор через reject(requeue=True) либо DLQ после исчерпания попыток."""
    # В quorum-очередях число доставок хранится в x-delivery-count
    # (растёт при reject(requeue=True) и при падении клиента, но НЕ при nack).
    # На первой доставке заголовка нет, поэтому попытка = 1 + значение.
    headers = message.headers or {}
    current_attempt = int(headers.get("x-delivery-count", 0)) + 1

    log.error(
        "Ошибка при обработке task=%s (попытка %d/%d): %s",
        msg.task_id,
        current_attempt,
        settings.worker_max_attempts,
        exc,
    )

    if current_attempt < settings.worker_max_attempts:
        await asyncio.sleep(settings.worker_retry_delay_s)
        # reject(requeue=True) увеличивает x-delivery-count и возвращает сообщение в очередь
        await message.reject(requeue=True)
    else:
        log.error(
            "Исчерпаны все попытки (%d) для task=%s. Перенаправление в DLQ.",
            settings.worker_max_attempts,
            msg.task_id,
        )
        await _get_repo().mark_failed(msg.task_id, str(exc))
        # reject(requeue=False): брокер сам перенаправит сообщение в DLQ (DLX очереди)
        await message.reject(requeue=False)


async def _process(msg: TaskMessage, message: RabbitMessage, logger: StdLogger) -> None:
    """Обработать одну задачу. Вынесено из хендлера, чтобы тестировать без брокера."""
    task = asyncio.current_task()
    if task:
        _inflight.add(task)

    try:
        repo = _get_repo()
        try:
            # 1. PROCESSING (идемпотентный upsert). False => задача уже DONE:
            #    это повторная доставка, инференс повторно не запускаем (критерий 5.3).
            if not await repo.mark_processing(msg.task_id, msg.image_path):
                logger.info("task=%s уже DONE: повторная доставка, пропускаем", msg.task_id)
                await message.ack()
                return

            # 2. Inference в пуле потоков (event loop не блокируется)
            image_path = (
                str(Path(msg.image_path))
                if Path(msg.image_path).is_absolute()
                else str(Path(settings.data_dir) / msg.image_path)
            )
            started = time.perf_counter()
            detections = await engine.predict(image_path)
            elapsed_ms = (time.perf_counter() - started) * 1000

            # 3. Результат в БД ДО ack (иначе потеря данных при падении воркера)
            await repo.mark_done(msg.task_id, detections)
            logger.info("task=%s objects=%d took=%.0f ms", msg.task_id, len(detections), elapsed_ms)
        except Exception as exc:
            await _handle_failure(msg, message, exc)
            return

        # 4. ack только после успешной фиксации в БД. Он вне try/except выше:
        #    сбой ack не должен превращать уже DONE задачу в FAILED.
        await message.ack()
    finally:
        # Задача считается «в работе» до самого ack — shutdown дождётся и его.
        if task:
            _inflight.discard(task)


@broker.subscriber(
    get_tasks_queue(),
    channel=Channel(prefetch_count=settings.prefetch_count),
    ack_policy=AckPolicy.MANUAL,
)
async def handle_task(msg: TaskMessage, message: RabbitMessage, logger: Logger) -> None:
    """Хендлер очереди: ручной ack, запись в БД, идемпотентность."""
    await _process(msg, message, logger)
