from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from ..database import Database
from .errors import (
    AgentIdempotencyConflict,
    AgentSessionNotFound,
    AgentStateConflict,
)


SESSION_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_sessions (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (
        status IN ('active', 'waiting_input', 'waiting_approval', 'completed', 'failed')
    ),
    site_id INTEGER NOT NULL,
    customer_id INTEGER,
    current_draft_id TEXT,
    current_order_id INTEGER,
    pending_action_json TEXT NOT NULL DEFAULT 'null',
    context_summary TEXT NOT NULL DEFAULT '',
    summary_through_sequence INTEGER NOT NULL DEFAULT 0,
    last_message TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1,
    next_sequence INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (customer_id) REFERENCES customers(id),
    FOREIGN KEY (current_draft_id) REFERENCES drafts(id),
    FOREIGN KEY (current_order_id) REFERENCES orders(id)
);

CREATE TABLE IF NOT EXISTS agent_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    type TEXT NOT NULL,
    actor TEXT NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    summary TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES agent_sessions(id) ON DELETE CASCADE,
    UNIQUE(session_id, sequence)
);

CREATE INDEX IF NOT EXISTS idx_agent_events_session_sequence
ON agent_events(session_id, sequence);

CREATE TABLE IF NOT EXISTS agent_requests (
    session_id TEXT NOT NULL,
    request_key_digest TEXT NOT NULL,
    operation TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('in_progress', 'completed')),
    response_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (session_id, request_key_digest, operation),
    FOREIGN KEY (session_id) REFERENCES agent_sessions(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS agent_sku_delegations (
    session_id TEXT NOT NULL,
    draft_fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (session_id, draft_fingerprint),
    FOREIGN KEY (session_id) REFERENCES agent_sessions(id) ON DELETE CASCADE
);
"""


class AgentSessionStore:
    def __init__(self, database: Database):
        self._database = database

    def initialize(self) -> None:
        with self._database.connect() as connection:
            connection.executescript(SESSION_SCHEMA)
            self._migrate_agent_requests(connection)

    def create(self, site_id: int, customer_id: int | None) -> dict[str, Any]:
        now = _now()
        session_id = str(uuid4())
        with self._database.connect() as connection:
            connection.execute(
                """
                INSERT INTO agent_sessions(
                    id, status, site_id, customer_id, created_at, updated_at
                ) VALUES (?, 'active', ?, ?, ?, ?)
                """,
                (session_id, site_id, customer_id, now, now),
            )
        self.append_event(
            session_id,
            event_type="system",
            actor="harness",
            name="session_created",
            status="completed",
            summary="Agent 会话已创建",
            payload={"site_id": site_id, "customer_id": customer_id},
        )
        return self.get(session_id)

    def get(
        self,
        session_id: str,
        after_sequence: int | None = None,
        event_limit: int = 100,
    ) -> dict[str, Any]:
        limit = max(1, min(event_limit, 200))
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM agent_sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if row is None:
                raise AgentSessionNotFound("Agent 会话不存在")
            if after_sequence is None:
                events = connection.execute(
                    """
                    SELECT * FROM agent_events WHERE session_id = ?
                    ORDER BY sequence DESC LIMIT ?
                    """,
                    (session_id, limit + 1),
                ).fetchall()
                has_more = len(events) > limit
                events = list(reversed(events[:limit]))
            else:
                events = connection.execute(
                    """
                    SELECT * FROM agent_events
                    WHERE session_id = ? AND sequence > ?
                    ORDER BY sequence ASC LIMIT ?
                    """,
                    (session_id, after_sequence, limit + 1),
                ).fetchall()
                has_more = len(events) > limit
                events = events[:limit]
        result = dict(row)
        result["pending_action"] = json.loads(result.pop("pending_action_json"))
        result["last_event_sequence"] = int(result.pop("next_sequence")) - 1
        result["has_more_events"] = has_more
        result["events"] = [self._event_from_row(item) for item in events]
        return result

    def update(
        self,
        session_id: str,
        expected_version: int | None = None,
        **changes: Any,
    ) -> dict[str, Any]:
        allowed = {
            "status", "customer_id", "current_draft_id", "current_order_id",
            "pending_action", "context_summary", "summary_through_sequence",
            "last_message",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"不支持的会话字段: {sorted(unknown)}")
        assignments: list[str] = []
        values: list[Any] = []
        for key, value in changes.items():
            column = "pending_action_json" if key == "pending_action" else key
            assignments.append(f"{column} = ?")
            values.append(
                json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                if key == "pending_action" else value
            )
        assignments.extend(["version = version + 1", "updated_at = ?"])
        values.append(_now())
        where = "id = ?"
        values.append(session_id)
        if expected_version is not None:
            where += " AND version = ?"
            values.append(expected_version)
        with self._database.connect() as connection:
            result = connection.execute(
                f"UPDATE agent_sessions SET {', '.join(assignments)} WHERE {where}",
                values,
            )
            if result.rowcount != 1:
                exists = connection.execute(
                    "SELECT 1 FROM agent_sessions WHERE id = ?", (session_id,)
                ).fetchone()
                if exists is None:
                    raise AgentSessionNotFound("Agent 会话不存在")
                raise AgentStateConflict("Agent 会话已被其他请求更新")
        return self.get(session_id)

    def append_event(
        self,
        session_id: str,
        event_type: str,
        actor: str,
        name: str,
        status: str,
        summary: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            sequence_row = connection.execute(
                """
                UPDATE agent_sessions
                SET next_sequence = next_sequence + 1
                WHERE id = ?
                RETURNING next_sequence - 1
                """,
                (session_id,),
            ).fetchone()
            if sequence_row is None:
                raise AgentSessionNotFound("Agent 会话不存在")
            sequence = int(sequence_row[0])
            cursor = connection.execute(
                """
                INSERT INTO agent_events(
                    session_id, sequence, type, actor, name, status,
                    summary, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id, sequence, event_type, actor, name, status,
                    summary[:500],
                    json.dumps(
                        _redact(payload or {}),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    _now(),
                ),
            )
            row = connection.execute(
                "SELECT * FROM agent_events WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        return self._event_from_row(row)

    def events_between(
        self, session_id: str, first: int, last: int
    ) -> list[dict[str, Any]]:
        with self._database.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM agent_events
                WHERE session_id = ? AND sequence BETWEEN ? AND ?
                ORDER BY sequence
                """,
                (session_id, first, last),
            ).fetchall()
        return [self._event_from_row(row) for row in rows]

    def begin_request(
        self, session_id: str, request_key: str | None, operation: str, payload: Any
    ) -> tuple[str | None, dict[str, Any] | None, bool]:
        if not request_key:
            return None, None, False
        key_digest = _request_key_digest(session_id, operation, request_key)
        payload_digest = _payload_digest(payload)
        now = _now()
        with self._database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            exists = connection.execute(
                "SELECT 1 FROM agent_sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if exists is None:
                raise AgentSessionNotFound("Agent 会话不存在")
            row = connection.execute(
                """
                SELECT payload_digest, status, response_json FROM agent_requests
                WHERE session_id = ? AND request_key_digest = ? AND operation = ?
                """,
                (session_id, key_digest, operation),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO agent_requests(
                        session_id, request_key_digest, operation, payload_digest,
                        status, response_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'in_progress', NULL, ?, ?)
                    """,
                    (session_id, key_digest, operation, payload_digest, now, now),
                )
                return key_digest, None, False
        if not _payload_matches(row["payload_digest"], payload, payload_digest):
            raise AgentIdempotencyConflict("幂等键已用于不同的请求载荷")
        response = (
            json.loads(row["response_json"])
            if row["status"] == "completed" and row["response_json"]
            else None
        )
        return key_digest, response, row["status"] == "in_progress"

    def complete_request(
        self,
        session_id: str,
        request_key_digest: str | None,
        operation: str,
        payload: Any,
        response: dict[str, Any],
    ) -> None:
        if not request_key_digest:
            return
        payload_digest = _payload_digest(payload)
        with self._database.connect() as connection:
            row = connection.execute(
                """
                SELECT payload_digest FROM agent_requests
                WHERE session_id = ? AND request_key_digest = ? AND operation = ?
                """,
                (session_id, request_key_digest, operation),
            ).fetchone()
            if row is None or not _payload_matches(
                row["payload_digest"], payload, payload_digest
            ):
                raise AgentIdempotencyConflict("幂等请求占位不存在或载荷已变化")
            connection.execute(
                """
                UPDATE agent_requests
                SET status = 'completed', response_json = ?, updated_at = ?
                WHERE session_id = ? AND request_key_digest = ? AND operation = ?
                """,
                (
                    json.dumps(response, ensure_ascii=False, separators=(",", ":")),
                    _now(), session_id, request_key_digest, operation,
                ),
            )

    def reserve_sku_delegation(
        self, session_id: str, draft_fingerprint: str
    ) -> bool:
        with self._database.connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO agent_sku_delegations(
                    session_id, draft_fingerprint, created_at
                ) VALUES (?, ?, ?)
                """,
                (session_id, draft_fingerprint, _now()),
            )
        return cursor.rowcount == 1

    def assert_draft_customer_change_allowed(
        self, draft_id: str, customer_id: int | None
    ) -> None:
        with self._database.connect() as connection:
            rows = connection.execute(
                """
                SELECT customer_id FROM agent_sessions
                WHERE current_draft_id = ? AND status != 'completed'
                """,
                (draft_id,),
            ).fetchall()
        if any(row["customer_id"] != customer_id for row in rows):
            raise AgentStateConflict("Harness 已绑定的草稿不能切换客户")

    @staticmethod
    def _migrate_agent_requests(connection: sqlite3.Connection) -> None:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(agent_requests)").fetchall()
        }
        has_current_legacy = "request_key" in columns
        has_orphan_legacy = "agent_requests_legacy" in tables
        if not has_current_legacy and not has_orphan_legacy:
            return
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute("PRAGMA secure_delete = ON")
            if has_current_legacy:
                if has_orphan_legacy:
                    raise RuntimeError("检测到冲突的 Harness 幂等迁移表")
                connection.execute(
                    "ALTER TABLE agent_requests RENAME TO agent_requests_legacy"
                )
                connection.execute(
                    """
                    CREATE TABLE agent_requests (
                        session_id TEXT NOT NULL,
                        request_key_digest TEXT NOT NULL,
                        operation TEXT NOT NULL,
                        payload_digest TEXT NOT NULL,
                        status TEXT NOT NULL CHECK (
                            status IN ('in_progress', 'completed')
                        ),
                        response_json TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (session_id, request_key_digest, operation),
                        FOREIGN KEY (session_id) REFERENCES agent_sessions(id)
                            ON DELETE CASCADE
                    )
                    """
                )
            AgentSessionStore._copy_legacy_request_rows(connection)
            connection.execute("DROP TABLE agent_requests_legacy")
            connection.commit()
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            connection.rollback()
            raise

    @staticmethod
    def _copy_legacy_request_rows(connection: sqlite3.Connection) -> None:
        legacy_rows = connection.execute(
            "SELECT * FROM agent_requests_legacy"
        ).fetchall()
        for row in legacy_rows:
            key_digest = _request_key_digest(
                row["session_id"], row["operation"], row["request_key"]
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO agent_requests(
                    session_id, request_key_digest, operation, payload_digest,
                    status, response_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'completed', ?, ?, ?)
                """,
                (
                    row["session_id"], key_digest, row["operation"],
                    f"legacy-md5:{row['payload_md5']}", row["response_json"],
                    row["created_at"], row["created_at"],
                ),
            )

    @staticmethod
    def _event_from_row(row: Any) -> dict[str, Any]:
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        result.pop("session_id", None)
        return result


def _canonical_payload(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _payload_digest(payload: Any) -> str:
    canonical = _canonical_payload(payload)
    material = f"agent-request-payload:v1\0{canonical}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _legacy_payload_md5(payload: Any) -> str:
    canonical = _canonical_payload(payload)
    return hashlib.md5(canonical.encode("utf-8"), usedforsecurity=False).hexdigest()


def _payload_matches(stored: str, payload: Any, current: str) -> bool:
    if stored.startswith("legacy-md5:"):
        return stored == f"legacy-md5:{_legacy_payload_md5(payload)}"
    return stored == current


def _request_key_digest(session_id: str, operation: str, request_key: str) -> str:
    material = (
        f"agent-request-key:v1\0{session_id}\0{operation}\0{request_key}"
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


_REDACTED_NORMALIZED_KEYS = {
    "apikey", "authorization", "idempotencykey", "payloadmd5",
    "payloadsha256", "payloaddigest", "requestkey", "requestkeydigest",
    "error", "errormessage", "errortype", "exception", "internalerror",
    "internaldetail", "stack", "traceback",
}


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _redact(item)
            for key, item in value.items()
            if _normalized_key(key) not in _REDACTED_NORMALIZED_KEYS
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _normalized_key(key: str) -> str:
    return "".join(character for character in key.lower() if character.isalnum())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")
