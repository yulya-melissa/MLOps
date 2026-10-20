import os

import fiftyone as fo

os.environ["FIFTYONE_DATABASE_URI"] = "mongodb://localhost:27017"

dataset_name = "fire-smoke-dataset"

# Удаляем старый датасет, если он есть
if fo.dataset_exists(dataset_name):
    fo.delete_dataset(dataset_name)

# Создаем новый пустой датасет
dataset = fo.Dataset(dataset_name)

# Указываем АБСОЛЮТНЫЙ путь к корню датасета
root = "~/MLOps/fire-detection/data/fasdd"

# Загружаем сплиты по отдельности с явным указанием путей
for split_name in ["train", "val", "test"]:
    print(f"Загрузка сплита: {split_name}")
    dataset.add_dir(
        dataset_type=fo.types.YOLOv5Dataset,
        data_path=f"{root}/{split_name}/images",
        labels_path=f"{root}/{split_name}/labels",
        yaml_path=f"{root}/data.yaml",
        split=split_name,
        tags=[split_name],
    )

# Проверяем результат
print(f"Всего загружено изображений: {len(dataset)}")
print(dataset)

# Запускаем интерфейс
session = fo.launch_app(dataset)
session.wait()
