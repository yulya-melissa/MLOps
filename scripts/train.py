import argparse
import re
import sys
from pathlib import Path

import mlflow
import mlflow.data
import pandas as pd
import ultralytics
import yaml
from ultralytics import YOLO
from ultralytics import settings as yolo_settings

SCRIPTS_DIR = Path(__file__).resolve().parent
ROOT = SCRIPTS_DIR.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from fire_model import FireYoloModel, get_signature  # noqa: E402

# Своя интеграция с MLflow у Ultralytics создала бы лишние Runs — отключаем.
try:
    yolo_settings.update({"mlflow": False})
except Exception:
    pass

IMG_EXT = {".jpg", ".jpeg", ".png", ".bmp"}

# Диагностические файлы, которые Ultralytics кладёт в папку обучения.
DIAGNOSTICS = [
    "results.png",
    "results.csv",
    "confusion_matrix.png",
    "confusion_matrix_normalized.png",
    "PR_curve.png",
    "F1_curve.png",
    "P_curve.png",
    "R_curve.png",
    "labels.jpg",
    "val_batch0_labels.jpg",
    "val_batch0_pred.jpg",
    "args.yaml",
]


# --------------------------------------------------------------------------
# Данные
# --------------------------------------------------------------------------
def prepare_data_yaml(data_dir: Path, out_dir: Path) -> tuple[Path, list[str]]:
    """Собрать data.yaml с абсолютным путём

    Возвращает путь к новому yaml и список сплитов для оценки (val [+ test]).
    """
    src = yaml.safe_load((data_dir / "data.yaml").read_text(encoding="utf-8"))
    val_dir = "valid" if (data_dir / "valid" / "images").exists() else "val"
    fixed = {
        "path": str(data_dir),
        "train": "train/images",
        "val": f"{val_dir}/images",
        "nc": src["nc"],
        "names": src["names"],
    }
    splits = ["val"]
    if (data_dir / "test" / "images").exists():
        fixed["test"] = "test/images"
        splits.append("test")

    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "data_fixed.yaml"
    out.write_text(yaml.safe_dump(fixed, allow_unicode=True), encoding="utf-8")
    return out, splits


def build_dataset(data_dir: Path, name: str):
    """Описать датасет для MLflow (список файлов по сплитам) для связи Run <-> Dataset."""
    rows = []
    for split in ("train", "valid", "val", "test"):
        folder = data_dir / split / "images"
        if folder.exists():
            rows += [
                {"split": split, "file": p.name}
                for p in sorted(folder.iterdir())
                if p.suffix.lower() in IMG_EXT
            ]
    df = pd.DataFrame(rows)
    return mlflow.data.from_pandas(df, source=str(data_dir), name=name)


# --------------------------------------------------------------------------
# Варианты экспериментов
# --------------------------------------------------------------------------
def build_variants(train_cfg: dict, epochs: int) -> list[dict]:
    """Baseline + вариант с другим оптимизатором + вариант с другим размером входа."""
    base = {
        "epochs": epochs,
        "batch": train_cfg["batch"],
        "fraction": train_cfg["fraction"],
        "seed": train_cfg["seed"],
        "imgsz": train_cfg.get("imgsz", 320),
        # optimizer='auto' у Ultralytics игнорирует lr0, поэтому задаём явно
        "optimizer": "SGD",
        "lr0": 0.01,
    }
    return [
        {
            "run_name": "baseline_yolov8n_sgd_320",
            "variant": "baseline",
            "weights": "yolov8n.pt",
            "hp": dict(base),
        },
        {
            "run_name": "yolov8n_adamw_lr1e-3",
            "variant": "hyperparameters",
            "weights": "yolov8n.pt",
            "hp": {**base, "optimizer": "AdamW", "lr0": 0.001},
        },
        {
            "run_name": "yolov8n_sgd_416",
            "variant": "preprocessing",
            "weights": "yolov8n.pt",
            "hp": {**base, "imgsz": 416},
        },
    ]


# --------------------------------------------------------------------------
# Логирование
# --------------------------------------------------------------------------
def clean_metric_name(name: str) -> str:
    """MLflow не любит скобки в именах метрик: metrics/mAP50(B) -> metrics/mAP50."""
    name = name.strip().replace("(B)", "")
    return re.sub(r"[^A-Za-z0-9_\-\. /]", "", name)


def log_epoch_metrics(results_csv: Path) -> None:
    """Залить кривые обучения (loss, mAP по эпохам) с параметром step."""
    df = pd.read_csv(results_csv)
    df.columns = [c.strip() for c in df.columns]
    for _, row in df.iterrows():
        step = int(row["epoch"])
        metrics = {
            clean_metric_name(k): float(v) for k, v in row.items() if k != "epoch" and pd.notna(v)
        }
        mlflow.log_metrics(metrics, step=step)


def evaluate(weights: Path, data_yaml: Path, splits: list[str], imgsz: int) -> dict[str, float]:
    """Финальная оценка лучших весов на val (и test, если есть)."""
    model = YOLO(str(weights))
    out: dict[str, float] = {}
    for split in splits:
        m = model.val(
            data=str(data_yaml),
            split=split,
            imgsz=imgsz,
            batch=8,
            device="cpu",
            workers=0,
            plots=False,
            verbose=False,
        )
        p, r = float(m.box.mp), float(m.box.mr)
        out[f"{split}_map50"] = float(m.box.map50)  # ОСНОВНАЯ метрика
        out[f"{split}_map50_95"] = float(m.box.map)
        out[f"{split}_precision"] = p
        out[f"{split}_recall"] = r
        out[f"{split}_f1"] = 2 * p * r / (p + r) if (p + r) else 0.0
        out[f"{split}_inference_ms"] = float(m.speed["inference"])
    return out


def run_experiment(spec: dict, data_yaml: Path, splits: list[str], dataset, work_dir: Path):
    """Один Run: параметры -> обучение -> метрики -> артефакты -> модель."""
    hp = spec["hp"]
    with mlflow.start_run(run_name=spec["run_name"]) as run:
        mlflow.set_tags(
            {
                "variant": spec["variant"],
                "task": "fire_detection",
                "framework": "ultralytics",
                "dataset.name": dataset.name if dataset is not None else "unknown",
            }
        )
        mlflow.log_param("weights", spec["weights"])
        mlflow.log_params(hp)
        if dataset is not None:
            mlflow.log_input(dataset, context="training")

        # --- обучение (CPU, workers=0 — безопасно на Windows) ---
        model = YOLO(spec["weights"])
        model.train(
            data=str(data_yaml),
            device="cpu",
            workers=0,
            project=str(work_dir),
            name=spec["run_name"],
            exist_ok=True,
            plots=True,
            verbose=False,
            **hp,
        )
        save_dir = Path(model.trainer.save_dir)
        best = save_dir / "weights" / "best.pt"

        # --- метрики ---
        log_epoch_metrics(save_dir / "results.csv")
        final = evaluate(best, data_yaml, splits, hp["imgsz"])
        mlflow.log_metrics(final)

        # --- артефакты ---
        for fname in DIAGNOSTICS:
            f = save_dir / fname
            if f.exists():
                mlflow.log_artifact(str(f), artifact_path="diagnostics")
        mlflow.log_artifact(str(data_yaml), artifact_path="data")

        # --- модель (pyfunc) с весами, сигнатурой и кодом обёртки ---
        mlflow.pyfunc.log_model(
            artifact_path="model",
            python_model=FireYoloModel(imgsz=hp["imgsz"]),
            artifacts={"weights": str(best)},
            code_paths=[str(SCRIPTS_DIR / "fire_model.py")],
            signature=get_signature(),
            pip_requirements=[f"ultralytics=={ultralytics.__version__}"],
        )

        print(
            f"[+] {spec['run_name']}: val_map50={final['val_map50']:.4f} run_id={run.info.run_id}"
        )
        return run.info.run_id


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--epochs", type=int, help="переопределить число эпох из config.yaml")
    parser.add_argument("--only", help="запустить только Runs, в имени которых есть эта строка")
    args = parser.parse_args()

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    train_cfg = cfg["train"]

    mlflow.set_tracking_uri(cfg["mlflow"]["tracking_uri"])
    mlflow.set_experiment(train_cfg["experiment_name"])

    data_dir = (ROOT / train_cfg["data_dir"]).resolve()
    work_dir = ROOT / "runs"
    data_yaml, splits = prepare_data_yaml(data_dir, work_dir)

    try:
        dataset = build_dataset(
            data_dir, train_cfg.get("dataset_name", "fire-detection-roboflow-v4")
        )
    except Exception as exc:
        print(f"[!] Не удалось описать датасет для MLflow: {exc}")
        dataset = None

    variants = build_variants(train_cfg, args.epochs or train_cfg["epochs"])
    if args.only:
        variants = [v for v in variants if args.only in v["run_name"]]

    for spec in variants:
        run_experiment(spec, data_yaml, splits, dataset, work_dir)

    # итоговая таблица
    df = mlflow.search_runs(
        experiment_names=[train_cfg["experiment_name"]],
        order_by=["metrics.val_map50 DESC"],
    )
    cols = [
        c
        for c in (
            "tags.mlflow.runName",
            "run_id",
            "metrics.val_map50",
            "metrics.val_recall",
            "metrics.val_inference_ms",
        )
        if c in df.columns
    ]
    print("\n=== Лидерборд ===")
    print(df[cols].to_string(index=False))


if __name__ == "__main__":
    main()
