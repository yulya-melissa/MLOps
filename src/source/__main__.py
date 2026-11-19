"""Запуск источника: `python -m src.source`."""
import asyncio

import structlog

from src import logging_config
from src.config import get_settings
from src.source.producer import run


def main() -> None:
    """Настроить логирование и запустить источник."""
    settings = get_settings()
    logging_config.setup_logging(settings.log_level, service_name="source")
    asyncio.run(run())


if __name__ == "__main__":
    main()