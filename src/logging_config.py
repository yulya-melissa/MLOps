"""Настройка структурированного логирования через structlog.

Все сервисы (api / worker / source) вызывают `setup_logging(level, service_name=...)`
до первой записи в лог. Логи уходят в stdout в JSON: приложение не знает
о существовании агента и хранилища.

Единый набор полей: `event`, `level`, `logger`, `timestamp`, `service` плюс
`request_id` / `task_id` из contextvars, если они установлены. Поля получают
и записи стандартного logging — от наших модулей и от библиотек.
"""

from __future__ import annotations

import contextvars
import logging
import re
import sys

import structlog

# --- Контекстные переменные --------------------------------------------------
# request_id — HTTP-запрос в API; task_id — задача конвейера.
REQUEST_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id",
    default=None,
)
TASK_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "task_id",
    default=None,
)

# --- Секреты -----------------------------------------------------------------
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

# Пароль внутри URL: `scheme://user:password@host` -> `scheme://user:***@host`.
# Нужен отдельно от _SECRET_HINTS: DSN попадает в текст сообщения или исключения,
# где имени поля нет и маскировать по ключу нечего.
_URL_CREDENTIALS = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://[^:/\s@]+:)[^@\s]+@")

# Библиотеки, которые ставят собственные хендлеры и форматтеры.
_LIBRARY_LOGGERS: tuple[str, ...] = (
    "uvicorn",
    "uvicorn.error",
    "uvicorn.access",
    "fastapi",
    "asyncpg",
    "aio_pika",
    "aiormq",
    "faststream",
    "mlflow",
)


# --- Собственные процессоры --------------------------------------------------
def _mask_secrets(
    _logger: structlog.types.WrappedLogger,
    _method: str,
    event_dict: structlog.types.EventDict,
) -> structlog.types.EventDict:
    """Затереть значения полей, чьи имена похожи на секреты."""
    for key in list(event_dict):
        if any(hint in key.lower() for hint in _SECRET_HINTS):
            event_dict[key] = "***"
    return event_dict


def _mask_url_credentials(
    _logger: structlog.types.WrappedLogger,
    _method: str,
    event_dict: structlog.types.EventDict,
) -> structlog.types.EventDict:
    """Затереть пароль внутри строковых значений (DSN, AMQP URL, traceback)."""
    for key, value in event_dict.items():
        if isinstance(value, str):
            event_dict[key] = _URL_CREDENTIALS.sub(r"\1***@", value)
    return event_dict


def _merge_context(
    _logger: structlog.types.WrappedLogger,
    _method: str,
    event_dict: structlog.types.EventDict,
) -> structlog.types.EventDict:
    """Подмешать в запись значения наших contextvars: request_id и task_id.

    `structlog.contextvars.merge_contextvars` видит только переменные,
    установленные через `structlog.contextvars.bind_contextvars()`.
    REQUEST_ID и TASK_ID — обычные contextvars.ContextVar, поэтому читаем сами.
    Значение берётся в момент вызова log.*, а не создания логгера.
    """
    request_id = REQUEST_ID.get()
    if request_id is not None:
        event_dict.setdefault("request_id", request_id)

    task_id = TASK_ID.get()
    if task_id is not None:
        event_dict.setdefault("task_id", task_id)

    return event_dict


# --- Публичный API -----------------------------------------------------------
def setup_logging(level: str, *, service_name: str) -> None:
    """Настроить корневой логгер и structlog в едином JSON-формате.

    Args:
        level: уровень логирования, читается из `FIRE_LOG_LEVEL`.
        service_name: `api` / `worker` / `source` — поле `service` каждой записи.
    """
    log_level = level.upper()

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        _merge_context,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
        structlog.processors.format_exc_info,
        _mask_secrets,
        _mask_url_credentials,
        lambda _logger, _method, event_dict: event_dict | {"service": service_name},
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # foreign_pre_chain — те же процессоры для записей стандартного logging.
    # Без него логи библиотек рендерятся как {"event": "..."} без остальных полей.
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(log_level)

    for name in _LIBRARY_LOGGERS:
        logging.getLogger(name).setLevel(log_level)
    # access-лог дублирует наш middleware и засоряет INFO health-check'ами.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    capture_library_logs()


def capture_library_logs() -> None:
    """Отдать логгерам библиотек наш JSON-хендлер.

    Библиотеки настраивают logging сами и затирают хендлер из setup_logging:
    uvicorn — до импорта приложения, mlflow — при импорте, FastStream — при
    старте брокера. Поэтому функцию вызывают повторно, когда они загрузились.
    """
    root_handlers = logging.getLogger().handlers
    if not root_handlers:
        return

    for name in _LIBRARY_LOGGERS:
        lib_logger = logging.getLogger(name)
        lib_logger.handlers = list(root_handlers)
        lib_logger.propagate = False
