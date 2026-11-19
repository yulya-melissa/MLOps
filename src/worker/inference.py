"""Inference модели из MLflow: загрузка один раз, predict в пуле потоков."""

import asyncio
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

import structlog

from src import logging_config

if TYPE_CHECKING:
    from mlflow.pyfunc import PyFuncModel

log = structlog.get_logger(__name__)

# Рабочий вариант — `models:/<name>@<alias>`; ветка с версией оставлена на случай
# явного пиннинга в конфиге. Alias разбираем, а не зашиваем строкой в лог.
_MODEL_URI_RE = re.compile(r"^models:/(?P<name>[^/@]+)(?:@(?P<alias>.+)|/(?P<version>\d+))?$")


class InferenceEngine:
    """Обёртка над pyfunc-моделью, не блокирующая event loop.

    Конкурентность воркера = `concurrency` (размер пула потоков). Сам вызов
    `model.predict` сериализуется локом: один экземпляр YOLO не потокобезопасен.
    Искусственная задержка `delay_s` (имитация тяжёлой модели) лока не держит,
    поэтому при concurrency > 1 задержки идут параллельно.
    """

    def __init__(
        self,
        *,
        tracking_uri: str,
        model_uri: str,
        concurrency: int,
        delay_s: float,
    ) -> None:
        self._tracking_uri = tracking_uri
        self._model_uri = model_uri
        self._delay_s = delay_s
        self._pool = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="infer")
        self._lock = threading.Lock()
        self._model: PyFuncModel | None = None

        match = _MODEL_URI_RE.match(model_uri)
        self._model_name = match["name"] if match else model_uri
        self._model_alias = match["alias"] if match else None
        self._model_version = match["version"] if match else None

    @property
    def model_info(self) -> dict[str, str | None]:
        """Идентификаторы модели для логов: имя, alias и фактическая версия."""
        return {
            "model_name": self._model_name,
            "model_alias": self._model_alias,
            "model_version": self._model_version,
        }

    async def load(self) -> None:
        """Загрузить модель по alias (один раз, при старте воркера)."""
        loop = asyncio.get_running_loop()
        self._model = await loop.run_in_executor(self._pool, self._load_sync)

    def _load_sync(self) -> "PyFuncModel":
        import mlflow
        import mlflow.pyfunc

        # mlflow при импорте ставит свой хендлер и текстовый форматтер.
        # Возвращаем наш JSON сразу, иначе предупреждения библиотеки уйдут текстом.
        logging_config.capture_library_logs()

        mlflow.set_tracking_uri(self._tracking_uri)
        started = time.perf_counter()
        model = mlflow.pyfunc.load_model(self._model_uri)
        if self._model_alias is not None:
            self._model_version = self._version_by_alias()

        log.info(
            "model_loaded_sync",
            model_uri=self._model_uri,
            elapsed_s=round(time.perf_counter() - started, 2),
            **self.model_info,
        )
        return model

    def _version_by_alias(self) -> str | None:
        """Узнать, на какую версию Registry сейчас указывает alias.

        Alias — указатель, его значение меняется: в логе нужна именно версия,
        иначе по логам нельзя сказать, какая модель обработала задачу.
        Версия нужна только для наблюдаемости, поэтому её недоступность
        не должна ломать загрузку модели.
        """
        from mlflow.tracking import MlflowClient

        try:
            client = MlflowClient()
            version = client.get_model_version_by_alias(self._model_name, self._model_alias)
            return version.version
        except Exception:
            log.warning("model_version_unresolved", model_uri=self._model_uri, exc_info=True)
            return None

    async def predict(self, image_path: str) -> list[dict[str, object]]:
        """Прогнать одну картинку; тяжёлая часть выполняется в пуле потоков."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._pool, self._predict_sync, image_path)

    def _predict_sync(self, image_path: str) -> list[dict[str, object]]:
        import pandas as pd

        if self._model is None:
            raise RuntimeError("модель не загружена")
        with self._lock:
            df = self._model.predict(pd.DataFrame({"image_path": [image_path]}))
        if self._delay_s > 0:
            time.sleep(self._delay_s)  # имитация длительности inference
        return df.to_dict("records")

    def shutdown(self) -> None:
        """Дождаться уже запущенных вычислений и закрыть пул."""
        self._pool.shutdown(wait=True)
