import json

import pytest
from fastapi.testclient import TestClient

from app.agent_runtime.errors import AgentStateConflict
from app.database import Database
from app.main import create_app
from app.memory import CustomerMemoryService


def _draft_with_note(client: TestClient, customer_id: int, note: str) -> dict:
    draft = client.post(
        "/api/drafts/recognize",
        json={"customer_id": customer_id, "site_id": 1, "text": "胡萝卜1公斤"},
    ).json()
    item = {**draft["items"][0], "note": note}
    return client.put(
        f"/api/drafts/{draft['id']}",
        json={"customer_id": customer_id, "items": [item]},
    ).json()


def test_business_memory_requires_confirmation_and_preserves_conflicts(tmp_path):
    path = tmp_path / "memory.db"
    app = create_app(str(path))
    with TestClient(app) as client:
        unconfirmed = _draft_with_note(client, 1, "切片")
        assert CustomerMemoryService(app.state.database).list_relevant(1, 1) == []

        first = _draft_with_note(client, 1, "切丁")
        first_order = client.post(
            f"/api/drafts/{first['id']}/confirm",
            headers={"Idempotency-Key": "memory-first"},
        )
        replay = client.post(
            f"/api/drafts/{first['id']}/confirm",
            headers={"Idempotency-Key": "memory-first"},
        )
        assert first_order.status_code == replay.status_code == 200

        second = _draft_with_note(client, 1, "切丝")
        assert client.post(
            f"/api/drafts/{second['id']}/confirm",
            headers={"Idempotency-Key": "memory-second"},
        ).status_code == 200

        third = _draft_with_note(client, 1, "切丁")
        assert client.post(
            f"/api/drafts/{third['id']}/confirm",
            headers={"Idempotency-Key": "memory-third"},
        ).status_code == 200

        service = CustomerMemoryService(app.state.database)
        facts = service.list_relevant(1, 1, [2])
        assert {fact["value"] for fact in facts} == {"切丁", "切丝"}
        counts = {fact["value"]: fact["evidence_count"] for fact in facts}
        assert counts == {"切丁": 2, "切丝": 1}
        assert service.list_relevant(1, 2) == []

        with app.state.database.connect() as connection:
            evidence = connection.execute(
                "SELECT source_id FROM customer_memory_evidence ORDER BY id"
            ).fetchall()
        assert len(evidence) == 3
        assert unconfirmed["status"] == "needs_review"


def test_legacy_preferences_migrate_once_and_keep_compatibility_api(tmp_path):
    path = tmp_path / "legacy-memory.db"
    database = Database(str(path))
    database.initialize()
    with database.connect() as connection:
        connection.execute(
            """
            INSERT INTO customer_preferences(
                customer_id, preference_key, preference_value_json,
                evidence_count, updated_at
            ) VALUES (1, 'product:2:note', '"切丁"', 3, '2026-01-01T00:00:00')
            """
        )
    database.initialize()
    database.initialize()

    preferences = database.list_customer_preferences(1)
    assert preferences == [{
        "customer_id": 1,
        "preference_key": "product:2:note",
        "preference_value": "切丁",
        "evidence_count": 3,
        "updated_at": "2026-01-01T00:00:00",
    }]
    with database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM customer_memory_facts"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM customer_memory_evidence"
        ).fetchone()[0] == 1


def test_confirmed_order_history_is_backfilled_idempotently(tmp_path):
    path = tmp_path / "history-memory.db"
    app = create_app(str(path))
    with TestClient(app) as client:
        draft = _draft_with_note(client, 1, "去皮")
        assert client.post(
            f"/api/drafts/{draft['id']}/confirm",
            headers={"Idempotency-Key": "history-memory"},
        ).status_code == 200
    database = Database(str(path))
    with database.connect() as connection:
        connection.execute("DELETE FROM customer_memory_evidence")
        connection.execute("DELETE FROM customer_memory_facts")

    database.initialize()
    database.initialize()

    facts = CustomerMemoryService(database).list_relevant(1, 1, [2])
    assert [(fact["value"], fact["evidence_count"]) for fact in facts] == [
        ("去皮", 1)
    ]
    with database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM customer_memory_evidence"
        ).fetchone()[0] == 1


def test_structured_session_memory_and_business_projection_are_bounded(tmp_path):
    app = create_app(str(tmp_path / "session-memory.db"))
    with TestClient(app) as client:
        draft = _draft_with_note(client, 1, "切丁")
        assert client.post(
            f"/api/drafts/{draft['id']}/confirm",
            headers={"Idempotency-Key": "memory-context"},
        ).status_code == 200

        session = client.post(
            "/api/agent/sessions", json={"site_id": 1, "customer_id": 1}
        ).json()
        for index in range(5):
            response = client.post(
                f"/api/agent/sessions/{session['id']}/messages",
                json={"content": "有哪些商品？"},
                headers={"Idempotency-Key": f"memory-query-{index}"},
            )
            assert response.status_code == 200

        loop = app.state.agent_loop
        current = loop.store.get(session["id"])
        memory = loop.store.get_memory(session["id"])
        context = loop.context.assemble(current, loop.store)

    assert memory["through_sequence"] > 0
    assert memory["objective"] == "有哪些商品？"
    assert isinstance(memory["milestones"], list)
    assert len(memory["milestones"]) <= 8
    assert context["session_memory"]["through_sequence"] > 0
    assert context["business_memory"][0]["value"] == "切丁"
    assert "不得覆盖" in context["business_memory"][0]["usage"]
    assert len(json.dumps(context["business_memory"], ensure_ascii=False)) <= 2200


def test_session_memory_compaction_rolls_back_on_version_conflict(tmp_path):
    app = create_app(str(tmp_path / "memory-conflict.db"))
    with TestClient(app) as client:
        session = client.post(
            "/api/agent/sessions", json={"site_id": 1, "customer_id": 1}
        ).json()
        loop = app.state.agent_loop
        before = loop.store.get_memory(session["id"])
        with pytest.raises(AgentStateConflict):
            loop.store.save_compaction(
                session["id"],
                expected_session_version=session["version"] + 99,
                through_sequence=10,
                objective="不应保存",
                recent_intent="不应保存",
                unresolved=[],
                milestones=[],
                context_summary="不应保存",
            )
        after = loop.store.get_memory(session["id"])

    assert after == before
