import zipfile
from pathlib import Path

import yaml


def load_config(config_path: str = "config.yaml") -> dict:
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def unpack_zip_dataset():
    cfg = load_config()["dataset"]
    target_dir = Path(cfg["raw_dir"])

    # Ищем любой .zip файл в корне проекта или в папке data/
    zip_files = list(Path(".").glob("*.zip")) + list(Path("data").glob("*.zip"))

    if not zip_files:
        print("[-] `.zip` файл не найден! Положите скачанный архив в корень или `data/`.")
        return

    zip_path = zip_files[0]
    print(f"[+] Найден архив: {zip_path}")
    print(f"[+] Распаковка в {target_dir.absolute()}...")

    target_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as zip_ref:
        zip_ref.extractall(target_dir)

    print(f"[+] Датасет успешно распакован в {target_dir.absolute()}!")


if __name__ == "__main__":
    unpack_zip_dataset()
