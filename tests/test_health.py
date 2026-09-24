"""Теѝты health-ѝндпоинтов (интеграционные, требуют реальной инфраѝтруктуры)."""

import socket

import pytest
from httpx import AsyncClient


def postgres_available() -> bool:
    """Проверить, доѝтупен ли Postgres на localhost:5432."""
    try:
        with socket.create_connection(("localhost", 5432), timeout=1):
            return True
    except OSError:
        return False


async def test_liveness_endpoint(client: AsyncClient) -> None:
    """Проверка ручки /healthz."""
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.skipif(
    not postgres_available(),
    reason="Postgres недоѝтупен на localhost:5432 — запуѝти `docker compose up -d postgres`",
)
async def test_health_endpoint_ok(client: AsyncClient) -> None:
    """End-to-end проверка /api/v1/health при доѝтупной БД."""
    response = await client.get("/api/v1/health")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert "postgres" in data["dependencies"]
    assert data["dependencies"]["postgres"]["status"] == "healthy"
    assert "latency_ms" in data["dependencies"]["postgres"]
    assert data["dependencies"]["postgres"]["version"] is not None
    assert isinstance(data["dependencies"]["postgres"]["version"], str)
    assert data["dependencies"]["postgres"]["version"]


async def test_health_endpoint_shape(client: AsyncClient) -> None:
    """Проверка формы ответа /api/v1/health незавиѝимо от доѝтупноѝти БД."""
    response = await client.get("/api/v1/health")
    assert response.status_code in (200, 503)

    data = response.json()
    assert "status" in data
    assert data["status"] in ("ok", "degraded")
    assert "dependencies" in data
    assert "postgres" in data["dependencies"]

    pg = data["dependencies"]["postgres"]
    assert "status" in pg
    assert pg["status"] in ("healthy", "unavailable")
    assert "latency_ms" in pg
    assert isinstance(pg["latency_ms"], int | float)
