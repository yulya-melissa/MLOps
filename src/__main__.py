"""CLI-точка входа: `fire-detection` или `python -m src`."""

import uvicorn

from src.config import get_settings


def main() -> None:
    """Запуѝтить приложение через uvicorn ѝ наѝтройками из окружениѝ."""
    settings = get_settings()
    uvicorn.run(
        "src.app:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        reload=settings.debug,
    )


if __name__ == "__main__":  # pragma: no cover
    main()
