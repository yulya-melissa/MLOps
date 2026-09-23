"""Общие pytest-фикстуры."""

import pytest
from httpx import ASGITransport, AsyncClient

from src.app import create_app


@pytest.fixture
async def app():
    """Приложение с принудительно выполненным lifespan (startup/shutdown)."""
    _app = create_app()
    async with _app.router.lifespan_context(_app):
        yield _app


@pytest.fixture
async def client(app):
    """Тестовый HTTP-клиент, привязанный к инициализированному приложению."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as ac:
        yield ac