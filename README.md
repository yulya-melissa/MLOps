# Fire Detection — наблюдаемость конвейера

Конвейер обработки изображений:

```
POST /api/v1/tasks ─┐
                    ├─► RabbitMQ (fire.tasks, x-max-length=100) ─► FastStream worker ─► Postgres
источник (batch) ───┘        reject-publish                модель из MLflow по alias
```

Этот документ описывает **логирование и организацию docker-compose** (разделы 1 и 5
практической работы «Наблюдаемость»). Трейсинг, метрики и Grafana подключаются
поверх этой базы отдельными профилями.

## Быстрый старт

```bash
cp .env.example .env          # Windows: Copy-Item .env.example .env

docker compose up -d                       # только приложение, логи в stdout
docker compose --profile logs up -d        # + Alloy и Loki
docker compose --profile load up source    # нагрузочный источник
docker compose --profile logs down         # выключить наблюдаемость
```

Приложение **не зависит** от стека наблюдаемости: `depends_on` сервисов конвейера
не ссылается на Loki/Alloy, логи пишутся в stdout независимо от того, читает их кто-то или нет.

## 1. Логирование

### Выбор библиотеки: structlog

* **JSON в stdout** из коробки (`JSONRenderer`), без файлов внутри контейнера и без `print`.
* **Единый формат для библиотек.** `ProcessorFormatter(foreign_pre_chain=...)` применяет
  те же процессоры к записям стандартного `logging` — uvicorn, asyncpg, aio-pika,
  FastStream и mlflow пишут те же поля, что и наш код.
* **contextvars.** `task_id` кладётся в контекст один раз в обработчике, а процессор
  подмешивает его в каждую запись — идентификатор не протаскивается руками по функциям.
* Структурированные события (`log.info("task_completed", objects_count=..., elapsed_ms=...)`)
  вместо форматирования строк: поля остаются машиночитаемыми.

Альтернативы: стандартный `logging` + ручной JSON-форматтер (больше кода, нет contextvars),
`loguru` (проще старт, но перехват логов библиотек и контекст требуют самописных обвязок).

### Единый набор полей

| Поле | Источник | Пример |
|---|---|---|
| `event` | имя события | `task_completed` |
| `level` | `add_log_level` | `info` |
| `logger` | `add_logger_name` | `src.worker.app` |
| `timestamp` | `TimeStamper(fmt="iso")` | `2026-11-19T16:28:49.486277Z` |
| `service` | `setup_logging(service_name=...)` | `api` / `worker` / `source` |
| `task_id` | contextvar из AMQP `correlation_id` | `fe5b2d93…` |
| `request_id` | contextvar из middleware API | `a1b2c3d4` |

Уровень задаётся переменной окружения `FIRE_LOG_LEVEL` (общий якорь `x-pipeline-env`).

### Ключевые события

| Уровень | Событие | Поля |
|---|---|---|
| INFO | `model_loaded` | `model_uri`, `model_name`, `model_alias`, **`model_version`** |
| INFO | `graceful_shutdown_started` / `_completed` | `inflight_tasks`, `timeout_s` |
| WARNING | `source_queue_full_retry` | `queue_max_length`, `delay_s`, `rejected` |
| WARNING | `task_rejected_503` | `task_id`, `reason`, `retry_after_s` |
| ERROR | `task_processing_failed` | `attempt`, `max_attempts`, `exception` (стектрейс) |
| ERROR | `task_moved_to_dlq` | `attempts`, `error` |

Версия модели берётся из Model Registry (`get_model_version_by_alias`), а не из конфига:
alias — подвижный указатель, и по логу должно быть видно, **какая версия** обработала задачу.

### task_id

`publisher.publish(..., correlation_id=task_id)` → воркер читает `message.correlation_id`
→ `logging_config.TASK_ID.set(...)` → процессор `_merge_context` добавляет поле во все
записи внутри обработчика. Штатный `merge_contextvars` видит только переменные,
положенные через `bind_contextvars`, поэтому для собственных `ContextVar` нужен свой процессор.

### Секреты и шум

* `_mask_secrets` затирает значения полей с именами `password`, `secret`, `token`, `dsn`, …
* `_mask_url_credentials` затирает пароль **внутри строк**: `postgresql://postgres:***@postgres:5432/...`
  (DSN часто попадает в текст исключения, где маскировать по имени поля нечего).
* `postgres_password` объявлен как `SecretStr` — в дампах настроек печатается `**********`.
* Health-check `/healthz` и `/api/v1/health` не логируются на INFO; `uvicorn.access` переведён на WARNING.

## 2. Агент и хранилище

### Почему Grafana Alloy

Один агент закрывает все три сигнала (логи, метрики, трейсы) — не придётся плодить
Promtail + vmagent + OTel Collector. Нативный `discovery.docker` сам находит контейнеры
и их метки, есть UI на `:12345` с графом компонентов. Promtail, к слову, уже в режиме
поддержки — Grafana переводит сбор логов на Alloy.

### Почему Loki, а не ELK или VictoriaLogs

* **Loki индексирует только лейблы**, сами строки хранятся сжатыми чанками. Индекс крошечный,
  хранение дешёвое, а поиск по `task_id` делается фильтром по содержимому — на объёме стенда
  (72 часа, десятки МБ) это доли секунды.
* **ELK** индексирует каждое поле: поиск по любому полю быстрый, но Elasticsearch требует
  явного heap и в разы больше памяти — при лимите VM 3,7 ГиБ это неоправданно.
* **VictoriaLogs** тоже индексирует все поля и дешевле ES, но стек Grafana (Grafana + Tempo +
  Prometheus) с Loki интегрируется без дополнительных datasource-плагинов и derived fields.

Итог: при нашем объёме логов выигрывает дешёвое хранение и единая витрина, а не скорость
поиска по произвольному полю. При 1 ТБ логов в сутки выбор пересматривается в пользу
VictoriaLogs/ClickHouse.

### Пайплайн

```
discovery.docker (фильтр по label com.docker.compose.project)
   └─ discovery.relabel (keep: app|worker|source; service ← compose service)
        └─ loki.source.docker (docker.sock :ro, лейбл env)
             └─ loki.process (stage.json → stage.labels: только level)
                  └─ loki.write → http://loki:3100/loki/api/v1/push
```

Логи инфраструктурных контейнеров (postgres, rabbitmq, mlflow) не собираются: они не в JSON.
Чтобы включить — расширь `regex` в `discovery.relabel`.

### Лейблы и кардинальность

Потоки (streams) в Loki = уникальные комбинации лейблов:

| Лейбл | Значения | Кол-во |
|---|---|---|
| `service` | app, worker, source | 3 |
| `level` | debug, info, warning, error | 4 |
| `env` | production | 1 |

**3 × 4 × 1 = 12 потоков** (плюс до трёх потоков без `level` для не-JSON строк).

Если сделать `task_id` лейблом, каждый прогон на 6000 задач создаст **≈6000–12000 потоков**
вместо 12 — рост в 500–1000 раз. Каждый поток — это отдельная серия в индексе и открытый
чанк в памяти ingester'а; итог — раздувание индекса, давление на память и деградация всех
запросов. То же относится к `trace_id` и `user_id`.

Поэтому `task_id` и `trace_id` остаются **полями строки** и ищутся фильтром:
`{service="worker"} | json | task_id="…"`.

## 3. Организация compose и бюджет памяти

```
docker-compose.yml            # приложение, без профилей наблюдаемости
observability/
  compose.yaml                # include; все сервисы под профилями
  loki/config.yaml
  log-agent/config.alloy
.env.example                  # версии образов, настройки телеметрии
```

Профили: `logs` (Alloy + Loki), далее `traces`, `metrics`, `observability` (всё + Grafana).

### Лимиты и фактическое потребление

| Сервис | Лимит | Факт под нагрузкой |
|---|---|---|
| Loki | 512 MiB | _заполнить по `docker stats`_ |
| Alloy | 256 MiB | _заполнить_ |
| **Итого профиль `logs`** | **768 MiB** | _заполнить_ |
| worker (1 шт., для справки) | — | ≈425 MiB |
| rabbitmq (для справки) | — | ≈155 MiB |

Проверка: `docker stats --no-stream` во время прогона источника.
Ориентир задания на полный профиль `observability` — ≈1,5 ГБ; наш раздел занимает
до 768 MiB по лимитам, остаток бюджета留给 Collector / Tempo / Prometheus / Grafana.

⚠️ Docker Desktop на стенде имеет **3,73 ГиБ** памяти. При полном профиле
наблюдаемости и двух воркерах рекомендуется поднять лимит VM до 6 ГиБ.

### Хранение

* Данные — в именованных томах `loki_data`, `alloy_data` (переживают `docker compose down`).
* Retention Loki — **72 часа**, обеспечивается compactor'ом (`retention_enabled: true`).
* Версии образов закреплены в `.env.example`, тег `latest` не используется.
* У Loki есть healthcheck (`loki -health` — в образах 3.6+ нет shell/wget).
* `docker.sock` смонтирован **только на чтение**. Это ограничение файловой операции,
  а не прав Docker API: в продакшене агент ходит к сокету через socket-proxy.

### Бюджет памяти (под нагрузкой)

Замер снимался при работающем источнике (`docker compose --profile load up -d source`)
и включённом профиле наблюдаемости (`docker compose --profile logs up -d`).
Общая память Docker VM на стенде: **3,73 ГиБ**.

#### Стек наблюдаемости (профиль `logs`)

| Сервис | Образ | Лимит | Факт под нагрузкой | % от лимита |
|---|---|---|---|---|
| Loki | `grafana/loki:3.7.0` | 512 MiB | **107 MiB** | 21% |
| Alloy | `grafana/alloy:v1.21.3` | 256 MiB | **133 MiB** | 52% |
| **Итого `logs`** | | **768 MiB** | **≈240 MiB** | — |

Ориентир задания на полный профиль `observability` (с Grafana, Tempo, Prometheus) —
около 1,5 ГБ. Наш стек наблюдаемости занимает 240 MiB, оставляя ~1,25 ГБ на оставшиеся
сервисы наблюдаемости, которые подключит сокомандница.

#### Конвейер (для справки)

| Сервис | Факт под нагрузкой |
|---|---|
| fire_detection_app (API) | 70 MiB |
| mlops-worker-1 | 209 MiB |
| fire_detection_postgres | 34 MiB |
| fire_rabbitmq | 104 MiB |
| mlflow_server | 600 MiB |
| mlflow_postgres | 39 MiB |
| mlflow_rustfs | 92 MiB |
| **Итого конвейер** | **≈1,15 GiB** |

**Суммарно под нагрузкой** (конвейер + профиль `logs`): **≈1,39 GiB из 3,73 GiB**
(≈37% памяти VM). Запас достаточен для добавления Grafana и OTel Collector.

### Retention

| Хранилище | Retention | Механизм |
|---|---|---|
| Loki | **72 часа** | `limits_config.retention_period` + `compactor.retention_enabled` |
| Alloy (WAL) | 24 часа | буфер при недоступности Loki; персистентный в томе `alloy_data` |

### Healthchecks

| Сервис | Проверка | Статус |
|---|---|---|
| Loki | `/usr/bin/loki -health` | ✓ native, без busybox |
| Alloy | `wget http://127.0.0.1:12345/-/healthy` | ✓ HTTP, endpoint `/-/healthy` |

### Именованные тома и закрепление версий

- **Данные** — в именованных томах `loki_data`, `alloy_data` (переживают `docker compose down`).
- **Теги образов** закреплены в `.env.example`:
  - `grafana/loki:3.7.0`
  - `grafana/alloy:v1.21.3`
  - `amazon/aws-cli:2.41.1`
  - Тег `latest` **не используется** нигде в стеке.

## 4. Как найти историю задачи

LogQL (в Grafana — Explore, без Grafana — HTTP API Loki):

```logql
# вся история задачи во всех сервисах
{env="production"} | json | task_id="<TASK_ID>"

# все ошибки за 15 минут
{level="error"}

# перевод в DLQ и graceful shutdown
{service="worker"} |~ "task_moved_to_dlq|graceful_shutdown"

# отказы брокера и замедление источника
{service="source", level="warning"}
```

Пример запроса без Grafana (PowerShell):

```powershell
$q = [uri]::EscapeDataString('{env="production"} | json | task_id="TASK_ID"')
$r = Invoke-RestMethod "http://localhost:3100/loki/api/v1/query_range?query=$q&limit=100"
foreach ($s in $r.data.result) { $s.stream.service; foreach ($v in $s.values) { $v[1] } }
```

## 5. Что подключает команда дальше

* **Grafana** (раздел 4): datasource `http://loki:3100`, derived fields `trace_id` → Tempo,
  дашборд «Конвейер», алерты на заполнение очереди и рост DLQ.
* **Трейсинг** (раздел 2): OTel SDK → Collector → Tempo; `trace_id`/`span_id` добавляются
  в те же JSON-логи, поэтому корреляция заработает без изменения формата.
* **Метрики** (раздел 3): Prometheus + RabbitMQ-плагин `:15692`.

Все они добавляются в `observability/compose.yaml` под своими профилями.

## 6. Известные ограничения

* Логи инфраструктурных контейнеров не собираются (не-JSON формат).
* Реплики воркера в логах неразличимы по лейблу (`service=worker` у обеих);
  различение появится с `service.instance.id` в трейсинге.
* Retention 72 часа — стенд, не продакшен.

## Демонстрация независимости приложения от наблюдаемости

Это ключевой критерий раздела 5: стек наблюдаемости можно отключить,
и конвейер продолжит работать.

```bash
# Поднять приложение
docker compose up -d

# Поднять наблюдаемость
docker compose --profile logs up -d

# Отправить задачу и убедиться, что она обработалась
curl.exe -X POST http://localhost:8000/api/v1/tasks/by-path \
  -H "Content-Type: application/json" \
  -d '{"image_path":"images/test.jpg"}'

# Остановить стек наблюдаемости — конвейер продолжает работать
docker compose --profile logs stop alloy loki

curl.exe -X POST http://localhost:8000/api/v1/tasks/by-path \
  -H "Content-Type: application/json" \
  -d '{"image_path":"images/test.jpg"}'
# → 202 Accepted, task_id возвращается, воркер обрабатывает

# Логи в это время копируются в stdout контейнеров (docker logs mlops-worker-1)
# и ждут Alloy: после повторного старта агент дочитает их с момента останова.
docker compose --profile logs start alloy loki




---

## 🎯 Что дальше: следующие разделы задания

Согласно ТЗ, осталось выполнить три раздела:

### Раздел 2: Распределенная трассировка (3 балла)
**Что нужно сделать:**
1. Инструментировать код OpenTelemetry SDK
2. Настроить OTel Collector (профиль `traces`)
3. Выбрать хранилище трейсов (Tempo/Jaeger/VictoriaTraces)
4. Создать сквозные трейсы: `API → RabbitMQ → Worker → MLflow → БД`
5. Добавить ручные спаны для inference и записи в БД

**Ключевые критерии:**
- Один `trace_id` проходит через все сервисы
- Контекст передается через RabbitMQ (заголовок `traceparent`)
- Inference выполняется в пуле потоков, контекст передается явно
- Ошибки видны в трейсах (статус ERROR, исключения)

### Раздел 3: Метрики (1.5 балла)
**Что нужно сделать:**
1. Экспортировать метрики API, worker, source
2. Настроить сбор метрик RabbitMQ (порт 15692)
3. Выбрать хранилище (Prometheus/VictoriaMetrics)
4. Обосновать pull vs push модель

**Ключевые метрики:**
- API: количество запросов, latency (гистограмма), коды ответов (включая 503)
- Worker: обработано задач (ok/error/dlq), latency inference, задач в работе (gauge)
- Source: опубликовано задач, отказов брокера
- RabbitMQ: глубина очередей (рабочей и DLQ), unacked messages

### Раздел 4: Grafana (2.5 балла)
**Что нужно сделать:**
1. Настроить Grafana через provisioning (YAML + JSON)
2. Создать дашборд "Конвейер":
   - Глубина очереди с линией лимита
   - Скорость источника vs пропускная способность воркеров
   - Перцентили latency (p50, p95, p99)
   - Память сервисов
   - Доля 503, отказы брокера, рост DLQ
3. Настроить корреляцию: лог → трейс → лог
4. Создать алерты (очередь заполнена, рост DLQ)

---
