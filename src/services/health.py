"""Бизнес-логика health-отчёта."""

import logging
import time

import asyncpg

from src.config import Settings
from src.schemas import (
    DependencyHealth,
    HealthReport,
    HealthStatus,
    ReportStatus,
)

log = logging.getLogger(__name__)


async def check_postgres(pool: asyncpg.Pool) -> DependencyHealth:
    """Проверить Postgres: доступность, версию сервера и latency."""
    start = time.perf_counter()
    try:
        async with pool.acquire(timeout=2.0) as conn:
            row = await conn.fetchrow(
                "SELECT current_setting('server_version') AS server_version"
            )
    except Exception as exc:  # noqa: BLE001
        latency_ms = round((time.perf_counter() - start) * 1000, 2)
        log.warning("Ошибка проверки здоровья Postgres: %s", exc)
        return DependencyHealth(
            status=HealthStatus.unavailable,
            latency_ms=latency_ms,
            detail=str(exc),
        )

    latency_ms = round((time.perf_counter() - start) * 1000, 2)
    return DependencyHealth(
        status=HealthStatus.healthy,
        latency_ms=latency_ms,
        version=row["server_version"],
    )


async def build_report(pool: asyncpg.Pool, settings: Settings) -> HealthReport:
    """Собрать итоговый health-отчёт."""
    pg_health = await check_postgres(pool)

    overall_status = (
        ReportStatus.ok
        if pg_health.status == HealthStatus.healthy
        else ReportStatus.degraded
    )

    return HealthReport(
        status=overall_status,
        dependencies={"postgres": pg_health},
    )