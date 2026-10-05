import argparse
from pathlib import Path

import mlflow
import yaml
from mlflow import MlflowClient

ROOT = Path(__file__).resolve().parents[1]
PRIMARY_METRIC = "val_map50"


def best_run_id(experiment_name: str) -> str:
    """Найти завершённый run с максимальной основной метрикой."""
    df = mlflow.search_runs(
        experiment_names=[experiment_name],
        filter_string="attributes.status = 'FINISHED'",
        order_by=[f"metrics.{PRIMARY_METRIC} DESC"],
        max_results=1,
    )
    if df.empty:
        raise SystemExit(f"[-] В эксперименте '{experiment_name}' нет завершённых Runs.")
    return str(df.loc[0, "run_id"])


def print_registry(client: MlflowClient, name: str) -> None:
    """Показать версии модели, их раны и алиасы."""
    versions = sorted(client.search_model_versions(f"name='{name}'"), key=lambda v: int(v.version))
    print(f"\n=== Модель '{name}' ===")
    for v in versions:
        aliases = ", ".join(v.aliases) if v.aliases else "-"
        score = v.tags.get(PRIMARY_METRIC, "?")
        print(f"  v{v.version}  run_id={v.run_id}  {PRIMARY_METRIC}={score}  aliases=[{aliases}]")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", help="какой run регистрировать (по умолчанию — лучший)")
    parser.add_argument("--version", type=int, help="существующая версия (только для смены alias)")
    parser.add_argument("--alias", help="alias, например champion")
    parser.add_argument("--show", action="store_true", help="только показать реестр")
    args = parser.parse_args()

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    train_cfg = cfg["train"]
    name = train_cfg.get("registered_model_name", "fire-detector")

    mlflow.set_tracking_uri(cfg["mlflow"]["tracking_uri"])
    client = MlflowClient()

    # 1. просто показать
    if args.show:
        print_registry(client, name)
        return

    # 2. переключить alias на существующую версию
    if args.version is not None:
        if not args.alias:
            raise SystemExit("[-] Для --version нужен --alias.")
        client.set_registered_model_alias(name, args.alias, str(args.version))
        print(f"[+] Alias '{args.alias}' -> версия {args.version}")
        print_registry(client, name)
        return

    # 3. зарегистрировать новую версию из Run
    run_id = args.run_id or best_run_id(train_cfg["experiment_name"])
    mv = mlflow.register_model(model_uri=f"runs:/{run_id}/model", name=name)
    print(f"[+] Зарегистрирована версия {mv.version} модели '{name}' (run_id={run_id})")

    # связь версии с run и метрикой чтобы видно было в UI
    run = client.get_run(run_id)
    score = run.data.metrics.get(PRIMARY_METRIC)
    if score is not None:
        client.set_model_version_tag(name, mv.version, PRIMARY_METRIC, f"{score:.4f}")
    client.update_model_version(
        name,
        mv.version,
        description=f"Run '{run.info.run_name}'. Основная метрика {PRIMARY_METRIC}={score}",
    )

    if args.alias:
        client.set_registered_model_alias(name, args.alias, mv.version)
        print(f"[+] Alias '{args.alias}' -> версия {mv.version}")

    print_registry(client, name)


if __name__ == "__main__":
    main()
