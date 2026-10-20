"""Пакетный источник задач с обратной связью от брокера."""

import asyncio
import logging
import random
import time
from collections.abc import Iterator
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from uuid import uuid4

from faststream.rabbit import RabbitBroker

from src.config import Settings, get_settings
from src.pipeline.messages import TaskMessage
from src.pipeline.publisher import QueueFullError, publish_task
from src.pipeline.topology import get_tasks_queue

log = logging.getLogger(__name__)

IMG_EXT = {".jpg", ".jpeg", ".png", ".bmp"}


@dataclass
class Stats:
    """Счётчики источника (для логов и эксперимента)."""

    published: int = 0
    rejected: int = 0


def iter_images(data_dir: Path) -> Iterator[Path]:
    """Лениво обходить картинки датасета (папки images/), не собирая список в памяти."""
    for path in data_dir.rglob("*"):
        if path.suffix.lower() in IMG_EXT and path.parent.name == "images":
            yield path


def iter_tasks(settings: Settings) -> Iterator[TaskMessage]:
    """Поток задач: `source_repeat` проходов по датасету, не более `source_limit` задач."""
    data_dir = Path(settings.data_dir)

    def all_tasks() -> Iterator[TaskMessage]:
        for _ in range(settings.source_repeat):
            for path in iter_images(data_dir):
                yield TaskMessage(
                    task_id=uuid4().hex,
                    image_path=path.relative_to(data_dir).as_posix(),
                )

    return islice(all_tasks(), settings.source_limit or None)


async def publish_with_backoff(
    broker: RabbitBroker,
    msg: TaskMessage,
    stats: Stats,
    settings: Settings,
) -> None:
    """Публиковать задачу, пока брокер не примет её.

    Это и есть backpressure: пока очередь полна, источник не берёт следующую задачу
    и держит в памяти ровно одну. Пауза растёт экспоненциально (с jitter) до
    `source_backoff_max_s` — её нужно держать заметно меньше времени inference,
    иначе очередь успеет опустеть, пока источник спит.
    """
    delay = settings.source_backoff_min_s
    while True:
        try:
            await publish_task(broker, msg)
        except QueueFullError:
            stats.rejected += 1
            await asyncio.sleep(delay * (1 + random.random()))
            delay = min(delay * 2, settings.source_backoff_max_s)
        else:
            stats.published += 1
            return


async def report(stats: Stats, interval_s: float) -> None:
    """Периодически логировать скорость публикации."""
    last_time, last_published = time.perf_counter(), 0
    while True:
        await asyncio.sleep(interval_s)
        now = time.perf_counter()
        rate = (stats.published - last_published) / (now - last_time)
        log.info(
            "published=%d rate=%.2f msg/s rejected(nack)=%d",
            stats.published,
            rate,
            stats.rejected,
        )
        last_time, last_published = now, stats.published


async def run() -> None:
    """Прогнать датасет через очередь с учётом перегрузки."""
    settings = get_settings()
    stats = Stats()
    started = time.perf_counter()

    async with RabbitBroker(settings.rabbit_url) as broker:
        # Объявляем очередь сами: иначе при старте раньше воркера сообщения
        # ушли бы в никуда (default exchange без очереди).
        await broker.declare_queue(get_tasks_queue())
        reporter = asyncio.create_task(report(stats, settings.source_report_interval_s))
        try:
            for msg in iter_tasks(settings):
                await publish_with_backoff(broker, msg, stats, settings)
        finally:
            reporter.cancel()

    elapsed = time.perf_counter() - started
    log.info(
        "Готово: published=%d rejected=%d за %.1f c (средняя скорость %.2f msg/s)",
        stats.published,
        stats.rejected,
        elapsed,
        stats.published / elapsed if elapsed else 0.0,
    )
