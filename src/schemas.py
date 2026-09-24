"""Pydantic-схемы для API."""

from enum import StrEnum

from pydantic import BaseModel


class LivenessResponse(BaseModel):
    """Ответ liveness-проверки."""

    status: str = "ok"


class VersionResponse(BaseModel):
    """Ответ о версии приложения."""

    version: str
    environment: str


class HealthStatus(StrEnum):
    """Статус отдельной зависимости."""

    healthy = "healthy"
    unavailable = "unavailable"


class ReportStatus(StrEnum):
    """Общий статус приложения в health-отчёте."""

    ok = "ok"
    degraded = "degraded"


class DependencyHealth(BaseModel):
    """Здоровье зависимости: статус, версия, задержка и деталь при ошибке."""

    status: HealthStatus
    latency_ms: float
    version: str | None = None
    detail: str | None = None


class HealthReport(BaseModel):
    """Сводный health-отчёт по приложению и зависимостям."""

    status: ReportStatus
    dependencies: dict[str, DependencyHealth]
