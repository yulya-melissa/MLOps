"""Запуск источника: `python -m src.source`."""

import asyncio
import logging

from src.config import get_settings
from src.source.producer import run


def main() -> None:
    """Настроить логирование и запустить источник."""
    logging.basicConfig(
        level=get_settings().log_level.upper(),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )
    asyncio.run(run())


if __name__ == "__main__":
    main()
