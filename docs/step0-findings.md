# Шаг 0 — верификация API (FastStream 0.7.7) и поведения RabbitMQ 4.4

Проверено на реальном брокере (`rabbitmq:4-management-alpine` → **RabbitMQ 4.4.0**),
FastStream **0.7.7** (в `uv.lock`), Python 3.12.

Все утверждения ниже — результат запуска кода, а не чтения документации.

---

## 1. FastStream 0.7.7 — факты по API

| Что | Факт | Итог для кода |
|---|---|---|
| `AckPolicy` | лежит **не** в `faststream.rabbit`, а в `faststream.middlewares`; реэкспортирован из корня | `from faststream import AckPolicy` — работает |
| Состав `AckPolicy` | `ACK_FIRST, ACK, REJECT_ON_ERROR, NACK_ON_ERROR, MANUAL` | — |
| Политика по умолчанию (не задана ни брокеру, ни подписчику) | `REJECT_ON_ERROR` | полагаться нельзя — задаём явно |
| `MANUAL` | `auto_ack_disabled = True` → middleware подтверждений **не устанавливается**, FastStream не ack'ает/не nack'ает ничего | нет двойного settle, полный контроль |
| `RabbitMessage` | `ack(multiple=False)`, `nack(multiple=False, requeue=True)`, `reject(requeue=False)` — все `async` | смена nack на reject — см. §2 |
| Доступ к сообщению в хендлере | `Annotated[StreamMessage, Context()]` | работает, заголовки доходят |
| `broker.publish(..., headers=, timeout=, mandatory=, persist=)` | поддерживается | публикация с confirm по умолчанию |
| `Channel.publisher_confirms` | **True по умолчанию** | `DeliveryError` при nack брокера приходит сам |
| `Channel.prefetch_count`, `global_qos` | есть | `global_qos` у quorum-очередей **не поддерживается** брокером |
| `RabbitQueue(queue_type=QueueType.QUORUM, arguments={...})` | аргументы пробрасываются как есть; `x-max-length`, `x-overflow`, `x-delivery-limit`, `x-dead-letter-*` валидны | — |
| Жизненный цикл брокера | `connect()`, `start()`, `stop()`, `declare_queue()`, `ping(timeout)`, `async with broker` | метода `close()` **нет** — только `stop()` |
| `graceful_timeout` | дефолт **15.0 c** в `RabbitBroker.__init__` | поднимаем до 30–60 c |
| `faststream.rabbit` | `QueueType` (CLASSIC/QUORUM/STREAM), `TestRabbitBroker`, `RabbitExchange` | код участника 1 валиден |

---

## 2. RabbitMQ 4.4 — факты по очереди (проверено на живом брокере)

### 2.1 Переполнение очереди → `DeliveryError` (нужно для 4.1)

`x-max-length: 2` + `x-overflow: reject-publish` на quorum-очереди:

```
publish #0..#2 — приняты
publish #3..#5 — DeliveryError: Message delivery failed: Basic.Nack(delivery_tag=4, multiple=True)
```

* publisher confirms включены → брокер отвечает **nack** → `aio_pika` бросает `DeliveryError`;
  FastStream его **не** перехватывает → `src/pipeline/publisher.py` (`except DeliveryError`)
  работает корректно, 403/503 по нему выдаётся мгновенно.
* **Важно:** quorum-очередь превышает лимит минимум на одно сообщение
  (документация RabbitMQ: «allows a quorum queue to overshoot its limit by at least one
  message»). Наблюдалось ровно +1. Формулировка «глубина ≤ лимита» на защите должна
  быть «лимит + 1 (документированный overshoot брокера)».

### 2.2 Счётчик попыток: `nack` не считается, `reject` — считается

Документация RabbitMQ 4.4 + проверка:

| Действие | `x-acquired-count` | `x-delivery-count` |
|---|---|---|
| `basic.nack(requeue=True)` | +1 | **не меняется** |
| `basic.reject(requeue=True)` | +1 | **+1** |
| падение клиента / обрыв канала | +1 | +1 |

Фактическое наблюдение при `nack(requeue=True)` — бесконечная передоставка:

```
доставка 1: headers={'x-retry-count': 0}                                  redelivered=False
доставка 2: headers={'x-acquired-count': 1, 'x-retry-count': 0}           redelivered=True
доставка 3: headers={'x-acquired-count': 2, ...}
доставка 4: headers={'x-acquired-count': 3, ...}
доставка 5: headers={'x-acquired-count': 4, ...}   -> в DLQ не попало, лимит не сработал
```

**Вывод: `nack(requeue=True)` для ретраев использовать нельзя** — лимит доставок
брокера не срабатывает, возможен бесконечный цикл. Надо `reject(requeue=True)`.

### 2.3 Ограниченное число попыток + переход в DLQ — нативно, без ручной публикации

`x-delivery-limit: 3` + `x-dead-letter-exchange: ""` + `x-dead-letter-routing-key: <dlq>`,
ретрай через `reject(requeue=True)`:

```
доставка 1: headers={'x-retry-count': 0}                                              redelivered=False
доставка 2: headers={'x-acquired-count': 1, 'x-delivery-count': 1, ...}              redelivered=True
доставка 3: headers={'x-acquired-count': 2, 'x-delivery-count': 2, ...}
доставка 4: headers={'x-acquired-count': 3, 'x-delivery-count': 3, ...}
доставка 5: сообщений нет  -> брокер сработал лимитом
В DLQ: x-death=[{'count': 1, 'reason': 'delivery_limit', 'queue': 'raw.rejectcase', ...}]
```

* `x-delivery-limit: N` → сообщение доставляется **N+1 раз**, потом брокер сам
  переносит его в DLQ с `x-death.reason = delivery_limit`.
* `reject(requeue=False)` → сообщение уходит в DLQ сразу,
  `x-death.reason = rejected` (проверено отдельно).
* В DLQ сообщение сохраняет исходные заголовки + `x-death`, `x-first-death-*`,
  `x-last-death-*`, `x-delivery-count`.
* Окно потери сообщения отсутствует: маршрут в DLQ делает **брокер**, а не наш код.

### 2.4 Прочее

* Dead-lettering по умолчанию **at-most-once**. Для at-least-once нужен
  `dead-letter-strategy: at-least-once` **политикой** + `overflow: reject-publish`
  (у нас он есть) + feature flag `stream_queue`. Не обязательно для лабораторной,
  но это аргумент для защиты.
* Quorum-очереди не поддерживают global QoS → `Channel(global_qos=...)` не использовать.
* `x-overflow: reject-publish-dlx` для quorum-очередей **не поддерживается**.

---

## 3. Что это меняет в плане (моя часть: п. 4 и 5)

| Пункт | Было в плане | Стало | Почему |
|---|---|---|---|
| 5.2 ретрай | `nack(requeue=True)` | **`reject(requeue=True)`** | `nack` не инкрементит delivery-count → лимит брокера не срабатывает → бесконечный цикл |
| 5.2 счётчик попыток | заголовок `x-delivery-count` от брокера | он же, но читаем только при `reject`-ретраях: попытка = `1 + x-delivery-count` (на первой доставке заголовка нет) | проверено фактически |
| 5.2 лимит | `x-delivery-limit` в аргументах очереди | `x-delivery-limit = max_attempts` + DLX/DLQ — **работает**, страхует от краша воркера (delivery-count растёт при падении клиента) | проверено |
| 5.2 «последняя» попытка | `reject(requeue=False)` | без изменений → `x-death.reason = rejected` | проверено |
| 4.1 503 | `DeliveryError` → 503 | без изменений, подтверждено на живом брокере | проверено |
| 4.1/эксперимент | «глубина ≤ лимита» | «глубина ≤ лимита + 1 (документированный overshoot quorum-очереди)» | доказанный overshoot +1 |
| 5.1 ack_policy | `MANUAL` | без изменений | `auto_ack_disabled=True` → FastStream не трогает подтверждения |

Не требует изменений: порядок «publish → insert» (4.2), идемпотентность через
`PRIMARY KEY` + `ON CONFLICT` (5.3), graceful shutdown через `graceful_timeout`
+ трекер in-flight (5.4).

---

## 4. Решения, принятые после шага 0

* Дальше по плану — **шаг 1** (схема БД + репозиторий).
* `POST /api/v1/tasks` — **оба варианта**: multipart с файлом + `by-path` с JSON
  `{image_path}` (by-path нужен для нагрузочных сценариев и демо-скриптов).
* `POST /process` — **пока не трогаем** (переведём на новый flow в шаге 4 вместе с UI).

Окружение: контейнер `fire_rabbitmq` поднят и оставлен работающим
(`docker compose up -d rabbitmq`), тестовые очереди удалены.
