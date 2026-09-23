"""Тесты /api/v1/health через моки — не требуют реальной БД."""

from unittest.mock import patch

from httpx import AsyncClient

from src.schemas import DependencyHealth, HealthStatus


async def test_health_ok_mocked(client: AsyncClient) -> None:
    """При healthy Postgres /health отдаёт 200 и status=ok."""
    fake = DependencyHealth(
        status=HealthStatus.healthy,
        latency_ms=1.23,
        version="16.3",
    )
    with patch("src.services.health.check_postgres", return_value=fake):
        response = await client.get("/api/v1/health")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["dependencies"]["postgres"]["status"] == "healthy"
    assert data["dependencies"]["postgres"]["latency_ms"] == 1.23
    assert data["dependencies"]["postgres"]["version"] == "16.3"


async def test_health_degraded_mocked(client: AsyncClient) -> None:
    """При unavailable Postgres /health отдаёт 503 и status=degraded."""
    fake = DependencyHealth(
        status=HealthStatus.unavailable,
        latency_ms=12.34,
        detail="connection refused",
    )
    with patch("src.services.health.check_postgres", return_value=fake):
        response = await client.get("/api/v1/health")

    assert response.status_code == 503
    data = response.json()
    assert data["status"] == "degraded"
    assert data["dependencies"]["postgres"]["status"] == "unavailable"
    assert data["dependencies"]["postgres"]["latency_ms"] == 12.34
    assert data["dependencies"]["postgres"]["detail"] == "connection refused"


async def test_health_mocked_includes_version(client: AsyncClient) -> None:
    """В ответе /health есть версия third-party компонента."""
    fake = DependencyHealth(
        status=HealthStatus.healthy,
        latency_ms=0.5,
        version="16.3",
    )
    with patch("src.services.health.check_postgres", return_value=fake):
        response = await client.get("/api/v1/health")

    data = response.json()
    assert "version" in data["dependencies"]["postgres"]
    assert data["dependencies"]["postgres"]["version"] == "16.3"