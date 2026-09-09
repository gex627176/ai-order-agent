from __future__ import annotations

import json
import hashlib
import math
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .error_handling import public_error


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    contact TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY,
    sku TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL UNIQUE,
    aliases_json TEXT NOT NULL DEFAULT '[]',
    unit TEXT NOT NULL,
    unit_price REAL NOT NULL CHECK (unit_price >= 0),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);

CREATE TABLE IF NOT EXISTS sku_catalog_versions (
    site_id INTEGER NOT NULL,
    version INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'archived')),
    content_md5 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (site_id, version)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_sku_version_per_site
ON sku_catalog_versions(site_id) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS sku_snapshots (
    site_id INTEGER NOT NULL,
    sku_version INTEGER NOT NULL,
    product_id INTEGER NOT NULL,
    sku TEXT NOT NULL,
    name TEXT NOT NULL,
    aliases_json TEXT NOT NULL,
    unit TEXT NOT NULL,
    unit_price REAL NOT NULL CHECK (unit_price >= 0),
    active INTEGER NOT NULL CHECK (active IN (0, 1)),
    PRIMARY KEY (site_id, sku_version, product_id),
    FOREIGN KEY (site_id, sku_version)
        REFERENCES sku_catalog_versions(site_id, version)
);

CREATE TABLE IF NOT EXISTS drafts (
    id TEXT PRIMARY KEY,
    customer_id INTEGER,
    source_text TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('needs_review', 'confirmed')),
    items_json TEXT NOT NULL,
    warnings_json TEXT NOT NULL DEFAULT '[]',
    trace_json TEXT NOT NULL DEFAULT '[]',
    provider TEXT NOT NULL,
    site_id INTEGER NOT NULL DEFAULT 1,
    sku_version INTEGER NOT NULL DEFAULT 1,
    retrieval_json TEXT NOT NULL DEFAULT '[]',
    preferences_json TEXT NOT NULL DEFAULT '[]',
    input_type TEXT NOT NULL DEFAULT 'text',
    source_filename TEXT NOT NULL DEFAULT '',
    input_confidence REAL,
    input_warnings_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (customer_id) REFERENCES customers(id)
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_no TEXT NOT NULL UNIQUE,
    draft_id TEXT NOT NULL UNIQUE,
    customer_id INTEGER NOT NULL,
    source_text TEXT NOT NULL,
    total_amount REAL NOT NULL CHECK (total_amount >= 0),
    status TEXT NOT NULL DEFAULT 'confirmed',
    site_id INTEGER NOT NULL DEFAULT 1,
    sku_version INTEGER NOT NULL DEFAULT 1,
    idempotency_key TEXT NOT NULL UNIQUE,
    payload_md5 TEXT NOT NULL,
    input_type TEXT NOT NULL DEFAULT 'text',
    source_filename TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY (customer_id) REFERENCES customers(id),
    FOREIGN KEY (draft_id) REFERENCES drafts(id)
);

CREATE TABLE IF NOT EXISTS order_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL,
    line_no INTEGER NOT NULL,
    product_id INTEGER NOT NULL,
    product_name TEXT NOT NULL,
    quantity REAL NOT NULL CHECK (quantity > 0),
    unit TEXT NOT NULL,
    unit_price REAL NOT NULL CHECK (unit_price >= 0),
    note TEXT NOT NULL DEFAULT '',
    subtotal REAL NOT NULL CHECK (subtotal >= 0),
    FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE,
    FOREIGN KEY (product_id) REFERENCES products(id),
    UNIQUE (order_id, line_no)
);

CREATE TABLE IF NOT EXISTS correction_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id TEXT NOT NULL,
    customer_id INTEGER,
    field_path TEXT NOT NULL,
    before_json TEXT NOT NULL,
    after_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (draft_id) REFERENCES drafts(id),
    FOREIGN KEY (customer_id) REFERENCES customers(id)
);

CREATE TABLE IF NOT EXISTS customer_preferences (
    customer_id INTEGER NOT NULL,
    preference_key TEXT NOT NULL,
    preference_value_json TEXT NOT NULL,
    evidence_count INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (customer_id, preference_key),
    FOREIGN KEY (customer_id) REFERENCES customers(id)
);

CREATE TABLE IF NOT EXISTS llm_call_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    draft_id TEXT,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    prompt_tokens INTEGER NOT NULL DEFAULT 0 CHECK (prompt_tokens >= 0),
    completion_tokens INTEGER NOT NULL DEFAULT 0 CHECK (completion_tokens >= 0),
    total_tokens INTEGER NOT NULL DEFAULT 0 CHECK (total_tokens >= 0),
    estimated_cost REAL NOT NULL DEFAULT 0 CHECK (estimated_cost >= 0),
    duration_ms INTEGER NOT NULL DEFAULT 0 CHECK (duration_ms >= 0),
    status TEXT NOT NULL CHECK (status IN ('succeeded', 'fallback', 'failed')),
    error_type TEXT NOT NULL DEFAULT '',
    fallback_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_llm_call_records_created_at
ON llm_call_records(created_at DESC);

CREATE INDEX IF NOT EXISTS idx_llm_call_records_status
ON llm_call_records(status);

CREATE TABLE IF NOT EXISTS recognition_tasks (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    payload_md5 TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'running', 'retrying', 'succeeded', 'failed', 'cancelled')
    ),
    text TEXT NOT NULL,
    customer_id INTEGER,
    site_id INTEGER NOT NULL DEFAULT 1,
    input_type TEXT NOT NULL DEFAULT 'text',
    source_filename TEXT NOT NULL DEFAULT '',
    input_confidence REAL,
    input_warnings_json TEXT NOT NULL DEFAULT '[]',
    draft_id TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    max_attempts INTEGER NOT NULL DEFAULT 3 CHECK (max_attempts >= 1),
    cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK (cancel_requested IN (0, 1)),
    error_type TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    next_run_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    FOREIGN KEY (customer_id) REFERENCES customers(id),
    FOREIGN KEY (draft_id) REFERENCES drafts(id)
);

CREATE INDEX IF NOT EXISTS idx_recognition_tasks_runnable
ON recognition_tasks(status, next_run_at, created_at);

CREATE TABLE IF NOT EXISTS catalog_alias_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id INTEGER NOT NULL,
    product_id INTEGER NOT NULL,
    alias TEXT NOT NULL,
    evidence_count INTEGER NOT NULL DEFAULT 1 CHECK (evidence_count >= 1),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending', 'approved', 'rejected')
    ),
    source_type TEXT NOT NULL DEFAULT 'human_correction',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    reviewed_at TEXT,
    UNIQUE(site_id, product_id, alias),
    FOREIGN KEY (product_id) REFERENCES products(id)
);

CREATE INDEX IF NOT EXISTS idx_alias_candidates_status
ON catalog_alias_candidates(status, updated_at DESC);

CREATE TABLE IF NOT EXISTS catalog_audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    source_type TEXT NOT NULL,
    before_json TEXT NOT NULL DEFAULT 'null',
    after_json TEXT NOT NULL DEFAULT 'null',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_catalog_audit_logs_created_at
ON catalog_audit_logs(created_at DESC);
"""


CUSTOMERS = [
    (1, "惠民餐厅", "王店长"),
    (2, "阳光幼儿园", "李老师"),
    (3, "青禾生鲜店", "陈老板"),
]

PRODUCTS = [
    (1, "SKU-TOMATO", "西红柿", ["番茄"], "斤", 3.80, 1),
    (2, "SKU-CARROT", "胡萝卜", ["红萝卜"], "公斤", 4.60, 1),
    (3, "SKU-APPLE", "苹果", ["红富士"], "箱", 68.00, 1),
    (4, "SKU-ONION", "洋葱", ["葱头", "圆葱"], "斤", 2.90, 1),
    (5, "SKU-POTATO", "土豆", ["马铃薯"], "斤", 2.50, 1),
]


class IdempotencyConflictError(ValueError):
    pass


class TaskStateConflictError(ValueError):
    pass


class Database:
    def __init__(self, path: str):
        self.path = path

    @contextmanager
    def connect(self):
        db_path = Path(self.path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            self._migrate_existing_schema(connection)
            connection.executemany(
                "INSERT OR IGNORE INTO customers(id, name, contact) VALUES (?, ?, ?)",
                CUSTOMERS,
            )
            connection.executemany(
                """
                INSERT OR IGNORE INTO products(
                    id, sku, name, aliases_json, unit, unit_price, active
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (product_id, sku, name, json.dumps(aliases, ensure_ascii=False), unit, price, active)
                    for product_id, sku, name, aliases, unit, price, active in PRODUCTS
                ],
            )
            self._seed_initial_sku_snapshot(connection)

    def list_customers(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id, name, contact FROM customers ORDER BY id"
            ).fetchall()
        return [dict(row) for row in rows]

    def list_products(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, sku, name, aliases_json, unit, unit_price, active
                FROM products ORDER BY id
                """
            ).fetchall()
        return [self._product_from_row(row) for row in rows]

    def create_product(
        self, payload: dict[str, Any], site_id: int
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        normalized = self._normalize_product_payload(payload)
        with self.connect() as connection:
            self._require_site_locked(connection, site_id)
            cursor = connection.execute(
                """
                INSERT INTO products(sku, name, aliases_json, unit, unit_price, active)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized["sku"], normalized["name"],
                    self._json(normalized["aliases"]), normalized["unit"],
                    normalized["unit_price"], int(normalized["active"]),
                ),
            )
            product_id = int(cursor.lastrowid)
            product = {"id": product_id, **normalized}
            self._record_catalog_audit(
                connection, site_id, "create", "product", str(product_id),
                payload.get("source_type", "admin"), None, product, now,
            )
            version = self._publish_catalog_locked(connection, site_id, now)
        return product, version

    def update_product(
        self, product_id: int, payload: dict[str, Any], site_id: int,
        source_type: str = "admin",
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        normalized = self._normalize_product_payload(payload)
        with self.connect() as connection:
            self._require_site_locked(connection, site_id)
            row = connection.execute(
                """
                SELECT id, sku, name, aliases_json, unit, unit_price, active
                FROM products WHERE id = ?
                """,
                (product_id,),
            ).fetchone()
            if not row:
                raise LookupError("商品不存在")
            before = self._product_from_row(row)
            connection.execute(
                """
                UPDATE products
                SET sku = ?, name = ?, aliases_json = ?, unit = ?,
                    unit_price = ?, active = ?
                WHERE id = ?
                """,
                (
                    normalized["sku"], normalized["name"],
                    self._json(normalized["aliases"]), normalized["unit"],
                    normalized["unit_price"], int(normalized["active"]), product_id,
                ),
            )
            product = {"id": product_id, **normalized}
            self._record_catalog_audit(
                connection, site_id, "update", "product", str(product_id),
                source_type, before, product, now,
            )
            version = self._publish_catalog_locked(connection, site_id, now)
        return product, version

    def import_products(
        self, products: list[dict[str, Any]], site_id: int
    ) -> dict[str, Any]:
        if not products:
            raise ValueError("导入文件没有商品数据")
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        normalized_rows = [self._normalize_product_payload(row) for row in products]
        imported_skus = {product["sku"] for product in normalized_rows}
        with self.connect() as connection:
            self._require_site_locked(connection, site_id)
            existing_skus = {
                row["sku"] for row in connection.execute(
                    "SELECT sku FROM products"
                ).fetchall()
            }
            connection.executemany(
                """
                INSERT INTO products(sku, name, aliases_json, unit, unit_price, active)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(sku) DO UPDATE SET
                    name = excluded.name,
                    aliases_json = excluded.aliases_json,
                    unit = excluded.unit,
                    unit_price = excluded.unit_price,
                    active = excluded.active
                """,
                [
                    (
                        product["sku"], product["name"],
                        self._json(product["aliases"]), product["unit"],
                        product["unit_price"], int(product["active"]),
                    )
                    for product in normalized_rows
                ],
            )
            imported_by_sku = {
                row["sku"]: int(row["id"])
                for row in connection.execute(
                    "SELECT id, sku FROM products"
                ).fetchall()
                if row["sku"] in imported_skus
            }
            connection.executemany(
                """
                INSERT INTO catalog_audit_logs(
                    site_id, action, entity_type, entity_id, source_type,
                    before_json, after_json, created_at
                ) VALUES (?, ?, 'product', ?, 'import', 'null', ?, ?)
                """,
                [
                    (
                        site_id,
                        "import_update" if product["sku"] in existing_skus else "import_create",
                        str(imported_by_sku[product["sku"]]),
                        self._json({"id": imported_by_sku[product["sku"]], **product}),
                        now,
                    )
                    for product in normalized_rows
                ],
            )
            version = self._publish_catalog_locked(connection, site_id, now)
        return {"imported_count": len(normalized_rows), "version": version}

    def get_active_sku_version(self, site_id: int = 1) -> int:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT version FROM sku_catalog_versions
                WHERE site_id = ? AND status = 'active'
                """,
                (site_id,),
            ).fetchone()
        if not row:
            raise LookupError(f"站点 {site_id} 没有生效的 SKU 版本")
        return int(row["version"])

    def get_sku_version(self, site_id: int, version: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT site_id, version, status, content_md5, created_at
                FROM sku_catalog_versions WHERE site_id = ? AND version = ?
                """,
                (site_id, version),
            ).fetchone()
        return dict(row) if row else None

    def list_sku_snapshot(self, site_id: int, version: int) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT product_id AS id, sku, name, aliases_json, unit,
                       unit_price, active
                FROM sku_snapshots
                WHERE site_id = ? AND sku_version = ?
                ORDER BY product_id
                """,
                (site_id, version),
            ).fetchall()
        return [self._product_from_row(row) for row in rows]

    def create_sku_version(self, site_id: int) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_site_locked(connection, site_id)
            version = self._publish_catalog_locked(connection, site_id, now)
        return version

    def get_product(
        self, product_id: int, site_id: int | None = None, sku_version: int | None = None
    ) -> dict[str, Any] | None:
        if site_id is not None and sku_version is not None:
            with self.connect() as connection:
                row = connection.execute(
                    """
                    SELECT product_id AS id, sku, name, aliases_json, unit,
                           unit_price, active
                    FROM sku_snapshots
                    WHERE site_id = ? AND sku_version = ? AND product_id = ?
                    """,
                    (site_id, sku_version, product_id),
                ).fetchone()
            return self._product_from_row(row) if row else None
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT id, sku, name, aliases_json, unit, unit_price, active
                FROM products WHERE id = ?
                """,
                (product_id,),
            ).fetchone()
        return self._product_from_row(row) if row else None

    def customer_exists(self, customer_id: int) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM customers WHERE id = ?", (customer_id,)
            ).fetchone()
        return row is not None

    def create_draft(self, draft: dict[str, Any]) -> dict[str, Any]:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO drafts(
                    id, customer_id, source_text, status, items_json,
                    warnings_json, trace_json, provider, site_id, sku_version,
                    retrieval_json, preferences_json, created_at, updated_at,
                    input_type, source_filename, input_confidence, input_warnings_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    draft["id"], draft["customer_id"], draft["source_text"],
                    draft["status"], self._json(draft["items"]),
                    self._json(draft["warnings"]), self._json(draft["trace"]),
                    draft["provider"], draft.get("site_id", 1),
                    draft.get("sku_version", 1), self._json(draft.get("retrieval", [])),
                    self._json(draft.get("preferences", [])),
                    draft["created_at"], draft["updated_at"],
                    draft.get("input_type", "text"), draft.get("source_filename", ""),
                    draft.get("input_confidence"),
                    self._json(draft.get("input_warnings", [])),
                ),
            )
        return self.get_draft(draft["id"])

    def get_draft(self, draft_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM drafts WHERE id = ?", (draft_id,)
            ).fetchone()
        return self._draft_from_row(row) if row else None

    def list_drafts(
        self, status: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM drafts"
        parameters: list[Any] = []
        if status is not None:
            query += " WHERE status = ?"
            parameters.append(status)
        query += " ORDER BY updated_at DESC, created_at DESC LIMIT ?"
        parameters.append(limit)
        with self.connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._draft_from_row(row) for row in rows]

    def create_recognition_task(self, task: dict[str, Any]) -> dict[str, Any]:
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT * FROM recognition_tasks WHERE idempotency_key = ?",
                (task["idempotency_key"],),
            ).fetchone()
            if existing:
                if existing["payload_md5"] != task["payload_md5"]:
                    raise IdempotencyConflictError("任务幂等键已用于不同的识别载荷")
                return self._recognition_task_from_row(existing)
            try:
                connection.execute(
                    """
                    INSERT INTO recognition_tasks(
                        id, idempotency_key, payload_md5, status, text,
                        customer_id, site_id, input_type, source_filename,
                        input_confidence, input_warnings_json,
                        attempt_count, max_attempts, cancel_requested,
                        error_type, error_message, next_run_at,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, 0, ?, 0, '', '', ?, ?, ?)
                    """,
                    (
                        task["id"], task["idempotency_key"], task["payload_md5"],
                        task["text"], task.get("customer_id"), task["site_id"],
                        task.get("input_type", "text"), task.get("source_filename", ""),
                        task.get("input_confidence"),
                        self._json(task.get("input_warnings", [])),
                        task.get("max_attempts", 3), task["created_at"],
                        task["created_at"], task["created_at"],
                    ),
                )
            except sqlite3.IntegrityError:
                raced = connection.execute(
                    "SELECT * FROM recognition_tasks WHERE idempotency_key = ?",
                    (task["idempotency_key"],),
                ).fetchone()
                if raced is None:
                    raise
                if raced["payload_md5"] != task["payload_md5"]:
                    raise IdempotencyConflictError(
                        "任务幂等键已用于不同的识别载荷"
                    )
                return self._recognition_task_from_row(raced)
            row = connection.execute(
                "SELECT * FROM recognition_tasks WHERE id = ?", (task["id"],)
            ).fetchone()
        if row is None:
            raise RuntimeError("识别任务写入后读取失败")
        return self._recognition_task_from_row(row)

    def get_recognition_task(self, task_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM recognition_tasks WHERE id = ?", (task_id,)
            ).fetchone()
        return self._recognition_task_from_row(row) if row else None

    def list_recognition_tasks(
        self, status: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM recognition_tasks"
        parameters: list[Any] = []
        if status is not None:
            query += " WHERE status = ?"
            parameters.append(status)
        query += " ORDER BY created_at DESC LIMIT ?"
        parameters.append(limit)
        with self.connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._recognition_task_from_row(row) for row in rows]

    def recover_interrupted_recognition_tasks(self, now: str) -> int:
        with self.connect() as connection:
            retry_result = connection.execute(
                """
                UPDATE recognition_tasks
                SET status = 'retrying', error_type = 'WorkerInterrupted',
                    error_message = '应用重启后恢复执行', next_run_at = ?,
                    updated_at = ?, started_at = NULL
                WHERE status = 'running' AND cancel_requested = 0
                  AND attempt_count < max_attempts
                """,
                (now, now),
            )
            cancel_result = connection.execute(
                """
                UPDATE recognition_tasks
                SET status = 'cancelled', finished_at = ?, updated_at = ?
                WHERE status = 'running' AND cancel_requested = 1
                """,
                (now, now),
            )
            failed_result = connection.execute(
                """
                UPDATE recognition_tasks
                SET status = 'failed', error_type = 'WorkerInterrupted',
                    error_message = '任务在最后一次执行中断后无法继续重试',
                    finished_at = ?, updated_at = ?
                WHERE status = 'running' AND cancel_requested = 0
                  AND attempt_count >= max_attempts
                """,
                (now, now),
            )
        return retry_result.rowcount + cancel_result.rowcount + failed_result.rowcount

    def claim_next_recognition_task(self, now: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM recognition_tasks
                WHERE status IN ('pending', 'retrying')
                  AND cancel_requested = 0 AND next_run_at <= ?
                ORDER BY next_run_at, created_at LIMIT 1
                """,
                (now,),
            ).fetchone()
            if not row:
                return None
            result = connection.execute(
                """
                UPDATE recognition_tasks
                SET status = 'running', attempt_count = attempt_count + 1,
                    started_at = ?, updated_at = ?
                WHERE id = ? AND status IN ('pending', 'retrying')
                """,
                (now, now, row["id"]),
            )
            if result.rowcount != 1:
                return None
            claimed = connection.execute(
                "SELECT * FROM recognition_tasks WHERE id = ?", (row["id"],)
            ).fetchone()
        return self._recognition_task_from_row(claimed) if claimed else None

    def complete_recognition_task(
        self, task_id: str, draft_id: str, now: str
    ) -> dict[str, Any]:
        with self.connect() as connection:
            current = connection.execute(
                "SELECT cancel_requested FROM recognition_tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if not current:
                raise LookupError("识别任务不存在")
            if current["cancel_requested"]:
                connection.execute(
                    """
                    UPDATE recognition_tasks
                    SET status = 'cancelled', draft_id = ?, finished_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (draft_id, now, now, task_id),
                )
            else:
                connection.execute(
                    """
                    UPDATE recognition_tasks
                    SET status = 'succeeded', draft_id = ?, error_type = '',
                        error_message = '', finished_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (draft_id, now, now, task_id),
                )
            row = connection.execute(
                "SELECT * FROM recognition_tasks WHERE id = ?", (task_id,)
            ).fetchone()
        return self._recognition_task_from_row(row)

    def fail_recognition_task(
        self, task_id: str, error_type: str, error_message: str,
        now: str, next_run_at: str, retryable: bool = True,
    ) -> dict[str, Any]:
        with self.connect() as connection:
            current = connection.execute(
                """
                SELECT attempt_count, max_attempts, cancel_requested
                FROM recognition_tasks WHERE id = ?
                """,
                (task_id,),
            ).fetchone()
            if not current:
                raise LookupError("识别任务不存在")
            if current["cancel_requested"]:
                status = "cancelled"
            elif retryable and current["attempt_count"] < current["max_attempts"]:
                status = "retrying"
            else:
                status = "failed"
            finished_at = now if status in {"cancelled", "failed"} else None
            connection.execute(
                """
                UPDATE recognition_tasks
                SET status = ?, error_type = ?, error_message = ?,
                    next_run_at = ?, updated_at = ?, finished_at = ?
                WHERE id = ?
                """,
                (
                    status, error_type, error_message[:500], next_run_at,
                    now, finished_at, task_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM recognition_tasks WHERE id = ?", (task_id,)
            ).fetchone()
        return self._recognition_task_from_row(row)

    def cancel_recognition_task(self, task_id: str, now: str) -> dict[str, Any]:
        with self.connect() as connection:
            current = connection.execute(
                "SELECT status FROM recognition_tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if not current:
                raise LookupError("识别任务不存在")
            if current["status"] not in {"succeeded", "failed", "cancelled"}:
                immediate = current["status"] in {"pending", "retrying"}
                connection.execute(
                    """
                    UPDATE recognition_tasks
                    SET cancel_requested = 1,
                        status = CASE WHEN ? THEN 'cancelled' ELSE status END,
                        finished_at = CASE WHEN ? THEN ? ELSE finished_at END,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (immediate, immediate, now, now, task_id),
                )
            row = connection.execute(
                "SELECT * FROM recognition_tasks WHERE id = ?", (task_id,)
            ).fetchone()
        return self._recognition_task_from_row(row)

    def retry_recognition_task(self, task_id: str, now: str) -> dict[str, Any]:
        with self.connect() as connection:
            current = connection.execute(
                "SELECT status FROM recognition_tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if not current:
                raise LookupError("识别任务不存在")
            if current["status"] != "failed":
                raise TaskStateConflictError("只有失败任务可以手动重试")
            connection.execute(
                """
                UPDATE recognition_tasks
                SET status = 'pending', attempt_count = 0, cancel_requested = 0,
                    error_type = '', error_message = '', next_run_at = ?,
                    updated_at = ?, started_at = NULL, finished_at = NULL
                WHERE id = ?
                """,
                (now, now, task_id),
            )
            row = connection.execute(
                "SELECT * FROM recognition_tasks WHERE id = ?", (task_id,)
            ).fetchone()
        return self._recognition_task_from_row(row)

    def update_draft(
        self, draft_id: str, customer_id: int | None, items: list[dict[str, Any]],
        warnings: list[str], updated_at: str,
    ) -> dict[str, Any] | None:
        with self.connect() as connection:
            before_row = connection.execute(
                "SELECT * FROM drafts WHERE id = ? AND status = 'needs_review'",
                (draft_id,),
            ).fetchone()
            if not before_row:
                return None
            before = self._draft_from_row(before_row)
            result = connection.execute(
                """
                UPDATE drafts
                SET customer_id = ?, items_json = ?, warnings_json = ?, updated_at = ?
                WHERE id = ? AND status = 'needs_review'
                """,
                (customer_id, self._json(items), self._json(warnings), updated_at, draft_id),
            )
            corrections = self._diff_draft(
                draft_id,
                customer_id,
                before["customer_id"],
                before["items"],
                items,
                updated_at,
            )
            if corrections:
                connection.executemany(
                    """
                    INSERT INTO correction_events(
                        draft_id, customer_id, field_path, before_json,
                        after_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    corrections,
                )
            self._collect_alias_candidates(
                connection, before, items, updated_at
            )
        return self.get_draft(draft_id) if result.rowcount else None

    def confirm_draft(
        self, draft_id: str, created_at: str, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        with self.connect() as connection:
            draft_row = connection.execute(
                "SELECT * FROM drafts WHERE id = ?", (draft_id,)
            ).fetchone()
            if not draft_row:
                raise LookupError("草稿不存在")

            draft = self._draft_from_row(draft_row)
            resolved_key = idempotency_key or f"draft:{draft_id}"
            payload_md5 = self._order_payload_md5(draft)
            by_key = connection.execute(
                "SELECT id, payload_md5 FROM orders WHERE idempotency_key = ?",
                (resolved_key,),
            ).fetchone()
            if by_key:
                if by_key["payload_md5"] != payload_md5:
                    raise IdempotencyConflictError("幂等键已用于不同的订单载荷")
                order_id = by_key["id"]
            else:
                existing = connection.execute(
                    "SELECT id FROM orders WHERE draft_id = ?", (draft_id,)
                ).fetchone()
                if existing:
                    order_id = existing["id"]
                elif draft["status"] != "needs_review":
                    raise IdempotencyConflictError("草稿已经确认")
                else:
                    order_id = None
                if order_id is not None:
                    order_row = connection.execute(
                        """
                        SELECT o.*, c.name AS customer_name
                        FROM orders o JOIN customers c ON c.id = o.customer_id
                        WHERE o.id = ?
                        """,
                        (order_id,),
                    ).fetchone()
                    if order_row is None:
                        raise RuntimeError("幂等订单读取失败")
                    return self._order_from_row(order_row, connection)
                if draft["customer_id"] is None:
                    raise ValueError("请选择客户")
                if not draft["items"]:
                    raise ValueError("订单至少需要一条商品")
                customer = connection.execute(
                    "SELECT 1 FROM customers WHERE id = ?",
                    (draft["customer_id"],),
                ).fetchone()
                if not customer:
                    raise ValueError("客户不存在")

                checked_items = []
                for item in draft["items"]:
                    product_id = item.get("product_id")
                    product_row = connection.execute(
                        """
                        SELECT product_id AS id, sku, name, aliases_json, unit,
                               unit_price, active
                        FROM sku_snapshots
                        WHERE site_id = ? AND sku_version = ? AND product_id = ?
                        """,
                        (draft["site_id"], draft["sku_version"], product_id),
                    ).fetchone() if product_id else None
                    product = self._product_from_row(product_row) if product_row else None
                    if not product or not product["active"]:
                        raise ValueError(f"第 {item['line_no']} 行商品未匹配")
                    if (
                        not math.isfinite(item["quantity"])
                        or item["quantity"] <= 0
                        or item["quantity"] > 1_000_000
                    ):
                        raise ValueError(
                            f"第 {item['line_no']} 行数量必须大于 0 且不能超过 1000000"
                        )
                    if (
                        not math.isfinite(item["unit_price"])
                        or item["unit_price"] < 0
                        or item["unit_price"] > 1_000_000
                    ):
                        raise ValueError(
                            f"第 {item['line_no']} 行单价不能小于 0 且不能超过 1000000"
                        )
                    checked_items.append(item)

                total = round(sum(
                    round(item["quantity"] * item["unit_price"], 2)
                    for item in checked_items
                ), 2)
                time_part = created_at.replace("-", "").replace(":", "").replace("T", "")[:14]
                order_no = f"ORD-{time_part}-{uuid4().hex[:6].upper()}"
                cursor = connection.execute(
                    """
                    INSERT INTO orders(
                        order_no, draft_id, customer_id, source_text,
                        total_amount, status, site_id, sku_version, created_at
                        , idempotency_key, payload_md5, input_type, source_filename
                    ) VALUES (?, ?, ?, ?, ?, 'confirmed', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        order_no, draft_id, draft["customer_id"], draft["source_text"],
                        total, draft["site_id"], draft["sku_version"], created_at,
                        resolved_key, payload_md5,
                        draft.get("input_type", "text"), draft.get("source_filename", ""),
                    ),
                )
                order_id = cursor.lastrowid
                connection.executemany(
                    """
                    INSERT INTO order_items(
                        order_id, line_no, product_id, product_name, quantity,
                        unit, unit_price, note, subtotal
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            order_id, item["line_no"], item["product_id"],
                            item["product_name"], item["quantity"], item["unit"],
                            item["unit_price"], item.get("note", ""),
                            round(item["quantity"] * item["unit_price"], 2),
                        )
                        for item in checked_items
                    ],
                )
                connection.execute(
                    "UPDATE drafts SET status = 'confirmed', updated_at = ? WHERE id = ?",
                    (created_at, draft_id),
                )
                self._refresh_customer_preferences(
                    connection, draft["customer_id"], checked_items, created_at
                )
        order = self.get_order(order_id)
        if order is None:
            raise RuntimeError("订单写入后读取失败")
        return order

    def list_orders(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT o.*, c.name AS customer_name
                FROM orders o JOIN customers c ON c.id = o.customer_id
                ORDER BY o.id DESC
                """
            ).fetchall()
            item_rows = connection.execute(
                """
                SELECT order_id, line_no, product_id, product_name, quantity,
                       unit, unit_price, note, subtotal
                FROM order_items ORDER BY order_id, line_no
                """
            ).fetchall()
        items_by_order: dict[int, list[dict[str, Any]]] = {}
        for item_row in item_rows:
            item = dict(item_row)
            order_id = int(item.pop("order_id"))
            items_by_order.setdefault(order_id, []).append(item)
        return [
            self._order_from_row(row, items=items_by_order.get(int(row["id"]), []))
            for row in rows
        ]

    def get_order(self, order_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT o.*, c.name AS customer_name
                FROM orders o JOIN customers c ON c.id = o.customer_id
                WHERE o.id = ?
                """,
                (order_id,),
            ).fetchone()
            return self._order_from_row(row, connection) if row else None

    def get_order_by_draft(self, draft_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT o.*, c.name AS customer_name
                FROM orders o JOIN customers c ON c.id = o.customer_id
                WHERE o.draft_id = ?
                """,
                (draft_id,),
            ).fetchone()
            return self._order_from_row(row, connection) if row else None

    def get_order_by_idempotency_key(self, key: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT o.*, c.name AS customer_name
                FROM orders o JOIN customers c ON c.id = o.customer_id
                WHERE o.idempotency_key = ?
                """,
                (key,),
            ).fetchone()
            return self._order_from_row(row, connection) if row else None

    def list_corrections(self, draft_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, draft_id, customer_id, field_path, before_json,
                       after_json, created_at
                FROM correction_events WHERE draft_id = ? ORDER BY id
                """,
                (draft_id,),
            ).fetchall()
        def deserialize(row: sqlite3.Row) -> dict[str, Any]:
            event = dict(row)
            event["before"] = json.loads(event.pop("before_json"))
            event["after"] = json.loads(event.pop("after_json"))
            return event

        return list(map(deserialize, rows))

    def list_customer_preferences(self, customer_id: int) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT customer_id, preference_key, preference_value_json,
                       evidence_count, updated_at
                FROM customer_preferences WHERE customer_id = ?
                ORDER BY evidence_count DESC, preference_key
                """,
                (customer_id,),
            ).fetchall()
        def deserialize(row: sqlite3.Row) -> dict[str, Any]:
            preference = dict(row)
            preference["preference_value"] = json.loads(
                preference.pop("preference_value_json")
            )
            return preference

        return list(map(deserialize, rows))

    def list_alias_candidates(
        self, status: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        where = "WHERE c.status = ?" if status else ""
        params: tuple[Any, ...] = (status, limit) if status else (limit,)
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT c.id, c.site_id, c.product_id, p.name AS product_name,
                       c.alias, c.evidence_count, c.status, c.source_type,
                       c.created_at, c.updated_at, c.reviewed_at
                FROM catalog_alias_candidates c
                JOIN products p ON p.id = c.product_id
                {where}
                ORDER BY CASE c.status WHEN 'pending' THEN 0 ELSE 1 END,
                         c.evidence_count DESC, c.updated_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def get_alias_candidate(self, candidate_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT c.id, c.site_id, c.product_id, p.name AS product_name,
                       c.alias, c.evidence_count, c.status, c.source_type,
                       c.created_at, c.updated_at, c.reviewed_at
                FROM catalog_alias_candidates c
                JOIN products p ON p.id = c.product_id
                WHERE c.id = ?
                """,
                (candidate_id,),
            ).fetchone()
        return dict(row) if row else None

    def review_alias_candidate(
        self, candidate_id: int, decision: str
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        if decision not in {"approved", "rejected"}:
            raise ValueError("审核结果只能是 approved 或 rejected")
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        version = None
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM catalog_alias_candidates WHERE id = ?",
                (candidate_id,),
            ).fetchone()
            if not row:
                raise LookupError("别名候选不存在")
            if row["status"] != "pending":
                raise ValueError("该别名候选已经审核")
            result = connection.execute(
                """
                UPDATE catalog_alias_candidates
                SET status = ?, reviewed_at = ?, updated_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (decision, now, now, candidate_id),
            )
            if result.rowcount != 1:
                raise ValueError("该别名候选已被其他操作审核")
            if decision == "approved":
                product_row = connection.execute(
                    """
                    SELECT id, sku, name, aliases_json, unit, unit_price, active
                    FROM products WHERE id = ?
                    """,
                    (row["product_id"],),
                ).fetchone()
                if not product_row:
                    raise LookupError("候选关联商品不存在")
                before = self._product_from_row(product_row)
                aliases = list(before["aliases"])
                if row["alias"] not in aliases and row["alias"] != before["name"]:
                    aliases.append(row["alias"])
                    connection.execute(
                        "UPDATE products SET aliases_json = ? WHERE id = ?",
                        (self._json(aliases), row["product_id"]),
                    )
                after = {**before, "aliases": aliases}
                self._record_catalog_audit(
                    connection, int(row["site_id"]), "approve_alias",
                    "alias_candidate", str(candidate_id), "human_review",
                    before, after, now,
                )
                version = self._publish_catalog_locked(
                    connection, int(row["site_id"]), now
                )
            else:
                self._record_catalog_audit(
                    connection, int(row["site_id"]), "reject_alias",
                    "alias_candidate", str(candidate_id), "human_review",
                    {"status": "pending"}, {"status": "rejected"}, now,
                )
        candidate = self.get_alias_candidate(candidate_id)
        if candidate is None:
            raise RuntimeError("别名候选审核后读取失败")
        return candidate, version

    def get_business_metrics(self, site_id: int = 1) -> dict[str, Any]:
        with self.connect() as connection:
            task_summary = connection.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN status = 'succeeded' THEN 1 ELSE 0 END) AS completed
                FROM recognition_tasks
                WHERE site_id = ?
                """,
                (site_id,),
            ).fetchone()
            task_duration_rows = connection.execute(
                """
                SELECT started_at, finished_at FROM recognition_tasks
                WHERE site_id = ? AND started_at IS NOT NULL
                  AND finished_at IS NOT NULL
                """,
                (site_id,),
            ).fetchall()
            draft_count = int(connection.execute(
                "SELECT COUNT(*) FROM drafts WHERE site_id = ?",
                (site_id,),
            ).fetchone()[0])
            retrieval_summary = connection.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE
                           WHEN json_extract(item.value, '$.selected_product_id') IS NOT NULL
                           THEN 1 ELSE 0 END) AS matched
                FROM drafts AS draft
                JOIN json_each(
                    CASE WHEN json_valid(draft.retrieval_json)
                         THEN draft.retrieval_json ELSE '[]' END
                ) AS item
                WHERE draft.site_id = ?
                """,
                (site_id,),
            ).fetchone()
            confirmed_orders = int(connection.execute(
                "SELECT COUNT(*) FROM orders WHERE site_id = ?",
                (site_id,),
            ).fetchone()[0])
            corrected_drafts = int(connection.execute(
                """
                SELECT COUNT(DISTINCT c.draft_id)
                FROM correction_events c JOIN drafts d ON d.id = c.draft_id
                WHERE d.site_id = ?
                """,
                (site_id,),
            ).fetchone()[0])
            candidate_counts = {
                row["status"]: int(row["count"])
                for row in connection.execute(
                    """
                    SELECT status, COUNT(*) AS count FROM catalog_alias_candidates
                    WHERE site_id = ? GROUP BY status
                    """,
                    (site_id,),
                ).fetchall()
            }
            catalog_products = int(connection.execute(
                "SELECT COUNT(*) FROM products"
            ).fetchone()[0])
            active_products = int(connection.execute(
                "SELECT COUNT(*) FROM products WHERE active = 1"
            ).fetchone()[0])
            version_row = connection.execute(
                """
                SELECT version FROM sku_catalog_versions
                WHERE site_id = ? AND status = 'active'
                """,
                (site_id,),
            ).fetchone()
        if version_row is None:
            raise LookupError("站点不存在")
        total_items = int(retrieval_summary["total"] or 0)
        auto_matched = int(retrieval_summary["matched"] or 0)

        def task_duration(row: sqlite3.Row) -> int:
            started = datetime.fromisoformat(row["started_at"])
            finished = datetime.fromisoformat(row["finished_at"])
            return max(0, int((finished - started).total_seconds() * 1000))

        durations = sorted(map(
            task_duration, task_duration_rows
        ))
        total_tasks = int(task_summary["total"] or 0)
        completed_tasks = int(task_summary["completed"] or 0)
        unmatched = total_items - auto_matched
        return {
            "total_tasks": total_tasks,
            "completed_tasks": completed_tasks,
            "task_success_rate": self._percentage(completed_tasks, total_tasks),
            "confirmed_orders": confirmed_orders,
            "total_recognized_items": total_items,
            "auto_matched_items": auto_matched,
            "auto_match_rate": self._percentage(auto_matched, total_items),
            "unmatched_items": unmatched,
            "unmatched_rate": self._percentage(unmatched, total_items),
            "corrected_drafts": corrected_drafts,
            "correction_rate": self._percentage(corrected_drafts, draft_count),
            "pending_alias_candidates": candidate_counts.get("pending", 0),
            "approved_alias_candidates": candidate_counts.get("approved", 0),
            "catalog_products": catalog_products,
            "active_products": active_products,
            "current_sku_version": int(version_row["version"]),
            "average_task_duration_ms": round(
                sum(durations) / len(durations), 2
            ) if durations else 0.0,
            "p95_task_duration_ms": self._percentile_95(durations),
        }

    def record_llm_call(self, record: dict[str, Any]) -> dict[str, Any]:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO llm_call_records(
                    task_id, draft_id, provider, model, prompt_version,
                    prompt_tokens, completion_tokens, total_tokens,
                    estimated_cost, duration_ms, status, error_type,
                    fallback_reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["task_id"], record.get("draft_id"), record["provider"],
                    record["model"], record["prompt_version"],
                    record.get("prompt_tokens", 0),
                    record.get("completion_tokens", 0),
                    record.get("total_tokens", 0),
                    record.get("estimated_cost", 0.0),
                    record.get("duration_ms", 0), record["status"],
                    record.get("error_type", ""),
                    record.get("fallback_reason", ""), record["created_at"],
                ),
            )
            row = connection.execute(
                "SELECT * FROM llm_call_records WHERE id = ?",
                (cursor.lastrowid,),
            ).fetchone()
        if row is None:
            raise RuntimeError("模型调用记录写入后读取失败")
        return dict(row)

    def get_metrics_overview(self) -> dict[str, Any]:
        return self._metric_summary_sql()

    def list_llm_failures(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM llm_call_records
                WHERE status IN ('fallback', 'failed')
                ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                **dict(row),
                "fallback_reason": (
                    "外部模型调用失败，已回退本地规则"
                    if row["status"] == "fallback"
                    else "外部模型调用失败"
                ),
            }
            for row in rows
        ]

    def get_model_metrics(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            groups = connection.execute(
                """
                SELECT DISTINCT provider, model, prompt_version
                FROM llm_call_records
                ORDER BY provider, model, prompt_version
                """
            ).fetchall()
        return [
            {
                "provider": row["provider"],
                "model": row["model"],
                "prompt_version": row["prompt_version"],
                **self._metric_summary_sql(
                    "WHERE provider = ? AND model = ? AND prompt_version = ?",
                    (row["provider"], row["model"], row["prompt_version"]),
                ),
            }
            for row in groups
        ]

    def _metric_summary_sql(
        self, where_clause: str = "", parameters: tuple[Any, ...] = ()
    ) -> dict[str, Any]:
        with self.connect() as connection:
            summary = connection.execute(
                f"""
                SELECT COUNT(*) AS total_calls,
                       SUM(CASE WHEN status = 'succeeded' THEN 1 ELSE 0 END) AS success_count,
                       SUM(CASE WHEN status = 'fallback' THEN 1 ELSE 0 END) AS fallback_count,
                       SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failure_count,
                       AVG(duration_ms) AS average_duration_ms,
                       SUM(prompt_tokens) AS prompt_tokens,
                       SUM(completion_tokens) AS completion_tokens,
                       SUM(total_tokens) AS total_tokens,
                       SUM(estimated_cost) AS estimated_cost
                FROM llm_call_records {where_clause}
                """,
                parameters,
            ).fetchone()
            durations = [
                int(row["duration_ms"])
                for row in connection.execute(
                    f"""
                    SELECT duration_ms FROM llm_call_records
                    {where_clause} ORDER BY duration_ms
                    """,
                    parameters,
                ).fetchall()
            ]
        total_calls = int(summary["total_calls"] or 0)
        success_count = int(summary["success_count"] or 0)
        fallback_count = int(summary["fallback_count"] or 0)
        failure_count = int(summary["failure_count"] or 0)
        return {
            "total_calls": total_calls,
            "success_count": success_count,
            "fallback_count": fallback_count,
            "failure_count": failure_count,
            "success_rate": round(success_count / total_calls * 100, 2)
            if total_calls else 0.0,
            "fallback_rate": round(fallback_count / total_calls * 100, 2)
            if total_calls else 0.0,
            "average_duration_ms": round(
                float(summary["average_duration_ms"] or 0), 2
            ),
            "p95_duration_ms": self._percentile_95(durations),
            "prompt_tokens": int(summary["prompt_tokens"] or 0),
            "completion_tokens": int(summary["completion_tokens"] or 0),
            "total_tokens": int(summary["total_tokens"] or 0),
            "estimated_cost": round(float(summary["estimated_cost"] or 0), 8),
        }

    def _order_from_row(
        self,
        row: sqlite3.Row,
        connection: sqlite3.Connection | None = None,
        items: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        order = dict(row)
        if items is None and connection is not None:
            item_rows = connection.execute(
                """
                SELECT line_no, product_id, product_name, quantity, unit,
                       unit_price, note, subtotal
                FROM order_items WHERE order_id = ? ORDER BY line_no
                """,
                (order["id"],),
            ).fetchall()
            items = [dict(item) for item in item_rows]
        if items is None:
            with self.connect() as item_connection:
                item_rows = item_connection.execute(
                    """
                    SELECT line_no, product_id, product_name, quantity, unit,
                           unit_price, note, subtotal
                    FROM order_items WHERE order_id = ? ORDER BY line_no
                    """,
                    (order["id"],),
                ).fetchall()
            items = [dict(item) for item in item_rows]
        order["items"] = items
        return order

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False)

    @staticmethod
    def _draft_from_row(row: sqlite3.Row) -> dict[str, Any]:
        draft = dict(row)
        draft["items"] = json.loads(draft.pop("items_json"))
        draft["warnings"] = json.loads(draft.pop("warnings_json"))
        draft["trace"] = json.loads(draft.pop("trace_json"))
        draft["retrieval"] = json.loads(draft.pop("retrieval_json", "[]"))
        draft["preferences"] = json.loads(draft.pop("preferences_json", "[]"))
        draft["input_warnings"] = json.loads(draft.pop("input_warnings_json", "[]"))
        return draft

    @staticmethod
    def _recognition_task_from_row(row: sqlite3.Row) -> dict[str, Any]:
        task = dict(row)
        task["cancel_requested"] = bool(task["cancel_requested"])
        task["input_warnings"] = json.loads(task.pop("input_warnings_json", "[]"))
        if task["error_message"]:
            task["error_message"] = Database._public_task_error_message(
                task["error_message"]
            )
        return task

    @staticmethod
    def _public_task_error_message(message: str) -> str:
        return public_error(message, "识别暂时失败，请稍后重试")

    @staticmethod
    def _snapshot_md5(rows: list[dict[str, Any]]) -> str:
        payload = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.md5(payload.encode("utf-8"), usedforsecurity=False).hexdigest()

    @staticmethod
    def _order_payload_md5(draft: dict[str, Any]) -> str:
        canonical = {
            "customer_id": draft["customer_id"],
            "site_id": draft["site_id"],
            "sku_version": draft["sku_version"],
            "items": [
                {
                    "line_no": item["line_no"],
                    "product_id": item.get("product_id"),
                    "quantity": item["quantity"],
                    "unit": item["unit"],
                    "unit_price": item["unit_price"],
                    "note": item.get("note", ""),
                }
                for item in draft["items"]
            ],
        }
        payload = json.dumps(
            canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.md5(
            payload.encode("utf-8"), usedforsecurity=False
        ).hexdigest()

    @staticmethod
    def _ensure_column(
        connection: sqlite3.Connection, table: str, column: str, definition: str
    ) -> None:
        columns = {
            row["name"] for row in connection.execute(f"PRAGMA table_info({table})")
        }
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _migrate_existing_schema(self, connection: sqlite3.Connection) -> None:
        self._ensure_column(connection, "drafts", "site_id", "INTEGER NOT NULL DEFAULT 1")
        self._ensure_column(connection, "drafts", "sku_version", "INTEGER NOT NULL DEFAULT 1")
        self._ensure_column(connection, "drafts", "retrieval_json", "TEXT NOT NULL DEFAULT '[]'")
        self._ensure_column(connection, "drafts", "preferences_json", "TEXT NOT NULL DEFAULT '[]'")
        self._ensure_column(connection, "drafts", "input_type", "TEXT NOT NULL DEFAULT 'text'")
        self._ensure_column(connection, "drafts", "source_filename", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column(connection, "drafts", "input_confidence", "REAL")
        self._ensure_column(connection, "drafts", "input_warnings_json", "TEXT NOT NULL DEFAULT '[]'")
        self._ensure_column(connection, "orders", "site_id", "INTEGER NOT NULL DEFAULT 1")
        self._ensure_column(connection, "orders", "sku_version", "INTEGER NOT NULL DEFAULT 1")
        self._ensure_column(connection, "orders", "idempotency_key", "TEXT")
        self._ensure_column(connection, "orders", "payload_md5", "TEXT")
        self._ensure_column(connection, "orders", "input_type", "TEXT NOT NULL DEFAULT 'text'")
        self._ensure_column(connection, "orders", "source_filename", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column(connection, "recognition_tasks", "input_confidence", "REAL")
        self._ensure_column(connection, "recognition_tasks", "input_warnings_json", "TEXT NOT NULL DEFAULT '[]'")
        legacy_rows = connection.execute(
            "SELECT id, draft_id FROM orders WHERE idempotency_key IS NULL"
        ).fetchall()
        for row in legacy_rows:
            connection.execute(
                """
                UPDATE orders SET idempotency_key = ?, payload_md5 = ? WHERE id = ?
                """,
                (f"legacy:{row['draft_id']}", hashlib.md5(
                    row["draft_id"].encode("utf-8"), usedforsecurity=False
                ).hexdigest(), row["id"]),
            )
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_idempotency_key
            ON orders(idempotency_key)
            """
        )

    @staticmethod
    def _diff_draft(
        draft_id: str,
        customer_id: int | None,
        old_customer_id: int | None,
        old_items: list[dict[str, Any]],
        new_items: list[dict[str, Any]],
        created_at: str,
    ) -> list[tuple]:
        changes: list[tuple] = []

        def add(path: str, before: Any, after: Any) -> None:
            changes.append((
                draft_id,
                customer_id,
                path,
                json.dumps(before, ensure_ascii=False),
                json.dumps(after, ensure_ascii=False),
                created_at,
            ))

        if old_customer_id != customer_id:
            add("customer_id", old_customer_id, customer_id)
        old_by_line = {item["line_no"]: item for item in old_items}
        new_by_line = {item["line_no"]: item for item in new_items}
        for line_no in sorted(set(old_by_line) | set(new_by_line)):
            old_item = old_by_line.get(line_no)
            new_item = new_by_line.get(line_no)
            if old_item is None or new_item is None:
                add(f"items[{line_no}]", old_item, new_item)
                continue
            for field in ("product_id", "quantity", "unit", "unit_price", "note"):
                if old_item.get(field) != new_item.get(field):
                    add(
                        f"items[{line_no}].{field}",
                        old_item.get(field),
                        new_item.get(field),
                    )
        return changes

    @staticmethod
    def _refresh_customer_preferences(
        connection: sqlite3.Connection,
        customer_id: int,
        items: list[dict[str, Any]],
        updated_at: str,
    ) -> None:
        for item in items:
            note = item.get("note", "").strip()
            if not note:
                continue
            key = f"product:{item['product_id']}:note"
            value_json = json.dumps(note, ensure_ascii=False)
            existing = connection.execute(
                """
                SELECT preference_value_json FROM customer_preferences
                WHERE customer_id = ? AND preference_key = ?
                """,
                (customer_id, key),
            ).fetchone()
            if existing and existing["preference_value_json"] == value_json:
                connection.execute(
                    """
                    UPDATE customer_preferences
                    SET evidence_count = evidence_count + 1, updated_at = ?
                    WHERE customer_id = ? AND preference_key = ?
                    """,
                    (updated_at, customer_id, key),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO customer_preferences(
                        customer_id, preference_key, preference_value_json,
                        evidence_count, updated_at
                    ) VALUES (?, ?, ?, 1, ?)
                    ON CONFLICT(customer_id, preference_key) DO UPDATE SET
                        preference_value_json = excluded.preference_value_json,
                        evidence_count = 1,
                        updated_at = excluded.updated_at
                    """,
                    (customer_id, key, value_json, updated_at),
                )

    def _seed_initial_sku_snapshot(self, connection: sqlite3.Connection) -> None:
        existing = connection.execute(
            "SELECT 1 FROM sku_catalog_versions WHERE site_id = 1 LIMIT 1"
        ).fetchone()
        if existing:
            return
        rows = [
            {
                "product_id": product_id,
                "sku": sku,
                "name": name,
                "aliases_json": json.dumps(aliases, ensure_ascii=False),
                "unit": unit,
                "unit_price": price,
                "active": active,
            }
            for product_id, sku, name, aliases, unit, price, active in PRODUCTS
        ]
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        connection.execute(
            """
            INSERT INTO sku_catalog_versions(site_id, version, status, content_md5, created_at)
            VALUES (1, 1, 'active', ?, ?)
            """,
            (self._snapshot_md5(rows), now),
        )
        connection.executemany(
            """
            INSERT INTO sku_snapshots(
                site_id, sku_version, product_id, sku, name, aliases_json,
                unit, unit_price, active
            ) VALUES (1, 1, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    row["product_id"], row["sku"], row["name"], row["aliases_json"],
                    row["unit"], row["unit_price"], row["active"],
                )
                for row in rows
            ],
        )

    def _publish_catalog_locked(
        self, connection: sqlite3.Connection, site_id: int, created_at: str
    ) -> dict[str, Any]:
        current = connection.execute(
            """
            SELECT MAX(version) AS version FROM sku_catalog_versions
            WHERE site_id = ?
            """,
            (site_id,),
        ).fetchone()
        new_version = int(current["version"] or 0) + 1
        rows = connection.execute(
            """
            SELECT id AS product_id, sku, name, aliases_json, unit,
                   unit_price, active FROM products ORDER BY id
            """
        ).fetchall()
        if not rows:
            raise ValueError("商品目录不能为空")
        content_md5 = self._snapshot_md5([dict(row) for row in rows])
        connection.execute(
            "UPDATE sku_catalog_versions SET status = 'archived' WHERE site_id = ?",
            (site_id,),
        )
        connection.execute(
            """
            INSERT INTO sku_catalog_versions(
                site_id, version, status, content_md5, created_at
            ) VALUES (?, ?, 'active', ?, ?)
            """,
            (site_id, new_version, content_md5, created_at),
        )
        connection.executemany(
            """
            INSERT INTO sku_snapshots(
                site_id, sku_version, product_id, sku, name, aliases_json,
                unit, unit_price, active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    site_id, new_version, row["product_id"], row["sku"],
                    row["name"], row["aliases_json"], row["unit"],
                    row["unit_price"], row["active"],
                )
                for row in rows
            ],
        )
        return {
            "site_id": site_id,
            "version": new_version,
            "status": "active",
            "content_md5": content_md5,
            "created_at": created_at,
        }

    @staticmethod
    def _require_site_locked(
        connection: sqlite3.Connection, site_id: int
    ) -> None:
        if site_id < 1:
            raise ValueError("站点编号必须大于 0")
        row = connection.execute(
            "SELECT 1 FROM sku_catalog_versions WHERE site_id = ? LIMIT 1",
            (site_id,),
        ).fetchone()
        if row is None:
            raise LookupError("站点不存在")

    @staticmethod
    def _normalize_product_payload(payload: dict[str, Any]) -> dict[str, Any]:
        aliases = list(dict.fromkeys(
            normalized
            for alias in payload.get("aliases", [])
            if (normalized := str(alias).strip())
        ))
        sku = str(payload.get("sku", "")).strip()
        name = str(payload.get("name", "")).strip()
        unit = str(payload.get("unit", "")).strip()
        if not sku or not name or not unit:
            raise ValueError("SKU、商品名称和单位不能为空")
        raw_price = payload.get("unit_price")
        if raw_price is None or str(raw_price).strip() == "":
            raise ValueError("商品单价不能为空")
        try:
            unit_price = float(raw_price)
        except (TypeError, ValueError) as exc:
            raise ValueError("商品单价必须是数字") from exc
        if not math.isfinite(unit_price) or unit_price < 0 or unit_price > 1_000_000:
            raise ValueError("商品单价必须是 0 到 1000000 之间的有限数字")
        aliases = [alias for alias in aliases if alias != name]
        return {
            "sku": sku,
            "name": name,
            "aliases": aliases,
            "unit": unit,
            "unit_price": unit_price,
            "active": bool(payload.get("active", True)),
        }

    @staticmethod
    def _record_catalog_audit(
        connection: sqlite3.Connection,
        site_id: int,
        action: str,
        entity_type: str,
        entity_id: str,
        source_type: str,
        before: Any,
        after: Any,
        created_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO catalog_audit_logs(
                site_id, action, entity_type, entity_id, source_type,
                before_json, after_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                site_id, action, entity_type, entity_id, source_type,
                json.dumps(before, ensure_ascii=False),
                json.dumps(after, ensure_ascii=False), created_at,
            ),
        )

    @staticmethod
    def _collect_alias_candidates(
        connection: sqlite3.Connection,
        before: dict[str, Any],
        after_items: list[dict[str, Any]],
        updated_at: str,
    ) -> None:
        old_by_line = {item["line_no"]: item for item in before["items"]}
        changed_items = [
            item for item in after_items
            if item.get("product_id")
            and old_by_line.get(item["line_no"])
            and old_by_line[item["line_no"]].get("product_id") != item["product_id"]
        ]
        product_ids = {int(item["product_id"]) for item in changed_items}
        if not product_ids:
            return
        placeholders = ",".join("?" for _ in product_ids)
        product_by_id = {
            int(row["id"]): {
                "name": row["name"],
                "aliases": set(json.loads(row["aliases_json"])),
            }
            for row in connection.execute(
                f"SELECT id, name, aliases_json FROM products WHERE id IN ({placeholders})",
                tuple(product_ids),
            ).fetchall()
        }

        def candidate_tuple(item: dict[str, Any]) -> tuple | None:
            old_item = old_by_line.get(item["line_no"])
            product_id = int(item["product_id"])
            alias = str(old_item.get("raw_product_name", "")).strip()
            product = product_by_id.get(product_id)
            if not alias or not product:
                return None
            if alias == product["name"] or alias in product["aliases"]:
                return None
            return (before["site_id"], product_id, alias, updated_at, updated_at)

        candidates = list(filter(None, map(candidate_tuple, changed_items)))
        connection.executemany(
            """
            INSERT INTO catalog_alias_candidates(
                site_id, product_id, alias, evidence_count, status,
                source_type, created_at, updated_at
            ) VALUES (?, ?, ?, 1, 'pending', 'human_correction', ?, ?)
            ON CONFLICT(site_id, product_id, alias) DO UPDATE SET
                evidence_count = catalog_alias_candidates.evidence_count + 1,
                updated_at = excluded.updated_at
            """,
            candidates,
        )

    @staticmethod
    def _percentage(numerator: int, denominator: int) -> float:
        return round(numerator / denominator * 100, 2) if denominator else 0.0

    @staticmethod
    def _percentile_95(values: list[int]) -> int:
        if not values:
            return 0
        index = max(0, math.ceil(len(values) * 0.95) - 1)
        return int(values[index])

    @staticmethod
    def _product_from_row(row: sqlite3.Row) -> dict[str, Any]:
        product = dict(row)
        product["aliases"] = json.loads(product.pop("aliases_json"))
        product["active"] = bool(product["active"])
        return product
