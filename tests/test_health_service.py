"""Юнит-тесты бизнес-логики health-отчёта."""

from unittest.mock import AsyncMock, MagicMock

from src.schemas import DependencyHealth, HealthStatus, ReportStatus
from src.services.health import build_report, check_postgres


async def test_check_postgres_healthy() -> None:
    """check_postgres возвращает healthy, версию сервера и latency при успехе."""
    conn = AsyncMock()
    conn.fetchrow.return_value = {"server_version": "16.3"}

    pool = MagicMock()
    pool.acquire.return_value.__aenter__.return_value = conn

    result = await check_postgres(pool)

    assert result.status == HealthStatus.healthy
    assert result.version == "16.3"
    assert result.latency_ms >= 0
    assert result.detail is None


async def test_check_postgres_unavailable() -> None:
    """check_postgres возвращает unavailable и detail при ошибке."""
    pool = MagicMock()
    pool.acquire.side_effect = RuntimeError("connection refused")

    result = await check_postgres(pool)

    assert result.status == HealthStatus.unavailable
    assert result.latency_ms >= 0
    assert result.detail == "connection refused"
    assert result.version is None


async def test_build_report_ok() -> None:
    """build_report возвращает ok, если Postgres healthy."""
    pool = MagicMock()
    settings = MagicMock()

    healthy = DependencyHealth(
        status=HealthStatus.healthy,
        latency_ms=1.0,
        version="16.3",
    )

    # Подменяем check_postgres прямо в модуле services.health
    import src.services.health as health_mod

    original = health_mod.check_postgres
    health_mod.check_postgres = AsyncMock(return_value=healthy)
    try:
        report = await build_report(pool, settings)
    finally:
        health_mod.check_postgres = original

    assert report.status == ReportStatus.ok
    assert report.dependencies["postgres"].status == HealthStatus.healthy


async def test_build_report_degraded() -> None:
    """build_report возвращает degraded, если Postgres недоступен."""
    pool = MagicMock()
    settings = MagicMock()

    unavailable = DependencyHealth(
        status=HealthStatus.unavailable,
        latency_ms=12.0,
        detail="boom",
    )

    import src.services.health as health_mod

    original = health_mod.check_postgres
    health_mod.check_postgres = AsyncMock(return_value=unavailable)
    try:
        report = await build_report(pool, settings)
    finally:
        health_mod.check_postgres = original

    assert report.status == ReportStatus.degraded
    assert report.dependencies["postgres"].status == HealthStatus.unavailable