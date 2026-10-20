"""Inference модели из MLflow: загрузка один раз, predict в пуле потоков."""

import asyncio
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mlflow.pyfunc import PyFuncModel

log = logging.getLogger(__name__)


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

    async def load(self) -> None:
        """Загрузить модель по alias (один раз, при старте воркера)."""
        loop = asyncio.get_running_loop()
        self._model = await loop.run_in_executor(self._pool, self._load_sync)

    def _load_sync(self) -> "PyFuncModel":
        import mlflow
        import mlflow.pyfunc

        mlflow.set_tracking_uri(self._tracking_uri)
        started = time.perf_counter()
        model = mlflow.pyfunc.load_model(self._model_uri)
        log.info("Модель %s загружена за %.1f c", self._model_uri, time.perf_counter() - started)
        return model

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
