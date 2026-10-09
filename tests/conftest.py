"""Общие pytest-фикѝтуры."""

from unittest.mock import MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

import src.app as app_module
from src.app import create_app


@pytest.fixture
async def app(monkeypatch):
    """?????????? ??? ?????? ??? ???????? ???????? MLflow-??????."""
    monkeypatch.setattr(
        app_module.mlflow.pyfunc,
        "load_model",
        lambda _model_uri: MagicMock(name="fake_inference_model"),
    )

    _app = create_app()

    async with _app.router.lifespan_context(_app):
        yield _app


@pytest.fixture
async def client(app):
    """Теѝтовый HTTP-клиент, привѝзанный к инициализированному приложению."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as ac:
        yield ac
