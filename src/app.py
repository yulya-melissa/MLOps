"""Точка сборки приложения FastAPI."""
import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from fastapi import FastAPI, Request
from starlette.responses import Response

from src import config, db, logging_config
from src.api import health, tasks, ui, version
from src.pipeline.client import TaskPublisher
from src.services.tasks import PgTaskRepository

log = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Initialize resources on startup and release them on shutdown."""
    settings = config.get_settings()
    logging_config.setup_logging(settings.log_level, service_name="api")
    # Привязываем service_name ко всем логам этого процесса
    structlog.contextvars.bind_contextvars(service="api")

    app.state.settings = settings
    app.state.db_pool = await db.create_db_pool(settings)
    app.state.task_repo = PgTaskRepository(app.state.db_pool)
    app.state.publisher = TaskPublisher(settings)

    try:
        try:
            await db.ensure_schema(app.state.db_pool)
        except Exception:
            log.warning("db_schema_check_failed", exc_info=True)

        await asyncio.to_thread(Path(settings.upload_dir).mkdir, parents=True, exist_ok=True)

        log.info(
            "service_started",
            service=settings.app_name,
            version=settings.version,
            environment=settings.environment,
        )
        yield
    finally:
        await app.state.publisher.close()
        await db.close_db_pool(app.state.db_pool)
        log.info("service_stopped")


def create_app() -> FastAPI:
    """Собрать и настроить приложение FastAPI."""
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
        """Поставить request_id в контекст и залогировать запрос.

        Health-check `/healthz` не логируем на INFO — он дёргается каждые 5 секунд
        и засоряет лог.
        """
        is_health = request.url.path == "/healthz"

        request_id = uuid.uuid4().hex[:8]
        token = logging_config.REQUEST_ID.set(request_id)
        start = time.perf_counter()
        try:
            response = await call_next(request)
            elapsed_ms = (time.perf_counter() - start) * 1000
            if not is_health:
                log.info(
                    "http_request",
                    method=request.method,
                    path=request.url.path,
                    status=response.status_code,
                    elapsed_ms=round(elapsed_ms, 1),
                )
        finally:
            logging_config.REQUEST_ID.reset(token)

        response.headers["X-Request-ID"] = request_id
        return response

    app.include_router(version.router)
    app.include_router(health.router)
    app.include_router(tasks.router)
    app.include_router(ui.router)
    return app


app = create_app()