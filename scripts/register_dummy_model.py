"""Регистрация фиктивной (dummy) модели в MLflow для тестов наблюдаемости.

Проблема: клиент 2.22.5 в `log_model` ходит в новый эндпоинт
`/api/2.0/mlflow/logged-models`, которого нет в сервере 2.13.0 → 404.

Решение: НЕ вызываем `log_model`. Собираем модель из старых стабильных
эндпоинтов, которые есть в любой версии 2.x:
  1. save_model               — сохраняет модель ЛОКАЛЬНО (без HTTP);
  2. log_artifacts            — загружает артефакты в run (старый эндпоинт);
  3. create_model_version     — создаёт версию из `runs:/<run_id>/model`;
  4. set_registered_model_alias — назначает псевдоним `champion`.

Контракт модели: возвращаем `pandas.DataFrame`, потому что
`src/worker/inference.py` вызывает `df.to_dict("records")`.
"""
import os
import sys
import tempfile
from pathlib import Path

# Страховка на случай, если где-то всё же дёрнется новый API (уже не критично,
# так как мы больше не вызываем log_model).
os.environ["MLFLOW_ENABLE_CLIENT_LOGGED_MODELS"] = "false"
os.environ["MLFLOW_ENABLE_LOGGED_MODELS"] = "false"

import mlflow
import mlflow.pyfunc
import pandas as pd


class DummyFireDetector(mlflow.pyfunc.PythonModel):
    """Фиктивная модель.

    Возвращает `pandas.DataFrame` — это ровно то, что ожидает
    `src/worker/inference.py` (он делает `df.to_dict("records")`).
    Вход игнорируем: модель-заглушка не читает картинку.
    """

    def predict(self, context, model_input):
        import time

        time.sleep(0.5)  # имитация длительности inference
        return pd.DataFrame(
            [
                {"class": "fire", "confidence": 0.95, "bbox": [0.1, 0.1, 0.9, 0.9]},
                {"class": "smoke", "confidence": 0.85, "bbox": [0.2, 0.2, 0.8, 0.8]},
            ]
        )


def main() -> None:
    # Перекодировка вывода для Windows (иначе возможны кракозябры / UnicodeEncodeError).
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")

    mlflow.set_tracking_uri(os.getenv("MLFLOW_TRACKING_URI", "http://localhost:5000"))
    mlflow.set_experiment("fire_detection_dev")

    client = mlflow.tracking.MlflowClient()
    model_name = "fire-detector"

    print(f"Регистрируем фиктивную модель '{model_name}' (DataFrame-контракт)...")

    # Шаг 1: сохраняем модель ЛОКАЛЬНО во временную папку. Без HTTP.
    with tempfile.TemporaryDirectory() as tmp_dir:
        local_model_dir = str(Path(tmp_dir) / "model")
        print("-> Сохраняем модель локально (save_model, без HTTP)...")
        mlflow.pyfunc.save_model(
            path=local_model_dir,
            python_model=DummyFireDetector(),
        )
        print(f"[OK] Модель сохранена в {local_model_dir}")

        # Шаг 2: создаём run и загружаем артефакты через СТАРЫЙ эндпоинт.
        with mlflow.start_run(run_name="dummy_model_for_observability") as run:
            run_id = run.info.run_id
            print(f"-> Начат run: {run_id}")

            print("-> Загружаем артефакты в run (log_artifacts, старый эндпоинт)...")
            mlflow.log_artifacts(local_model_dir, artifact_path="model")
            print("[OK] Артефакты загружены в run")

            # Шаг 3: реестр моделей (старый стабильный API).
            try:
                client.get_registered_model(model_name)
                print(f"-> Модель '{model_name}' уже есть в реестре.")
            except mlflow.exceptions.MlflowException:
                client.create_registered_model(model_name)
                print(f"-> Создана запись в реестре: '{model_name}'")

            # Шаг 4: создаём версию из артефактов run'а.
            source = f"runs:/{run_id}/model"
            print(f"-> Создаём версию модели из {source}...")
            mv = client.create_model_version(
                name=model_name,
                source=source,
                run_id=run_id,
            )
            print(f"[OK] Создана версия модели: {mv.version}")

            # Шаг 5: назначаем псевдоним 'champion'.
            print(f"-> Назначаем alias 'champion' для версии {mv.version}...")
            client.set_registered_model_alias(
                name=model_name, alias="champion", version=mv.version
            )
            print(f"Готово! Alias 'champion' указывает на версию {mv.version}.")


if __name__ == "__main__":
    main()