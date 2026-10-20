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

    # --- RabbitMQ / конвейер ---
    rabbit_url: str = "amqp://fire:fire_password@localhost:5672/"
    tasks_queue: str = "fire.tasks"
    queue_max_length: int = 100

    # --- воркер ---
    prefetch_count: int = 1
    inference_concurrency: int = 1
    inference_delay_s: float = 0.0
    mlflow_tracking_uri: str = "http://localhost:5000"
    model_uri: str = "models:/fire-detector@champion"
    data_dir: str = "data"

    # --- источник ---
    source_limit: int = 0  # 0 = без ограничения
    source_repeat: int = 1  # сколько раз пройти датасет
    source_backoff_min_s: float = 0.01
    source_backoff_max_s: float = 0.2
    source_report_interval_s: float = 2.0

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
