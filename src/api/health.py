"""Health-эндпоинты приложения (liveness & readiness)."""

import logging

from fastapi import APIRouter, Request, Response, status

from src.schemas import HealthReport, LivenessResponse, ReportStatus
from src.services import health as health_service

log = logging.getLogger(__name__)

router = APIRouter(tags=["Health"])


@router.get(
    "/healthz",
    response_model=LivenessResponse,
    status_code=status.HTTP_200_OK,
    summary="Быстрая проверка доступности сервиса (liveness)",
)
async def liveness() -> LivenessResponse:
    """Вернуть статус живости процесса."""
    return LivenessResponse(status="ok")


@router.get(
    "/api/v1/health",
    response_model=HealthReport,
    status_code=status.HTTP_200_OK,
    summary="End-to-end health-check всех зависимостей",
)
async def health(request: Request, response: Response) -> HealthReport:
    """Вернуть подробный health-отчёт по приложению и зависимостям."""
    report = await health_service.build_report(
        request.app.state.db_pool, request.app.state.settings
    )
    if report.status is not ReportStatus.ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        log.warning("Health report status: %s", report.status.value)
    return report
