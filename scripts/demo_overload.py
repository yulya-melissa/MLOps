"""Демонстрация работы границы системы при перегрузке (п. 4.1 и 4.2).

Инструкция к запуску на защите:
1. Оставить запущенными только API, RabbitMQ и Postgres:
   docker compose stop worker
2. Запустить данный скрипт:
   python scripts/demo_overload.py
"""

import asyncio
import io
import logging
from http import HTTPStatus

import httpx

API_URL = "http://localhost:8000/api/v1/tasks"
DB_DSN = "postgresql://postgres:postgres_password@localhost:5432/fire_detection"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("demo")


def create_dummy_image() -> bytes:
    """Генерация минимального валидного JPEG для отправки."""
    return (
        b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00"
        b"\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t"
        b"\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a"
        b"\x1f\x1e\x1d\x1a\x1c\x1c $.' \",#\x1c\x1c(7),01444\x1f'9=82<.342"
        b"\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00\xff\xc4"
        b"\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00"
        b"\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b"
        b"\xff\xda\x00\x08\x01\x01\x00\x00?\x00\xbf\x00\xff\xd9"
    )


async def check_db_for_task(task_id: str) -> bool:
    """Проверка присутствия task_id в таблице tasks через asyncpg."""
    import asyncpg

    conn = await asyncpg.connect(DB_DSN)
    try:
        row = await conn.fetchrow("SELECT status FROM tasks WHERE task_id = $1", task_id)
        return row is not None
    finally:
        await conn.close()


async def main() -> None:
    image_bytes = create_dummy_image()
    rejected_task_id = None
    retry_after_header = None

    log.info("--- 1. Наполнение очереди до лимита ---")
    async with httpx.AsyncClient(timeout=10.0) as client:
        for i in range(1, 200):
            files = {"file": ("test.jpg", io.BytesIO(image_bytes), "image/jpeg")}
            response = await client.post(API_URL, files=files)

            if response.status_code == HTTPStatus.ACCEPTED:
                data = response.json()
                if i % 20 == 0:
                    log.info("Запрос #%d принят: task_id=%s", i, data["task_id"])
            elif response.status_code == HTTPStatus.SERVICE_UNAVAILABLE:
                data = response.json()
                rejected_task_id = data.get("task_id")
                retry_after_header = response.headers.get("Retry-After")
                log.warning("Очередь заполнена! Запрос #%d отклонен с 503 Service Unavailable.", i)
                log.info("Заголовок Retry-After: %s", retry_after_header)
                log.info("Тело ответа: %s", data)
                break
            else:
                log.error("Неожиданный ответ %d: %s", response.status_code, response.text)
                return

    if not rejected_task_id:
        log.error("Не удалось упереться в лимит очереди. Проверьте, что воркер остановлен.")
        return

    log.info("\n--- 2. Проверка критерия 4.2 (отсутствие PENDING в БД) ---")
    exists_in_db = await check_db_for_task(rejected_task_id)

    print("\n================ РЕЗУЛЬТАТ ПРОВЕРКИ ================")
    print(f"Отклоненный task_id:      {rejected_task_id}")
    print(f"Заголовок Retry-After:    {retry_after_header} сек.")
    print(f"Запись в БД присутствует: {exists_in_db}")

    if not exists_in_db:
        print(" УСПЕХ: Задача при 503 не была сохранена в БД в статусе PENDING!")
    else:
        print(" ОШИБКА: Задача с 503 записалась в БД!")


if __name__ == "__main__":
    asyncio.run(main())
