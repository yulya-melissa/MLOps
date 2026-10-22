"""Shared pytest fixtures."""

from importlib import import_module

import pytest
from httpx import ASGITransport, AsyncClient

app_module = import_module("src.app")


@pytest.fixture
async def app():
    """
    Создание тестового приложения FastAPI без загрузки модели (в API модель больше не грузится)
    """
    test_app = app_module.create_app()

    async with test_app.router.lifespan_context(test_app):
        yield test_app


@pytest.fixture
async def client(app):
    """Создание HTTP-клиента для тестирования API."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as ac:
        yield ac
