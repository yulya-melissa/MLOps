"""Сообщения конвейера."""

from pydantic import BaseModel


class TaskMessage(BaseModel):
    """Задача: прогнать через модель одну картинку.

    `image_path` — путь относительно `settings.data_dir`, а не абсолютный:
    источник и воркер видят датасет по одному и тому же пути в контейнерах.
    """

    task_id: str
    image_path: str
