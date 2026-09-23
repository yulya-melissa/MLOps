"""Настройки приложения из переменных окружения (pydantic-settings)."""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Состояние приложения, собираемое из env / .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="FIRE_",
        extra="ignore",
    )

    app_name: str = "fire-detection"
    version: str = "0.1.0"
    environment: Literal["development", "staging", "production", "test"] = "development"
    debug: bool = False
    log_level: str = "INFO"

    host: str = "0.0.0.0"
    port: int = 8000

    # Параметры подключения к Postgres — собираем URL из частей.
    postgres_user: str = "postgres"
    postgres_password: str = "postgres"
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "fire_detection"

    db_pool_min_size: int = 0
    db_pool_max_size: int = 5
    db_command_timeout: float = 5.0

    @property
    def database_url(self) -> str:
        """Собрать DSN для asyncpg из отдельных полей."""
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


@lru_cache
def get_settings() -> Settings:
    """Вернуть кешированный экземпляр настроек."""
    return Settings()