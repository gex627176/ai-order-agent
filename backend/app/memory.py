from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .database import Database


MEMORY_TEXT_LIMIT = 2000
MEMORY_ITEM_LIMIT = 10


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _value_hash(value_json: str) -> str:
    material = f"customer-memory-value:v1\0{value_json}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _record_observation(
    connection: sqlite3.Connection,
    *,
    site_id: int,
    customer_id: int,
    subject_type: str,
    subject_id: int | None,
    memory_key: str,
    value: Any,
    source_type: str,
    source_id: str,
    source_path: str,
    observed_at: str,
) -> None:
    value_json = _canonical(value)
    digest = _value_hash(value_json)
    connection.execute(
        """
        INSERT INTO customer_memory_facts(
            site_id, customer_id, subject_type, subject_id, memory_key,
            value_json, value_hash, status, evidence_count,
            first_seen_at, last_seen_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'observed', 0, ?, ?)
        ON CONFLICT(
            site_id, customer_id, subject_type, subject_id, memory_key, value_hash
        ) DO NOTHING
        """,
        (
            site_id, customer_id, subject_type, subject_id, memory_key,
            value_json, digest, observed_at, observed_at,
        ),
    )
    fact = connection.execute(
        """
        SELECT id FROM customer_memory_facts
        WHERE site_id = ? AND customer_id = ? AND subject_type = ?
          AND subject_id IS ? AND memory_key = ? AND value_hash = ?
        """,
        (site_id, customer_id, subject_type, subject_id, memory_key, digest),
    ).fetchone()
    if fact is None:
        raise RuntimeError("客户记忆事实写入失败")
    inserted = connection.execute(
        """
        INSERT OR IGNORE INTO customer_memory_evidence(
            memory_id, source_type, source_id, source_path, observed_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (fact["id"], source_type, source_id, source_path, observed_at),
    )
    if inserted.rowcount:
        connection.execute(
            """
            UPDATE customer_memory_facts
            SET evidence_count = evidence_count + 1, last_seen_at = ?
            WHERE id = ?
            """,
            (observed_at, fact["id"]),
        )


def record_confirmed_order_memories(
    connection: sqlite3.Connection,
    *,
    order_id: int,
    site_id: int,
    customer_id: int,
    items: list[dict[str, Any]],
    observed_at: str,
) -> None:
    """Record confirmed observations without turning them into automatic rules."""
    for item in items:
        note = str(item.get("note", "")).strip()
        if not note:
            continue
        _record_observation(
            connection,
            site_id=site_id,
            customer_id=customer_id,
            subject_type="product",
            subject_id=int(item["product_id"]),
            memory_key="preparation_note",
            value=note,
            source_type="confirmed_order",
            source_id=str(order_id),
            source_path=f"items[{item['line_no']}].note",
            observed_at=observed_at,
        )


def migrate_confirmed_order_memories(connection: sqlite3.Connection) -> None:
    rows = connection.execute(
        """
        SELECT o.id AS order_id, o.site_id, o.customer_id, o.created_at,
               i.line_no, i.product_id, i.note
        FROM orders o
        JOIN order_items i ON i.order_id = o.id
        WHERE o.status = 'confirmed' AND TRIM(i.note) != ''
        ORDER BY o.id, i.line_no
        """
    ).fetchall()
    for row in rows:
        _record_observation(
            connection,
            site_id=int(row["site_id"]),
            customer_id=int(row["customer_id"]),
            subject_type="product",
            subject_id=int(row["product_id"]),
            memory_key="preparation_note",
            value=str(row["note"]).strip(),
            source_type="confirmed_order",
            source_id=str(row["order_id"]),
            source_path=f"items[{row['line_no']}].note",
            observed_at=str(row["created_at"]),
        )


def migrate_legacy_customer_preferences(connection: sqlite3.Connection) -> None:
    rows = connection.execute(
        """
        SELECT customer_id, preference_key, preference_value_json,
               evidence_count, updated_at
        FROM customer_preferences
        """
    ).fetchall()
    for row in rows:
        parts = str(row["preference_key"]).split(":")
        if len(parts) != 3 or parts[0] != "product" or parts[2] != "note":
            continue
        try:
            product_id = int(parts[1])
            value = json.loads(row["preference_value_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        value_json = _canonical(value)
        digest = _value_hash(value_json)
        inserted = connection.execute(
            """
            INSERT INTO customer_memory_facts(
                site_id, customer_id, subject_type, subject_id, memory_key,
                value_json, value_hash, status, evidence_count,
                first_seen_at, last_seen_at
            ) VALUES (1, ?, 'product', ?, 'preparation_note', ?, ?,
                      'observed', ?, ?, ?)
            ON CONFLICT(
                site_id, customer_id, subject_type, subject_id, memory_key, value_hash
            ) DO NOTHING
            """,
            (
                row["customer_id"], product_id, value_json, digest,
                max(1, int(row["evidence_count"])),
                row["updated_at"], row["updated_at"],
            ),
        )
        fact = connection.execute(
            """
            SELECT id FROM customer_memory_facts
            WHERE site_id = 1 AND customer_id = ? AND subject_type = 'product'
              AND subject_id = ? AND memory_key = 'preparation_note'
              AND value_hash = ?
            """,
            (row["customer_id"], product_id, digest),
        ).fetchone()
        if fact is not None and inserted.rowcount:
            connection.execute(
                """
                INSERT OR IGNORE INTO customer_memory_evidence(
                    memory_id, source_type, source_id, source_path, observed_at
                ) VALUES (?, 'legacy_preference', ?, ?, ?)
                """,
                (
                    fact["id"],
                    f"{row['customer_id']}:{row['preference_key']}",
                    row["preference_key"], row["updated_at"],
                ),
            )


class CustomerMemoryService:
    def __init__(self, database: Database):
        self._database = database

    def list_relevant(
        self,
        site_id: int,
        customer_id: int,
        product_ids: list[int] | None = None,
        limit: int = MEMORY_ITEM_LIMIT,
    ) -> list[dict[str, Any]]:
        bounded_limit = max(1, min(limit, MEMORY_ITEM_LIMIT))
        products = sorted({int(item) for item in (product_ids or [])})
        where = ["site_id = ?", "customer_id = ?", "status != 'rejected'"]
        params: list[Any] = [site_id, customer_id]
        if products:
            placeholders = ",".join("?" for _ in products)
            where.append(f"subject_id IN ({placeholders})")
            params.extend(products)
        params.append(bounded_limit)
        with self._database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT id, site_id, customer_id, subject_type, subject_id,
                       memory_key, value_json, status, evidence_count,
                       first_seen_at, last_seen_at
                FROM customer_memory_facts
                WHERE {' AND '.join(where)}
                ORDER BY CASE status WHEN 'approved' THEN 0 ELSE 1 END,
                         evidence_count DESC, last_seen_at DESC, id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [self._fact_from_row(row) for row in rows]

    def list_compatibility_preferences(
        self, customer_id: int
    ) -> list[dict[str, Any]]:
        with self._database.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, site_id, customer_id, subject_type, subject_id,
                       memory_key, value_json, status, evidence_count,
                       first_seen_at, last_seen_at
                FROM customer_memory_facts
                WHERE customer_id = ? AND status != 'rejected'
                  AND subject_type = 'product'
                  AND memory_key = 'preparation_note'
                ORDER BY subject_id,
                         CASE status WHEN 'approved' THEN 0 ELSE 1 END,
                         evidence_count DESC, last_seen_at DESC, id DESC
                """,
                (customer_id,),
            ).fetchall()
        selected: dict[tuple[int, int], dict[str, Any]] = {}
        for row in rows:
            key = (int(row["site_id"]), int(row["subject_id"]))
            selected.setdefault(key, self._fact_from_row(row))
        return [
            {
                "customer_id": fact["customer_id"],
                "preference_key": f"product:{fact['subject_id']}:note",
                "preference_value": fact["value"],
                "evidence_count": fact["evidence_count"],
                "updated_at": fact["last_seen_at"],
            }
            for fact in selected.values()
        ]

    def for_session(self, session: dict[str, Any]) -> list[dict[str, Any]]:
        customer_id = session.get("customer_id")
        if customer_id is None:
            return []
        product_ids: list[int] = []
        draft_id = session.get("current_draft_id")
        if draft_id:
            draft = self._database.get_draft(str(draft_id))
            if draft:
                product_ids = [
                    int(item["product_id"])
                    for item in draft.get("items", [])
                    if item.get("product_id") is not None
                ]
                if not product_ids:
                    return []
        return self.list_relevant(
            int(session["site_id"]), int(customer_id), product_ids or None
        )

    @staticmethod
    def project_for_model(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        projected: list[dict[str, Any]] = []
        size = 0
        for fact in facts[:MEMORY_ITEM_LIMIT]:
            item = {
                "subject_type": fact["subject_type"],
                "subject_id": fact["subject_id"],
                "key": fact["memory_key"],
                "value": fact["value"],
                "status": fact["status"],
                "evidence_count": fact["evidence_count"],
                "usage": "仅供人工核对，不得覆盖本次输入或本地目录事实",
            }
            encoded = _canonical(item)
            if size + len(encoded) > MEMORY_TEXT_LIMIT:
                break
            projected.append(item)
            size += len(encoded)
        return projected

    @staticmethod
    def _fact_from_row(row: sqlite3.Row) -> dict[str, Any]:
        fact = dict(row)
        fact["value"] = json.loads(fact.pop("value_json"))
        return fact
