from __future__ import annotations

import re
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "results" / "experiments"

EXPERIMENTS = {
    "Ограниченная, 1 worker": {
        "csv": "limited_1_worker_retry.csv",
        "source_logs": [
            "limited_1_worker_retry_source.log",
            "limited_1_worker_source.log",
        ],
        "worker_logs": [
            "limited_1_worker_retry_worker.log",
            "limited_1_worker_worker.log",
        ],
    },
    "Без лимита, 1 worker": {
        "csv": "unlimited_1_worker.csv",
        "source_logs": [
            "unlimited_1_worker_source.log",
        ],
        "worker_logs": [
            "unlimited_1_worker_worker.log",
        ],
    },
    "Ограниченная, 2 worker": {
        "csv": "limited_2_workers_retry.csv",
        "source_logs": [
            "limited_2_workers_retry_source.log",
            "limited_2_workers_source.log",
        ],
        "worker_logs": [
            "limited_2_workers_retry_worker.log",
            "limited_2_workers_worker.log",
        ],
    },
    "Без лимита, 2 worker": {
        "csv": "unlimited_2_workers.csv",
        "source_logs": [
            "unlimited_2_workers_source.log",
        ],
        "worker_logs": [
            "unlimited_2_workers_worker.log",
        ],
    },
}

plt.rcParams.update(
    {
        "figure.figsize": (12, 6.5),
        "figure.dpi": 120,
        "savefig.dpi": 200,
        "axes.grid": True,
        "grid.alpha": 0.3,
        "font.size": 10,
    }
)


def read_csv(path: Path) -> pd.DataFrame:
    """Читает CSV, в том числе если в нём есть нулевые байты."""
    if not path.exists():
        raise FileNotFoundError(f"Не найден файл: {path}")

    content = path.read_bytes().replace(b"\x00", b"")
    from io import StringIO

    df = pd.read_csv(StringIO(content.decode("utf-8-sig")))

    required = {
        "timestamp",
        "queue_total",
        "queue_ready",
        "queue_unacked",
        "rabbitmq_memory_bytes",
        "worker_memory_bytes",
    }
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"{path.name}: отсутствуют столбцы {sorted(missing)}")

    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    df = df.dropna(subset=["timestamp"]).sort_values("timestamp")

    numeric = required - {"timestamp"}
    for col in numeric:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    # Время относительно начала эксперимента, секунды.
    df["elapsed_s"] = (df["timestamp"] - df["timestamp"].iloc[0]).dt.total_seconds()

    return df


def find_log(candidates: list[str]) -> Path | None:
    for filename in candidates:
        path = OUT / filename
        if path.exists():
            return path
    return None


def read_source_speed(config: dict) -> float | None:
    """Берёт среднюю скорость из финальной строки лога источника."""
    path = find_log(config["source_logs"])
    if path is None:
        print("Нет лога источника:", config["source_logs"])
        return None

    text = path.read_text(encoding="utf-8", errors="replace")

    matches = re.findall(
        r"средняя скорость\s+([\d.,]+)\s*msg/s",
        text,
        flags=re.IGNORECASE,
    )
    if not matches:
        print(f"Не найдена итоговая скорость в {path.name}")
        return None

    return float(matches[-1].replace(",", "."))


def read_task_durations(config: dict) -> list[float]:
    """Извлекает длительности задач из сохранённого лога worker."""
    path = find_log(config["worker_logs"])
    if path is None:
        print("Нет лога worker:", config["worker_logs"])
        return []

    text = path.read_text(encoding="utf-8", errors="replace")

    # Поддерживает форматы took=523 ms и took=0.523 s.
    ms_values = re.findall(
        r"\btook\s*=\s*([\d.,]+)\s*ms\b",
        text,
        flags=re.IGNORECASE,
    )
    s_values = re.findall(
        r"\btook\s*=\s*([\d.,]+)\s*s\b",
        text,
        flags=re.IGNORECASE,
    )

    durations_ms = [float(value.replace(",", ".")) for value in ms_values]
    durations_ms += [float(value.replace(",", ".")) * 1000 for value in s_values]

    return durations_ms


def save_queue_plot(data: dict[str, pd.DataFrame]) -> None:
    fig, ax = plt.subplots()

    for label, df in data.items():
        ax.plot(
            df["elapsed_s"],
            df["queue_total"],
            label=label,
            linewidth=1.8,
        )

    ax.set_title("Глубина очереди во времени")
    ax.set_xlabel("Время с начала эксперимента, с")
    ax.set_ylabel("Количество сообщений")
    ax.legend()
    fig.tight_layout()
    fig.savefig(OUT / "queue_depth.png", bbox_inches="tight")
    plt.close(fig)


def save_memory_plot(data: dict[str, pd.DataFrame]) -> None:
    # Две панели в одном PNG: память RabbitMQ и worker.
    fig, (ax_rabbit, ax_worker) = plt.subplots(2, 1, figsize=(12, 9), sharex=False)

    for label, df in data.items():
        ax_rabbit.plot(
            df["elapsed_s"],
            df["rabbitmq_memory_bytes"] / (1024**2),
            label=label,
            linewidth=1.6,
        )
        ax_worker.plot(
            df["elapsed_s"],
            df["worker_memory_bytes"] / (1024**2),
            label=label,
            linewidth=1.6,
        )

    ax_rabbit.set_title("Память контейнера RabbitMQ")
    ax_rabbit.set_ylabel("Память, MiB")
    ax_rabbit.set_xlabel("Время с начала эксперимента, с")
    ax_rabbit.legend()

    ax_worker.set_title("Суммарная память worker")
    ax_worker.set_ylabel("Память, MiB")
    ax_worker.set_xlabel("Время с начала эксперимента, с")
    ax_worker.legend()

    fig.suptitle("Потребление памяти в четырёх экспериментах")
    fig.tight_layout()
    fig.savefig(OUT / "memory_usage.png", bbox_inches="tight")
    plt.close(fig)


def save_source_speed_plot() -> None:
    labels = []
    speeds = []

    for label, config in EXPERIMENTS.items():
        speed = read_source_speed(config)
        if speed is not None:
            labels.append(label)
            speeds.append(speed)

    if not speeds:
        print("График скорости не построен: нет данных из логов источника.")
        return

    fig, ax = plt.subplots()
    bars = ax.bar(labels, speeds)

    ax.bar_label(bars, fmt="%.2f")
    ax.set_title("Средняя скорость публикации задач источником")
    ax.set_ylabel("Задач в секунду, msg/s")
    ax.set_xlabel("Эксперимент")
    ax.tick_params(axis="x", labelrotation=15)

    fig.tight_layout()
    fig.savefig(OUT / "source_generation_speed.png", bbox_inches="tight")
    plt.close(fig)


def save_task_duration_plot() -> None:
    labels = []
    means = []
    medians = []

    for label, config in EXPERIMENTS.items():
        durations = read_task_durations(config)
        if durations:
            labels.append(label)
            means.append(sum(durations) / len(durations))
            medians.append(float(pd.Series(durations).median()))

    if not means:
        print(
            "График времени выполнения задач не построен: "
            "нужны сохранённые логи worker с полем took=... ms."
        )
        return

    fig, ax = plt.subplots()
    x = range(len(labels))
    width = 0.36

    bars_mean = ax.bar(
        [i - width / 2 for i in x],
        means,
        width,
        label="Среднее",
    )
    bars_median = ax.bar(
        [i + width / 2 for i in x],
        medians,
        width,
        label="Медиана",
    )

    ax.bar_label(bars_mean, fmt="%.1f", padding=2)
    ax.bar_label(bars_median, fmt="%.1f", padding=2)
    ax.set_title("Время выполнения задачи worker")
    ax.set_ylabel("Время, мс")
    ax.set_xlabel("Эксперимент")
    ax.set_xticks(list(x), labels, rotation=15)
    ax.legend()

    fig.tight_layout()
    fig.savefig(OUT / "task_execution_time.png", bbox_inches="tight")
    plt.close(fig)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)

    data = {}
    for label, config in EXPERIMENTS.items():
        path = OUT / config["csv"]
        if not path.exists():
            print(f"ПРОПУСК: не найден {path.name}")
            continue

        try:
            df = read_csv(path)
            data[label] = df
            print(f"{path.name}: загружено {len(df)} измерений")
        except (ValueError, OSError) as exc:
            print(f"ОШИБКА чтения {path.name}: {exc}")

    if not data:
        raise SystemExit("Не удалось загрузить ни одного CSV.")

    save_queue_plot(data)
    save_memory_plot(data)
    save_source_speed_plot()
    save_task_duration_plot()

    print("\nГрафики сохранены в:", OUT)
    print("  queue_depth.png")
    print("  memory_usage.png")
    print("  source_generation_speed.png")
    print("  task_execution_time.png — если доступны логи worker")


if __name__ == "__main__":
    main()
