"""Теѝты health-ѝндпоинтов (интеграционные, требуют реальной инфраѝтруктуры)."""

import asyncio

import asyncpg
import pytest
from httpx import AsyncClient

from src.config import get_settings


async def _postgres_available_async() -> bool:
    """????????? ???????? ?????? ? PostgreSQL ??????????? SQL-???????."""
    conn = None

    try:
        conn = await asyncpg.connect(
            dsn=get_settings().database_url,
            timeout=1.0,
        )
        await conn.fetchval("SELECT current_setting('server_version')")
        return True

    except Exception:
        return False

    finally:
        if conn is not None:
            await conn.close()


def postgres_available() -> bool:
    """????????? PostgreSQL ?? ??????? ??????????????? ?????."""
    return asyncio.run(_postgres_available_async())


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
