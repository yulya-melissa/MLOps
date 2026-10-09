import argparse
from pathlib import Path

import mlflow
import yaml
from mlflow import MlflowClient

ROOT = Path(__file__).resolve().parents[1]
PRIMARY_METRIC = "val_map50"


def best_run_id(experiment_name: str) -> str:
    """Найти завершенный run с максимальной основной метрикой."""
    df = mlflow.search_runs(
        experiment_names=[experiment_name],
        filter_string="attributes.status = 'FINISHED'",
        order_by=[f"metrics.{PRIMARY_METRIC} DESC"],
        max_results=1,
    )
    if df.empty:
        raise SystemExit(f"[-] В эксперименте '{experiment_name}' нет завершенных Runs.")
    return str(df.loc[0, "run_id"])


def print_registry(name: str) -> None:
    """Показать свежие версии модели, их раны и алиасы."""
    client = MlflowClient()
    try:
        alias_map = {}
        model = client.get_registered_model(name)

        for alias, version in model.aliases.items():
            ver = str(version)
            alias_map.setdefault(ver, []).append(alias)

    except Exception:
        alias_map = {}

    versions = sorted(
        client.search_model_versions(f"name='{name}'"),
        key=lambda version: int(version.version),
    )
    print(f"\n=== Модель '{name}' ===")

    if not versions:
        print("  (версий не найдено)")
        return

    for version in versions:
        # Берём алиасы из модели или из самой версии.
        version_aliases = alias_map.get(str(version.version), [])
        if not version_aliases and version.aliases:
            version_aliases = list(version.aliases)

        aliases_str = ", ".join(version_aliases) if version_aliases else "-"
        score = version.tags.get(PRIMARY_METRIC, "?")

        print(
            f"  v{version.version}  run_id={version.run_id}  "
            f"{PRIMARY_METRIC}={score}  aliases=[{aliases_str}]"
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Управление версиями и alias в MLflow Model Registry"
    )
    parser.add_argument(
        "--run-id",
        help="какой run регистрировать (по умолчанию - лучший)",
    )
    parser.add_argument(
        "--version",
        type=int,
        help="существующая версия (для переключения alias)",
    )
    parser.add_argument(
        "--alias",
        help="alias, например champion",
    )
    parser.add_argument(
        "--show",
        action="store_true",
        help="только показать реестр",
    )
    args = parser.parse_args()

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    train_cfg = cfg["train"]
    name = train_cfg.get("registered_model_name", "fire-detector")

    mlflow.set_tracking_uri(cfg["mlflow"]["tracking_uri"])

    # 1. Просто показать текущее состояние.
    if args.show:
        print_registry(name)
        return

    client = MlflowClient()

    # 2. Переключить alias на уже существующую версию.
    if args.version is not None:
        if not args.alias:
            raise SystemExit(
                "[-] Для установки alias через --version укажите --alias "
                "(например, --alias champion)."
            )

        client.set_registered_model_alias(
            name,
            args.alias,
            str(args.version),
        )
        print(f"[+] Alias '{args.alias}' успешно привязан к версии {args.version}")
        print_registry(name)
        return

    # 3. Зарегистрировать новую версию из Run.
    run_id = args.run_id or best_run_id(train_cfg["experiment_name"])
    mv = mlflow.register_model(
        model_uri=f"runs:/{run_id}/model",
        name=name,
    )
    print(f"[+] Зарегистрирована версия {mv.version} модели '{name}' (run_id={run_id})")

    # Получаем метрику из Run и привязываем к версии модели.
    run = client.get_run(run_id)
    score = run.data.metrics.get(PRIMARY_METRIC)

    if score is not None:
        client.set_model_version_tag(
            name,
            mv.version,
            PRIMARY_METRIC,
            f"{score:.4f}",
        )

    if score is not None:
        desc = f"Run '{run.info.run_name}'. Основная метрика {PRIMARY_METRIC}={score:.4f}"
    else:
        desc = f"Run '{run.info.run_name}'"

    client.update_model_version(
        name,
        mv.version,
        description=desc,
    )

    # Назначаем alias, если передан.
    if args.alias:
        client.set_registered_model_alias(
            name,
            args.alias,
            mv.version,
        )
        print(f"[+] Alias '{args.alias}' -> версия {mv.version}")

    # Показываем реестр.
    print_registry(name)


if __name__ == "__main__":
    main()
