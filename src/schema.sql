-- Идемпотентный DDL: безопасно выполнять при каждом старте API и воркера.
CREATE TABLE IF NOT EXISTS tasks (
    task_id     TEXT PRIMARY KEY,  -- уникальный ключ = идемпотентность (5.3)
    status      TEXT NOT NULL DEFAULT 'PENDING'
                CHECK (status IN ('PENDING', 'PROCESSING', 'DONE', 'FAILED')),
    source      TEXT NOT NULL DEFAULT 'api',
    image_path  TEXT NOT NULL,
    result      JSONB,
    error       TEXT,
    attempts    INTEGER NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks (status);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks (created_at DESC);
