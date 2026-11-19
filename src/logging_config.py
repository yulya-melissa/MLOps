"""Настройка структурированного логирования через structlog.

Все сервисы (api / worker / source) вызывают `setup_logging(level, service_name=...)`
в начале своей работы. Логи уходят в stdout в JSON-формате, приложение не знает
о существовании агента и хранилища.

Контекстные переменные (request_id, task_id) автоматически попадают в каждую
запись лога через `structlog.contextvars.merge_contextvars` — их не нужно
дописывать руками в каждую строку.
"""
from __future__ import annotations

import contextvars
import logging
import sys
from typing import Any

import structlog

# ---------------------------------------------------------------------------
# Контекстные переменные
# ---------------------------------------------------------------------------
# request_id — для HTTP-запросов в API (уже используется в app.py middleware)
# task_id — для задач конвейера; воркер выставляет его при получении сообщения,
#           источник/API — при создании задачи.
REQUEST_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id",
    default=None,
)
TASK_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "task_id",
    default=None,
)

# Ключи, которые считаются секретными и маскируются в логах.
# Сравнение регистронезависимое, по вхождению подстроки.
_SECRET_HINTS: tuple[str, ...] = (
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "dsn",
)


# ---------------------------------------------------------------------------
# Собственные процессоры structlog
# ---------------------------------------------------------------------------
def _mask_secrets(
    _logger: Any,
    _method: str,
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    """Затереть значения полей, чьи имена похожи на секреты.

    Работает по вхождению подстроки: `postgres_password`, `Authorization`,
    `X-Api-Key` и т.п. — всё будет заменено на `***`.
    """
    for key in list(event_dict.keys()):
        lower_key = key.lower()
        if any(hint in lower_key for hint in _SECRET_HINTS):
            event_dict[key] = "***"
    return event_dict


# ---------------------------------------------------------------------------
# Публичный API
# ---------------------------------------------------------------------------
def setup_logging(level: str, *, service_name: str) -> None:
    """Настроить корневой логгер и structlog в едином JSON-формате.

    Args:
        level: Уровень логирования (DEBUG / INFO / WARNING / ERROR).
            Читается из переменной окружения `FIRE_LOG_LEVEL`.
        service_name: Имя сервиса (`api` / `worker` / `source`).
            Попадает в поле `service` каждой записи — это лейбл низкой
            кардинальности, по которому агент логов группирует потоки.
    """
    # --- 1. Процессоры, общие для structlog и стандартного logging ----------
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,   # request_id, task_id из контекста
        structlog.stdlib.add_log_level,            # поле `level`
        structlog.stdlib.add_logger_name,          # поле `logger`
        structlog.processors.TimeStamper(fmt="iso"),  # поле `timestamp` в ISO-8601
        structlog.processors.StackInfoRenderer(),  # стек при log.debug(..., stack_info=True)
        structlog.processors.UnicodeDecoder(),
        structlog.processors.format_exc_info,      # traceback в поле `exception`
        _mask_secrets,                             # затирание секретов
    ]

    # --- 2. Конфигурация structlog ------------------------------------------
    structlog.configure(
        processors=[
            *shared_processors,
            # Передаём запись в стандартный logging, где её подхватит
            # ProcessorFormatter и превратит в JSON.
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # --- 3. Форматтер для стандартного logging ------------------------------
    # JSON — единственный разрешённый формат (требование задания).
    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    # Сбрасываем ранее установленные handlers (на случай повторного вызова)
    root.handlers.clear()
    root.handlers.append(handler)
    root.setLevel(level.upper())

    # --- 4. Подавляем шум ---------------------------------------------------
    # uvicorn.access логирует каждый запрос, включая health-check — это засоряет
    # INFO-уровень. Переводим его на WARNING: ошибки соединения всё равно увидим.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    # --- 5. Перехватываем логи библиотек в наш форматтер --------------------
    # Важно: propagate=False, чтобы запись не уходила в root дважды.
    for name in (
        "uvicorn",
        "uvicorn.error",
        "fastapi",
        "asyncpg",
        "aio_pika",
        "aiormq",
        "faststream",
        "mlflow",
    ):
        lib_logger = logging.getLogger(name)
        lib_logger.handlers = [handler]
        lib_logger.propagate = False
        # Уровень библиотек — не ниже INFO, но access уже отфильтрован выше.
        if lib_logger.level == logging.NOTSET:
            lib_logger.setLevel(level.upper())