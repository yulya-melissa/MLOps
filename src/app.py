"""Точка сборки приложения FastAPI."""

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from starlette.responses import Response

from src import config, db, logging_config
from src.api import health, tasks, ui, version
from src.pipeline.client import TaskPublisher
from src.services.tasks import PgTaskRepository

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Initialize resources on startup and release them on shutdown."""
    settings = config.get_settings()
    logging_config.setup_logging(settings.log_level)
    app.state.settings = settings

    # Модель живёт только в воркере: API лишь принимает задачи и отдаёт статусы.
    app.state.db_pool = await db.create_db_pool(settings)
    app.state.task_repo = PgTaskRepository(app.state.db_pool)
    app.state.publisher = TaskPublisher(settings)

    try:
        try:
            await db.ensure_schema(app.state.db_pool)
        except Exception:
            # API должен подняться и при недоступной БД: /healthz покажет проблему.
            log.warning("Не удалось проверить схему БД", exc_info=True)

        await asyncio.to_thread(Path(settings.upload_dir).mkdir, parents=True, exist_ok=True)

        log.info(
            "Service %s version %s started successfully!",
            settings.app_name,
            settings.version,
        )

        yield
    finally:
        await app.state.publisher.close()
        await db.close_db_pool(app.state.db_pool)
        log.info("Application stopped")


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
        """Поставить request_id в контекст и залогировать запрос."""
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
    app.include_router(tasks.router)
    app.include_router(ui.router)
    return app


app = create_app()
