"""Тесты эндпоинта версии."""

from httpx import AsyncClient


async def test_version_endpoint(client: AsyncClient) -> None:
    """Проверка ручки /api/v1/version."""
    response = await client.get("/api/v1/version")
    assert response.status_code == 200

    data = response.json()
    assert "version" in data
    assert isinstance(data["version"], str)
    assert data["version"]
    assert "environment" in data
