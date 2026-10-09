"""Наѝтройки приложениѝ из переменных окружениѝ (pydantic-settings)."""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Соѝтоѝние приложениѝ, ѝобираемое из env / .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="FIRE_",
        extra="ignore",
    )

    app_name: str = "fire-detection"
    version: str = "0.1.2"  # keep in sync with pyproject.toml for fallback
    environment: Literal["development", "staging", "production", "test"] = "development"
    debug: bool = False
    log_level: str = "INFO"

    host: str = "0.0.0.0"
    port: int = 8000

    # Параметры подключениѝ к Postgres — ѝобираем URL из чаѝтей.
    postgres_user: str = "postgres"
    postgres_password: str = "postgres"
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "fire_detection"

    db_pool_min_size: int = 0
    db_pool_max_size: int = 5
    db_command_timeout: float = 5.0

    mlflow_tracking_uri: str = "http://localhost:5000"
    mlflow_model_uri: str = "models:/fire-detector@champion"

    @property
    def database_url(self) -> str:
        """Собрать DSN длѝ asyncpg из отдельных полей."""
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


@lru_cache
def get_settings() -> Settings:
    """Вернуть кешированный ѝкземплѝр наѝтроек."""
    return Settings()
