"""Shared pytest fixtures."""

from importlib import import_module
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

app_module = import_module("src.app")


@pytest.fixture
async def app(monkeypatch):
    """Create an app without loading a real MLflow model."""

    fake_model = MagicMock(name="fake_inference_model")

    monkeypatch.setattr(
        app_module,
        "run_in_threadpool",
        AsyncMock(return_value=fake_model),
    )

    test_app = app_module.create_app()

    async with test_app.router.lifespan_context(test_app):
        yield test_app


@pytest.fixture
async def client(app):
    """Create an HTTP client for API tests."""

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as ac:
        yield ac
