"""Залить готовые веса YOLO в MLflow как pyfunc-модель (без обучения).

Нужно, когда обучение делал кто-то другой / в Colab, а в вашем MLflow runs нет.
После этого `register_model.py` найдёт этот run и зарегистрирует модель.

    uv run --group train python scripts/log_weights.py --weights yolov8n.pt
    uv run --group train python scripts/log_weights.py --weights best.pt --val-map50 0.62
"""

import argparse
import sys
from pathlib import Path

import mlflow
import mlflow.pyfunc
import ultralytics
import yaml

SCRIPTS_DIR = Path(__file__).resolve().parent
ROOT = SCRIPTS_DIR.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from fire_model import FireYoloModel, get_signature  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--weights", required=True, help="путь к .pt (например best.pt)")
    parser.add_argument("--run-name", default="imported_weights")
    parser.add_argument("--imgsz", type=int, default=320)
    parser.add_argument(
        "--val-map50",
        type=float,
        default=0.0,
        help="если известна — по ней register_model.py выбирает лучший run",
    )
    args = parser.parse_args()

    weights = Path(args.weights).resolve()
    if not weights.exists():
        raise SystemExit(f"[-] Файл весов не найден: {weights}")

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    mlflow.set_tracking_uri(cfg["mlflow"]["tracking_uri"])
    mlflow.set_experiment(cfg["train"]["experiment_name"])

    with mlflow.start_run(run_name=args.run_name) as run:
        mlflow.set_tags(
            {"variant": "imported", "task": "fire_detection", "framework": "ultralytics"}
        )
        mlflow.log_param("weights", weights.name)
        mlflow.log_param("imgsz", args.imgsz)
        mlflow.log_metric("val_map50", args.val_map50)
        mlflow.pyfunc.log_model(
            artifact_path="model",
            python_model=FireYoloModel(imgsz=args.imgsz),
            artifacts={"weights": str(weights)},
            code_paths=[str(SCRIPTS_DIR / "fire_model.py")],
            signature=get_signature(),
            pip_requirements=[f"ultralytics=={ultralytics.__version__}"],
        )
        print(f"[+] Run {run.info.run_id} создан, модель залита в MLflow.")


if __name__ == "__main__":
    main()
