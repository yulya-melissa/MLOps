import traceback
from pathlib import Path

import matplotlib.pyplot as plt
import mlflow
import mlflow.data
import numpy as np
import pandas as pd
import seaborn as sns
import yaml


def load_config(config_path: str = "config.yaml") -> dict:
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def get_class_mapping(root_dir: Path) -> dict:
    """Автоматически считывает названия классов из файла data.yaml."""
    yaml_files = list(root_dir.rglob("data.yaml")) + list(root_dir.rglob("*.yaml"))

    if yaml_files:
        try:
            with open(yaml_files[0], encoding="utf-8") as f:
                data = yaml.safe_load(f)
                names = data.get("names", {})
                if isinstance(names, list):
                    return {i: name for i, name in enumerate(names)}
                elif isinstance(names, dict):
                    return {int(k): v for k, v in names.items()}
        except Exception as e:
            print(f"[!] Ошибка чтения {yaml_files[0]}: {e}")

    # Запасной вариант, если YAML не найден
    return {0: "flame", 1: "smoke", 2: "fire"}


def parse_yolo_labels(root_dir: Path) -> pd.DataFrame:
    """Парсит файлы разметки YOLO и строит DataFrame."""
    class_mapping = get_class_mapping(root_dir)
    print(f"[+] Определены классы датасета: {class_mapping}")

    records = []
    txt_files = [
        f
        for f in root_dir.rglob("*.txt")
        if f.name not in ["data.yaml", "README.txt", "classes.txt", "LICENSE.txt"]
    ]

    if not txt_files:
        raise FileNotFoundError(f"Файлы разметки .txt не найдены в {root_dir}")

    print(f"[+] Обработка {len(txt_files)} файлов разметки...")

    for txt_file in txt_files:
        with open(txt_file, encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()

            valid_lines_found = False
            for line in lines:
                line_str = line.strip()
                if not line_str or line_str.startswith("#"):
                    continue

                parts = line_str.split()
                if len(parts) >= 5:
                    try:
                        class_id = int(parts[0])
                        x_c, y_c = float(parts[1]), float(parts[2])
                        w, h = float(parts[3]), float(parts[4])

                        # Фильтрация только валидных относительных координат YOLO (0..1)
                        if (
                            0.0 <= w <= 1.0
                            and 0.0 <= h <= 1.0
                            and 0.0 <= x_c <= 1.0
                            and 0.0 <= y_c <= 1.0
                        ):
                            records.append(
                                {
                                    "image_name": txt_file.stem,
                                    "class_id": class_id,
                                    "class_name": class_mapping.get(class_id, f"class_{class_id}"),
                                    "bbox_x_center": x_c,
                                    "bbox_y_center": y_c,
                                    "bbox_width": w,
                                    "bbox_height": h,
                                    "bbox_area": w * h,
                                    "aspect_ratio": (w / h) if h > 0 else np.nan,
                                }
                            )
                            valid_lines_found = True
                    except ValueError:
                        continue

            if not valid_lines_found:
                records.append(
                    {
                        "image_name": txt_file.stem,
                        "class_id": -1,
                        "class_name": "background",
                        "bbox_x_center": np.nan,
                        "bbox_y_center": np.nan,
                        "bbox_width": 0.0,
                        "bbox_height": 0.0,
                        "bbox_area": 0.0,
                        "aspect_ratio": np.nan,
                    }
                )

    return pd.DataFrame(records)


def _annotate_bars(ax):
    """Добавляет числовые подписи над столбцами barplot."""
    for p in ax.patches:
        height = p.get_height()
        if height > 0:
            ax.annotate(
                f"{int(height)}",
                (p.get_x() + p.get_width() / 2.0, height),
                ha="center",
                va="bottom",
                xytext=(0, 3),
                textcoords="offset points",
                fontsize=9,
            )


def run_eda_and_tracking():
    cfg = load_config()
    ds_cfg = cfg["dataset"]
    ml_cfg = cfg["mlflow"]

    mlflow.set_tracking_uri(ml_cfg["tracking_uri"])
    mlflow.set_experiment(ml_cfg["experiment_name"])

    dataset_path = Path(ds_cfg["raw_dir"])

    print("[+] Анализ разметки датасета...")
    df = parse_yolo_labels(dataset_path)

    print(f"[+] Запуск MLflow Run в эксперименте '{ml_cfg['experiment_name']}'...")

    with mlflow.start_run(run_name=ml_cfg["run_name"]):
        try:
            # --- 1. DATASET TRACKING ---
            summary_csv = Path(ds_cfg["summary_csv"])
            summary_csv.parent.mkdir(parents=True, exist_ok=True)
            df.to_csv(summary_csv, index=False)

            try:
                dataset_obj = mlflow.data.from_pandas(
                    df,
                    source=dataset_path.resolve().as_uri(),
                    name="fasdd_fire_smoke",
                    targets="class_name",
                )
                mlflow.log_input(dataset_obj, context="eda_and_preprocessing")
                print("[+] Датасет зарегистрирован в MLflow (Digest сохранен).")
            except Exception as ds_err:
                print(f"[!] Предупреждение: log_input пропущен ({ds_err})")

            # --- 2. МЕТРИКИ ---
            objects_df = df[df["class_id"] != -1].copy()
            all_images = df["image_name"].unique()
            total_images = len(all_images)
            total_objects = len(objects_df)

            # Изображения по классам
            imgs_with_fire = objects_df.loc[
                objects_df["class_name"] == "fire", "image_name"
            ].nunique()
            imgs_with_smoke = objects_df.loc[
                objects_df["class_name"] == "smoke", "image_name"
            ].nunique()

            # fire И smoke одновременно на одной картинке
            per_img_classes = objects_df.groupby("image_name")["class_name"].apply(set)
            imgs_with_both = per_img_classes.apply(lambda s: {"fire", "smoke"}.issubset(s)).sum()
            imgs_with_any = len(per_img_classes)
            imgs_background_only = total_images - imgs_with_any

            # Средние значения
            objects_per_img = objects_df.groupby("image_name").size()
            mean_objects_per_image = float(objects_per_img.mean()) if len(objects_per_img) else 0.0
            mean_bbox_area = float(objects_df["bbox_area"].mean()) if total_objects else 0.0
            mean_aspect_ratio = (
                float(objects_df["aspect_ratio"].dropna().mean()) if total_objects else 0.0
            )
            num_classes_present = int(objects_df["class_name"].nunique())

            # Логируем в MLflow
            mlflow.log_param("total_images_analyzed", total_images)
            mlflow.log_param("num_classes_present", num_classes_present)

            mlflow.log_metric("total_bounding_boxes", total_objects)
            mlflow.log_metric("images_with_fire", int(imgs_with_fire))
            mlflow.log_metric("images_with_smoke", int(imgs_with_smoke))
            mlflow.log_metric("images_with_both_fire_and_smoke", int(imgs_with_both))
            mlflow.log_metric("images_background_only", int(imgs_background_only))
            mlflow.log_metric("mean_objects_per_image", mean_objects_per_image)
            mlflow.log_metric("mean_bbox_area", mean_bbox_area)
            mlflow.log_metric("mean_aspect_ratio", mean_aspect_ratio)

            print(
                f"[+] Метрики: images={total_images}, bbox={total_objects}, "
                f"fire_imgs={imgs_with_fire}, smoke_imgs={imgs_with_smoke}, "
                f"bg_only={imgs_background_only}"
            )

            # ============================================================
            # ГРАФИК 1: Распределение объектов по классам
            # ============================================================
            fig1, ax1 = plt.subplots(figsize=(8, 5))
            class_counts = objects_df["class_name"].value_counts().reset_index()
            class_counts.columns = ["class_name", "count"]

            sns.barplot(
                data=class_counts,
                x="class_name",
                y="count",
                hue="class_name",
                palette="viridis",
                legend=False,
                ax=ax1,
            )
            ax1.set_title("Распределение Bounding Boxes по классам", fontsize=13)
            ax1.set_xlabel("Класс", fontsize=11)
            ax1.set_ylabel("Количество Bounding Boxes", fontsize=11)
            _annotate_bars(ax1)
            plt.tight_layout()
            mlflow.log_figure(fig1, "eda_plots/class_distribution.png")
            plt.close(fig1)

            # ============================================================
            # ГРАФИК 2: Количество изображений на класс
            # ============================================================
            fig2, ax2 = plt.subplots(figsize=(8, 5))
            imgs_per_class = (
                objects_df.groupby("class_name")["image_name"]
                .nunique()
                .reset_index(name="n_images")
                .sort_values("n_images", ascending=False)
            )
            sns.barplot(
                data=imgs_per_class,
                x="class_name",
                y="n_images",
                hue="class_name",
                palette="magma",
                legend=False,
                ax=ax2,
            )
            ax2.set_title("Количество изображений по классам", fontsize=13)
            ax2.set_xlabel("Класс", fontsize=11)
            ax2.set_ylabel("Количество изображений", fontsize=11)
            _annotate_bars(ax2)
            plt.tight_layout()
            mlflow.log_figure(fig2, "eda_plots/images_per_class.png")
            plt.close(fig2)

            # ============================================================
            # ГРАФИК 3: Размеры Bounding Box
            # ============================================================
            fig3, ax3 = plt.subplots(figsize=(8, 6))
            plot_samples = (
                objects_df.sample(n=min(10000, len(objects_df)), random_state=42)
                if len(objects_df) > 10000
                else objects_df
            )
            sns.scatterplot(
                data=plot_samples,
                x="bbox_width",
                y="bbox_height",
                hue="class_name",
                alpha=0.4,
                s=15,
                palette="Set2",
                ax=ax3,
            )
            ax3.set_title(
                f"Размеры Bounding Box (Выборка {len(plot_samples)} из {len(objects_df)})",
                fontsize=13,
            )
            ax3.set_xlabel("Относительная ширина", fontsize=11)
            ax3.set_ylabel("Относительная высота", fontsize=11)
            ax3.set_xlim(0, 1)
            ax3.set_ylim(0, 1)
            plt.tight_layout()
            mlflow.log_figure(fig3, "eda_plots/bbox_sizes.png")
            plt.close(fig3)

            # ============================================================
            # ГРАФИК 4: Heatmap позиций bbox (где в кадре находится объект)
            # ============================================================
            fig4, ax4 = plt.subplots(figsize=(7, 6))
            pos_df = objects_df.dropna(subset=["bbox_x_center", "bbox_y_center"])
            if len(pos_df) > 0:
                hb = ax4.hexbin(
                    pos_df["bbox_x_center"],
                    pos_df["bbox_y_center"],
                    gridsize=30,
                    cmap="Reds",
                    mincnt=1,
                    extent=(0, 1, 0, 1),
                )
                fig4.colorbar(hb, ax=ax4, label="Количество объектов")
                ax4.invert_yaxis()  # как в изображении: y растёт вниз
            ax4.set_title("Позиция центров Bounding Box в кадре", fontsize=13)
            ax4.set_xlabel("Относительный X центра", fontsize=11)
            ax4.set_ylabel("Относительный Y центра", fontsize=11)
            ax4.set_xlim(0, 1)
            ax4.set_ylim(0, 1)
            plt.tight_layout()
            mlflow.log_figure(fig4, "eda_plots/bbox_position_heatmap.png")
            plt.close(fig4)

            # ============================================================
            # ГРАФИК 5: Количество bbox на изображение
            # ============================================================
            fig5, ax5 = plt.subplots(figsize=(8, 5))
            if len(objects_per_img) > 0:
                sns.histplot(
                    objects_per_img.values,
                    bins=min(50, max(10, objects_per_img.max())),
                    kde=False,
                    color="steelblue",
                    ax=ax5,
                )
                ax5.axvline(
                    objects_per_img.mean(),
                    color="red",
                    linestyle="--",
                    label=f"Среднее = {objects_per_img.mean():.2f}",
                )
                ax5.legend()
            ax5.set_title("Распределение количества BBox на изображение", fontsize=13)
            ax5.set_xlabel("Количество BBox", fontsize=11)
            ax5.set_ylabel("Количество изображений", fontsize=11)
            plt.tight_layout()
            mlflow.log_figure(fig5, "eda_plots/objects_per_image.png")
            plt.close(fig5)

            # ============================================================
            # ГРАФИК 6: Aspect ratio по классам
            # ============================================================
            fig6, ax6 = plt.subplots(figsize=(8, 5))
            ar_df = objects_df.dropna(subset=["aspect_ratio"])
            ar_df = ar_df[ar_df["aspect_ratio"].between(0, 10)]  # обрезаем выбросы
            if len(ar_df) > 0:
                sns.histplot(
                    data=ar_df,
                    x="aspect_ratio",
                    hue="class_name",
                    bins=50,
                    element="step",
                    stat="density",
                    common_norm=False,
                    ax=ax6,
                )
                ax6.axvline(1.0, color="gray", linestyle=":", label="квадрат (1.0)")
                ax6.legend()
            ax6.set_title("Распределение Aspect Ratio (W/H) по классам", fontsize=13)
            ax6.set_xlabel("Aspect Ratio (W/H)", fontsize=11)
            ax6.set_ylabel("Плотность", fontsize=11)
            plt.tight_layout()
            mlflow.log_figure(fig6, "eda_plots/aspect_ratio_dist.png")
            plt.close(fig6)

            # ============================================================
            # ГРАФИК 7: Co-occurrence классов на изображениях
            # ============================================================
            fig7, ax7 = plt.subplots(figsize=(6, 5))
            present = objects_df.assign(present=1).pivot_table(
                index="image_name",
                columns="class_name",
                values="present",
                aggfunc="max",
                fill_value=0,
            )
            if present.shape[1] > 1:
                cooc = present.T.astype(int) @ present.astype(int)
                sns.heatmap(
                    cooc,
                    annot=True,
                    fmt="d",
                    cmap="Blues",
                    cbar_kws={"label": "Количество изображений"},
                    ax=ax7,
                )
            else:
                ax7.text(
                    0.5,
                    0.5,
                    "Недостаточно классов для co-occurrence",
                    ha="center",
                    va="center",
                    transform=ax7.transAxes,
                )
            ax7.set_title("Co-occurrence: классы на одном изображении", fontsize=13)
            plt.tight_layout()
            mlflow.log_figure(fig7, "eda_plots/cooccurrence_heatmap.png")
            plt.close(fig7)

            # --- Отчёт ---
            mlflow.log_artifact(str(summary_csv), artifact_path="eda_reports")
            print("[+] EDA графики и отчёт успешно сохранены в MLflow!")

        except Exception as e:
            print(f"[-] Ошибка во время выполнения MLflow run: {e}")
            traceback.print_exc()


if __name__ == "__main__":
    run_eda_and_tracking()
