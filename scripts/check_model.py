"""Проверка: какая версия стоит под alias и работает ли inference.

uv run --group train python scripts/check_model.py
uv run --group train python scripts/check_model.py --alias champion --image path/to/img.jpg
"""

import argparse
from pathlib import Path

import mlflow
import mlflow.pyfunc
import pandas as pd
import yaml
from mlflow import MlflowClient

ROOT = Path(__file__).resolve().parents[1]
IMG_EXT = {".jpg", ".jpeg", ".png", ".bmp"}


def find_sample_image(data_dir: Path) -> Path:
    """Взять любую картинку из валидационной выборки."""
    for split in ("valid", "val", "test", "train"):
        folder = data_dir / split / "images"
        if folder.exists():
            for p in sorted(folder.iterdir()):
                if p.suffix.lower() in IMG_EXT:
                    return p
    raise SystemExit("[-] Не нашла ни одной картинки в датасете, укажи --image.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--alias", default="champion")
    parser.add_argument("--image", help="путь к картинке для проверки")
    args = parser.parse_args()

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    train_cfg = cfg["train"]
    name = train_cfg.get("registered_model_name", "fire-detector")

    mlflow.set_tracking_uri(cfg["mlflow"]["tracking_uri"])
    client = MlflowClient()

    mv = client.get_model_version_by_alias(name, args.alias)
    print(f"[i] models:/{name}@{args.alias} -> версия {mv.version}, run_id={mv.run_id}")

    model = mlflow.pyfunc.load_model(f"models:/{name}@{args.alias}")
    print(f"[i] pyfunc-модель загружена, run_id внутри модели: {model.metadata.run_id}")

    image = (
        Path(args.image)
        if args.image
        else find_sample_image((ROOT / train_cfg["data_dir"]).resolve())
    )
    result = model.predict(pd.DataFrame({"image_path": [str(image)]}))
    print(f"[i] inference на {image.name}: найдено объектов — {len(result)}")
    print(result.to_string(index=False))


if __name__ == "__main__":
    main()
