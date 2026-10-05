"""pyfunc-обёртка над YOLO для MLflow.

Вход:  DataFrame с колонкой `image_path` (путь к картинке).
Выход: DataFrame, одна строка = один найденный объект (bbox + confidence).
"""

import mlflow.pyfunc
import pandas as pd
from mlflow.models import ModelSignature
from mlflow.types import ColSpec, Schema

OUTPUT_COLUMNS = ["image_path", "class_name", "confidence", "x1", "y1", "x2", "y2"]


def get_signature() -> ModelSignature:
    """Описать вход и выход модели (попадёт в MLmodel и в UI)."""
    inputs = Schema([ColSpec("string", "image_path")])
    outputs = Schema(
        [
            ColSpec("string", "image_path"),
            ColSpec("string", "class_name"),
            *[ColSpec("double", c) for c in ("confidence", "x1", "y1", "x2", "y2")],
        ]
    )
    return ModelSignature(inputs=inputs, outputs=outputs)


class FireYoloModel(mlflow.pyfunc.PythonModel):
    """Детектор огня на YOLO, упакованный как MLflow pyfunc."""

    def __init__(self, imgsz: int = 320, conf: float = 0.25, device: str = "cpu") -> None:
        self.imgsz = imgsz
        self.conf = conf
        self.device = device

    def load_context(self, context) -> None:
        """Вызывается один раз при mlflow.pyfunc.load_model: грузим веса."""
        from ultralytics import YOLO

        self._yolo = YOLO(context.artifacts["weights"])

    def predict(self, context, model_input: pd.DataFrame, params=None) -> pd.DataFrame:
        """Прогнать картинки через YOLO и вернуть найденные объекты."""
        paths = [str(p) for p in model_input["image_path"]]
        results = self._yolo.predict(
            paths,
            imgsz=self.imgsz,
            conf=self.conf,
            device=self.device,
            verbose=False,
        )

        rows = []
        for res in results:
            for box in res.boxes:
                x1, y1, x2, y2 = (float(v) for v in box.xyxy[0].tolist())
                rows.append(
                    {
                        "image_path": str(res.path),
                        "class_name": res.names[int(box.cls[0])],
                        "confidence": float(box.conf[0]),
                        "x1": x1,
                        "y1": y1,
                        "x2": x2,
                        "y2": y2,
                    }
                )
        return pd.DataFrame(rows, columns=OUTPUT_COLUMNS)
