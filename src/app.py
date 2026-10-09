"""Точка сборки приложения FastAPI."""

import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import mlflow
from fastapi import FastAPI, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response

from src import config, db, logging_config
from src.api import health, process, ui, version

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Initialize resources on startup and release them on shutdown."""
    settings = config.get_settings()
    logging_config.setup_logging(settings.log_level)
    app.state.settings = settings

    app.state.db_pool = await db.create_db_pool(settings)

    try:
        mlflow.set_tracking_uri(settings.mlflow_tracking_uri)

        log.info(
            "Loading inference model from MLflow: %s",
            settings.mlflow_model_uri,
        )

        app.state.inference_model = await run_in_threadpool(
            mlflow.pyfunc.load_model,
            settings.mlflow_model_uri,
        )

        log.info(
            "Service %s version %s started successfully!",
            settings.app_name,
            settings.version,
        )

        yield
    finally:
        await db.close_db_pool(app.state.db_pool)
        log.info("Application stopped")


def create_app() -> FastAPI:
    """Собрать и наѝтроить приложение FastAPI."""
    settings = config.get_settings()
    app = FastAPI(
        title=settings.app_name,
        version=settings.version,
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def log_requests(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """Поѝтавить request_id в контекѝт и залогировать запроѝ."""
        request_id = uuid.uuid4().hex[:8]
        token = logging_config.REQUEST_ID.set(request_id)
        start = time.perf_counter()
        try:
            response = await call_next(request)
            elapsed_ms = (time.perf_counter() - start) * 1000
            log.info(
                "%s %s -> %d (%.1f ms)",
                request.method,
                request.url.path,
                response.status_code,
                elapsed_ms,
            )
        finally:
            logging_config.REQUEST_ID.reset(token)

        response.headers["X-Request-ID"] = request_id
        return response

    app.include_router(version.router)
    app.include_router(health.router)
    app.include_router(process.router)
    app.include_router(ui.router)
    return app


app = create_app()
