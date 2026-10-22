"""Управление пулом соединений с Postgres (asyncpg)."""

import logging
from pathlib import Path

import asyncpg

from src.config import Settings

log = logging.getLogger(__name__)

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")


async def create_db_pool(settings: Settings) -> asyncpg.Pool:
    """Создать пул соединений с Postgres.

    `min_size=0` — при старте не подключаемся: приложение поднимется, даже
    если база недоступна, и health-эндпоинт покажет это в отчёте.
    """
    pool = await asyncpg.create_pool(
        dsn=settings.database_url,
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
        command_timeout=settings.db_command_timeout,
    )

    log.info(
        "Пул соединений с Postgres создан: %s",
        settings.database_url.rsplit("@", 1)[-1],
    )
    return pool


async def ensure_schema(pool: asyncpg.Pool) -> None:
    """Создать таблицы, если их нет. Безопасно вызывать при каждом старте."""
    async with pool.acquire() as conn, conn.transaction():
        # API и воркер стартуют одновременно: lock защищает от гонки CREATE TABLE
        await conn.execute("SELECT pg_advisory_xact_lock(7403001)")
        await conn.execute(_SCHEMA_PATH.read_text(encoding="utf-8"))
    log.info("Схема БД проверена")


async def close_db_pool(pool: asyncpg.Pool) -> None:
    """Закрыть пул и все его соединения."""
    await pool.close()
    log.info("Пул соединений с Postgres закрыт")
