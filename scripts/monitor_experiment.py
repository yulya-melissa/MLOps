import argparse
import base64
import csv
import json
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen


def to_bytes(value: str) -> int:
    match = re.search(r"([\d.]+)\s*(B|KiB|MiB|GiB|KB|MB|GB)", value)
    if not match:
        return 0

    number = float(match.group(1))
    unit = match.group(2)
    factors = {
        "B": 1,
        "KiB": 1024,
        "MiB": 1024**2,
        "GiB": 1024**3,
        "KB": 1000,
        "MB": 1000**2,
        "GB": 1000**3,
    }
    return int(number * factors[unit])


def get_queue(queue_name: str) -> dict:
    encoded_name = "".join(
        f"%{byte:02X}" if byte == 47 else chr(byte) for byte in queue_name.encode("utf-8")
    )
    url = f"http://localhost:15672/api/queues/%2F/{encoded_name}"
    token = base64.b64encode(b"fire:fire_password").decode("ascii")
    request = Request(url, headers={"Authorization": f"Basic {token}"})

    with urlopen(request, timeout=5) as response:
        return json.loads(response.read())


def get_container_memory() -> tuple[int, int]:
    result = subprocess.run(
        [
            "docker",
            "stats",
            "--no-stream",
            "--format",
            "{{.Name}}\t{{.MemUsage}}",
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    rabbit_memory = 0
    worker_memory = 0

    for line in result.stdout.splitlines():
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue

        name, usage = parts
        used = usage.split("/", 1)[0].strip()
        memory = to_bytes(used)

        if "rabbitmq" in name.lower():
            rabbit_memory += memory
        if "worker" in name.lower():
            worker_memory += memory

    return rabbit_memory, worker_memory


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--queue", default="fire.tasks")
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--duration", type=int, default=240)
    parser.add_argument("--interval", type=float, default=2)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    fields = [
        "timestamp",
        "scenario",
        "queue",
        "queue_ready",
        "queue_unacked",
        "queue_total",
        "queue_memory_bytes",
        "rabbitmq_memory_bytes",
        "worker_memory_bytes",
        "publish_rate_msg_s",
    ]

    print(f"Сбор данных: {args.scenario}, очередь {args.queue}")
    print(f"CSV: {output.resolve()}")
    print(f"Длительность: {args.duration} сек.")

    end_time = time.monotonic() + args.duration

    with output.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()

        while time.monotonic() < end_time:
            try:
                queue = get_queue(args.queue)
                rabbit_memory, worker_memory = get_container_memory()
                stats = queue.get("message_stats", {})

                row = {
                    "timestamp": datetime.now().astimezone().isoformat(),
                    "scenario": args.scenario,
                    "queue": args.queue,
                    "queue_ready": queue.get("messages_ready", 0),
                    "queue_unacked": queue.get("messages_unacknowledged", 0),
                    "queue_total": queue.get("messages", 0),
                    "queue_memory_bytes": queue.get("memory", 0),
                    "rabbitmq_memory_bytes": rabbit_memory,
                    "worker_memory_bytes": worker_memory,
                    "publish_rate_msg_s": stats.get("publish_details", {}).get("rate", 0),
                }
                writer.writerow(row)
                file.flush()

                print(
                    f"{row['timestamp']} | "
                    f"queue={row['queue_total']} "
                    f"(ready={row['queue_ready']}, "
                    f"unacked={row['queue_unacked']}) | "
                    f"worker_mem={worker_memory / 1024**2:.1f} MiB | "
                    f"publish_rate={row['publish_rate_msg_s']:.2f}/s"
                )

            except Exception as error:
                print(f"Ошибка измерения: {error}")

            time.sleep(args.interval)

    print(f"Измерения сохранены: {output.resolve()}")


if __name__ == "__main__":
    main()
