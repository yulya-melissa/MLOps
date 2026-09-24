"""Эндпоинт версии приложения."""

import importlib.metadata
import logging

from fastapi import APIRouter, Request, status

from src.config import Settings
from src.schemas import VersionResponse

log = logging.getLogger(__name__)
router = APIRouter(tags=["Version"])

_DIST_NAME = "fire-detection"


def resolve_version(settings: Settings) -> str:
    """Вернуть версию из метаданных пакета или из настроек."""
    try:
        return importlib.metadata.version(_DIST_NAME)
    except importlib.metadata.PackageNotFoundError:
        return settings.version


@router.get(
    "/api/v1/version",
    response_model=VersionResponse,
    status_code=status.HTTP_200_OK,
    summary="Текущая версия приложения",
)
async def get_version(request: Request) -> VersionResponse:
    """Вернуть версию приложения и окружение."""
    settings: Settings = request.app.state.settings
    return VersionResponse(
        version=resolve_version(settings),
        environment=settings.environment,
    )
