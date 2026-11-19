"""
FastStream-воркер: обработка задач с ack_policy (MANUAL), повторами (requeue=True) и DLQ
"""
import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from faststream import AckPolicy, FastStream, Logger
from faststream.rabbit import Channel, RabbitBroker, RabbitMessage

from src import db, logging_config
from src.config import get_settings
from src.pipeline.messages import TaskMessage
from src.pipeline.topology import get_dlq_queue, get_tasks_queue
from src.services.tasks import PgTaskRepository, TaskRepository
from src.worker.inference import InferenceEngine

log = logging.getLogger(__name__)
_settings = get_settings()

broker = RabbitBroker(
    _settings.rabbit_url,
    graceful_timeout=_settings.worker_shutdown_timeout_s,
)
engine = InferenceEngine(
    tracking_uri=_settings.mlflow_tracking_uri,
    model_uri=_settings.model_uri,
    concurrency=_settings.inference_concurrency,
    delay_s=_settings.inference_delay_s,
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
    if _settings.prefetch_count < _settings.inference_concurrency:
        log.warning(
            "worker_prefetch_mismatch",
            prefetch_count=_settings.prefetch_count,
            inference_concurrency=_settings.inference_concurrency,
            message="prefetch_count меньше конкурентности inference: воркер будет простаивать",
        )

    pool = await db.create_db_pool(_settings)
    try:
        await db.ensure_schema(pool)
        _repo = PgTaskRepository(pool)

        # Загрузка MLflow-модели один раз при старте воркера (критерий 1.2)
        log.info("model_loading_started", model_uri=_settings.model_uri)
        await engine.load()
        # Логируем фактическую версию модели после загрузки — требование задания 1.4
        log.info(
            "model_loaded",
            model_uri=_settings.model_uri,
            model_alias="champion",
        )
        yield
    finally:
        log.info(
            "graceful_shutdown_started",
            inflight_tasks=len(_inflight),
            timeout_s=_settings.worker_shutdown_timeout_s,
        )
        if _inflight:
            _, pending = await asyncio.wait(_inflight, timeout=_settings.worker_shutdown_timeout_s)
            if pending:
                log.warning(
                    "graceful_shutdown_timeout",
                    pending_tasks=len(pending),
                )
        engine.shutdown()
        await db.close_db_pool(pool)
        _repo = None
        log.info("graceful_shutdown_completed")


app = FastStream(broker, lifespan=lifespan)


@app.after_startup
async def _declare_dlq() -> None:
    """Объявить DLQ в воркере: иначе, если API не запускался, брокер молча выбросит
    мёртвые сообщения (dead-letter в несуществующую очередь = потеря)."""
    await broker.declare_queue(get_dlq_queue())


async def _handle_failure(msg: TaskMessage, message: RabbitMessage, exc: Exception) -> None:
    """Обработка ошибок: повтор через reject(requeue=True) либо DLQ после исчерпания попыток."""
    headers = message.headers or {}
    current_attempt = int(headers.get("x-delivery-count", 0)) + 1

    log.error(
        "task_processing_failed",
        task_id=msg.task_id,
        attempt=current_attempt,
        max_attempts=_settings.worker_max_attempts,
        error=str(exc),
        exc_info=True,
    )

    if current_attempt < _settings.worker_max_attempts:
        await asyncio.sleep(_settings.worker_retry_delay_s)
        await message.reject(requeue=True)
    else:
        log.error(
            "task_moved_to_dlq",
            task_id=msg.task_id,
            attempts=current_attempt,
            error=str(exc),
        )
        await _get_repo().mark_failed(msg.task_id, str(exc))
        await message.reject(requeue=False)


async def _process(msg: TaskMessage, message: RabbitMessage) -> None:
    """Обработать одну задачу. Вынесено из хендлера, чтобы тестировать без брокера.

    ВАЖНО: параметр `logger` от FastStream убран, так как мы используем
    глобальный structlog-логгер `log`, который поддерживает именованные аргументы.
    """
    task = asyncio.current_task()
    if task:
        _inflight.add(task)
    try:
        repo = _get_repo()
        try:
            # 1. PROCESSING (идемпотентный upsert). False => задача уже DONE:
            if not await repo.mark_processing(msg.task_id, msg.image_path):
                log.info("task_already_done")  # task_id уже в контексте из handle_task
                await message.ack()
                return

            # 2. Inference в пуле потоков (event loop не блокируется)
            image_path = (
                str(Path(msg.image_path))
                if Path(msg.image_path).is_absolute()
                else str(Path(_settings.data_dir) / msg.image_path)
            )
            started = time.perf_counter()
            detections = await engine.predict(image_path)
            elapsed_ms = (time.perf_counter() - started) * 1000

            # 3. Результат в БД ДО ack
            await repo.mark_done(msg.task_id, detections)
            log.info(
                "task_completed",
                objects_count=len(detections),
                elapsed_ms=round(elapsed_ms, 1),
            )
        except Exception as exc:
            await _handle_failure(msg, message, exc)
            return

        # 4. ack только после успешной фиксации в БД
        await message.ack()
    finally:
        if task:
            _inflight.discard(task)


@broker.subscriber(
    get_tasks_queue(),
    channel=Channel(prefetch_count=_settings.prefetch_count),
    ack_policy=AckPolicy.MANUAL,
)
async def handle_task(msg: TaskMessage, message: RabbitMessage, logger: Logger) -> None:
    """Хендлер очереди: ручной ack, запись в БД, идемпотентность.

    КЛЮЧЕВОЙ МОМЕНТ: кладём task_id в contextvar, чтобы все логи внутри
    _process (и глубже) автоматически получили поле task_id через merge_contextvars.
    """
    # Устанавливаем task_id в контекст для всех последующих логов
    token = logging_config.TASK_ID.set(msg.task_id)
    try:
        await _process(msg, message)  # <-- передаём только msg и message
    finally:
        logging_config.TASK_ID.reset(token)