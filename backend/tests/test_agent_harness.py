from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
import hashlib
import json
from pathlib import Path
import sqlite3
from threading import Event

from fastapi.testclient import TestClient
import pytest

from app.agent_runtime.errors import SkillLoadError
from app.agent_runtime.loop import _model_result
from app.agent_runtime.session import AgentSessionStore
from app.agent_runtime.skills import SkillLoader
from app.main import create_app
from app.tools.catalog_tools import _matches_query


def create_session(client: TestClient, customer_id: int | None = None) -> dict:
    payload = {"site_id": 1}
    if customer_id is not None:
        payload["customer_id"] = customer_id
    response = client.post("/api/agent/sessions", json=payload)
    assert response.status_code == 201
    return response.json()


def send_message(
    client: TestClient,
    session_id: str,
    content: str,
    key: str | None = None,
) -> dict:
    headers = {"Idempotency-Key": key} if key else None
    response = client.post(
        f"/api/agent/sessions/{session_id}/messages",
        json={"content": content},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_catalog_candidate_search_handles_descriptive_product_name():
    assert _matches_query("大土豆", "土豆") is True
    assert _matches_query("大土豆", "苹果") is False


def test_harness_planner_uses_configured_timeout(monkeypatch, tmp_path):
    captured: dict = {}
    monkeypatch.setenv("AGENT_HARNESS_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("DEEPSEEK_TIMEOUT_SECONDS", "8.5")

    def client_factory(**kwargs):
        captured.update(kwargs)
        return _FakeClient([_model_payload(content="请先告诉我客户名称。")])

    monkeypatch.setattr("app.agent_runtime.planner.httpx.Client", client_factory)
    app = create_app(str(tmp_path / "planner-timeout.db"))
    with TestClient(app) as client:
        session = create_session(client)
        send_message(client, session["id"], "帮我录番茄5斤")

    assert captured["timeout"] == 8.5


def test_agent_session_supports_multiturn_order_and_explicit_resume(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as client:
        session = create_session(client)
        waiting_customer = send_message(
            client, session["id"], "帮我录番茄5斤", "message-1"
        )

        assert waiting_customer["status"] == "waiting_input"
        assert waiting_customer["pending_action"]["name"] == "provide_customer"
        assert [
            event["type"] for event in waiting_customer["events"]
            if event["type"] in {"user", "assistant"}
        ] == ["user", "assistant"]
        assert all(
            "request_key_digest" not in event
            for event in waiting_customer["events"]
        )
        assert client.get("/api/orders").json() == []

        draft_ready = send_message(
            client, session["id"], "客户是惠民餐厅", "message-2"
        )
        assert draft_ready["status"] == "waiting_approval"
        assert draft_ready["current_draft_id"]
        assert draft_ready["pending_action"]["name"] == "confirm_order"
        assert [
            event["type"] for event in draft_ready["events"]
            if event["type"] in {"user", "assistant"}
        ] == ["user", "assistant", "user", "assistant"]
        assert client.get("/api/orders").json() == []

        resumed = client.post(
            f"/api/agent/sessions/{session['id']}/resume",
            json={"approved": True},
            headers={"Idempotency-Key": "agent-order-1"},
        )

        assert resumed.status_code == 200
        completed = resumed.json()
        assert completed["status"] == "completed"
        assert completed["current_order_id"]
        assert completed["pending_action"] is None
        assert len(client.get("/api/orders").json()) == 1
        tool_names = {
            event["name"] for event in completed["events"] if event["type"] == "tool"
        }
        assert {"customer_search", "draft_recognize", "draft_confirm"} <= tool_names
        assert "agent-order-1" not in resumed.text


def test_agent_session_events_survive_application_restart(tmp_path):
    database_path = str(tmp_path / "test.db")
    first_app = create_app(database_path)
    with TestClient(first_app) as client:
        session = create_session(client, customer_id=1)
        updated = send_message(client, session["id"], "番茄2斤", "restart-key")
        session_id = updated["id"]
        draft_id = updated["current_draft_id"]

    second_app = create_app(database_path)
    with TestClient(second_app) as client:
        restored = client.get(f"/api/agent/sessions/{session_id}")
        replay = send_message(client, session_id, "番茄2斤", "restart-key")

    assert restored.status_code == 200
    assert restored.json()["current_draft_id"] == draft_id
    assert restored.json()["status"] == "waiting_approval"
    assert any(event["type"] == "user" for event in restored.json()["events"])
    assert replay == updated


def test_unmatched_sku_requires_old_draft_edit_then_recheck(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as client:
        session = create_session(client, customer_id=1)
        result = send_message(client, session["id"], "榴莲1箱")

        assert result["status"] == "waiting_input"
        assert result["pending_action"]["name"] == "review_sku"
        assert any(
            event["type"] == "delegation"
            and event["name"] == "sku_resolution_agent"
            for event in result["events"]
        )
        assert client.get("/api/orders").json() == []
        cannot_resume = client.post(
            f"/api/agent/sessions/{session['id']}/resume",
            json={"approved": True},
        )
        assert cannot_resume.status_code == 409

        draft = client.get(f"/api/drafts/{result['current_draft_id']}").json()
        corrected = {
            "customer_id": 1,
            "items": [{
                **draft["items"][0],
                "product_id": 1,
                "product_name": "西红柿",
                "raw_product_name": "榴莲",
                "unit": "斤",
                "unit_price": 3.8,
                "matched": True,
            }],
        }
        edited = client.put(
            f"/api/drafts/{draft['id']}", json=corrected
        )
        assert edited.status_code == 200
        ready = send_message(client, session["id"], "已经修改，重新检查")
        assert ready["status"] == "waiting_approval"
        assert ready["pending_action"]["name"] == "confirm_order"


def test_agent_read_tools_are_selected_and_audited(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as client:
        session = create_session(client)
        result = send_message(client, session["id"], "有哪些客户和商品？")

    tool_names = [
        event["name"] for event in result["events"] if event["type"] == "tool"
    ]
    assert "customer_list" in tool_names
    assert "catalog_search" in tool_names
    assert "惠民餐厅" in result["last_message"]
    assert "西红柿" in result["last_message"]


def test_message_cannot_confirm_order(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as client:
        session = create_session(client, customer_id=1)
        ready = send_message(client, session["id"], "番茄1斤")
        result = send_message(client, session["id"], "确认下单")

        assert result["status"] == "waiting_approval"
        assert "resume" in result["last_message"]
        assert client.get("/api/orders").json() == []
        assert ready["current_draft_id"] == result["current_draft_id"]


def test_rejected_draft_can_be_rechecked_but_customer_scope_cannot_change(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as client:
        session = create_session(client, customer_id=1)
        ready = send_message(client, session["id"], "番茄1斤")
        rejected = client.post(
            f"/api/agent/sessions/{session['id']}/resume",
            json={"approved": False},
        )
        assert rejected.status_code == 200
        rechecked = send_message(client, session["id"], "重新检查")
        assert rechecked["status"] == "waiting_approval"

        second = create_session(client, customer_id=1)
        unresolved = send_message(client, second["id"], "榴莲1箱")
        draft = client.get(
            f"/api/drafts/{unresolved['current_draft_id']}"
        ).json()
        edited = client.put(
            f"/api/drafts/{draft['id']}",
            json={
                "customer_id": 2,
                "items": [{
                    **draft["items"][0],
                    "product_id": 1,
                    "product_name": "西红柿",
                    "unit": "斤",
                    "unit_price": 3.8,
                    "matched": True,
                }],
            },
        )
        assert edited.status_code == 409
        current = client.get(f"/api/agent/sessions/{second['id']}").json()
        assert current["customer_id"] == 1
        assert current["status"] == "waiting_input"
        assert client.get("/api/orders").json() == []


def test_message_and_resume_are_idempotent(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as client:
        session = create_session(client, customer_id=1)
        first = send_message(client, session["id"], "番茄1斤", "same-message")
        replay = send_message(client, session["id"], "番茄1斤", "same-message")
        assert replay == first
        conflict = client.post(
            f"/api/agent/sessions/{session['id']}/messages",
            json={"content": "苹果1箱"},
            headers={"Idempotency-Key": "same-message"},
        )
        assert conflict.status_code == 409

        first_resume = client.post(
            f"/api/agent/sessions/{session['id']}/resume",
            json={"approved": True},
            headers={"Idempotency-Key": "same-resume"},
        )
        second_resume = client.post(
            f"/api/agent/sessions/{session['id']}/resume",
            json={"approved": True},
            headers={"Idempotency-Key": "same-resume"},
        )
        assert first_resume.status_code == second_resume.status_code == 200
        assert first_resume.json() == second_resume.json()
        assert len(client.get("/api/orders").json()) == 1


def test_draft_change_invalidates_existing_approval(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as client:
        session = create_session(client, customer_id=1)
        ready = send_message(client, session["id"], "番茄1斤")
        draft = client.get(
            f"/api/drafts/{ready['current_draft_id']}"
        ).json()
        changed_item = {**draft["items"][0], "quantity": 2}
        changed = client.put(
            f"/api/drafts/{draft['id']}",
            json={"customer_id": 1, "items": [changed_item]},
        )
        assert changed.status_code == 200

        stale = client.post(
            f"/api/agent/sessions/{session['id']}/resume",
            json={"approved": True},
        )
        assert stale.status_code == 409
        current = client.get(f"/api/agent/sessions/{session['id']}").json()
        assert current["status"] == "waiting_input"
        assert current["pending_action"]["name"] == "review_sku"
        assert client.get("/api/orders").json() == []

        checked = send_message(client, session["id"], "重新检查")
        assert checked["status"] == "waiting_approval"
        confirmed = client.post(
            f"/api/agent/sessions/{session['id']}/resume",
            json={"approved": True},
        )
        assert confirmed.status_code == 200
        assert len(client.get("/api/orders").json()) == 1


def test_draft_customer_scope_change_is_rejected_before_state_diverges(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as client:
        session = create_session(client, customer_id=1)
        ready = send_message(client, session["id"], "番茄1斤")
        draft = client.get(
            f"/api/drafts/{ready['current_draft_id']}"
        ).json()
        changed = client.put(
            f"/api/drafts/{draft['id']}",
            json={"customer_id": 2, "items": draft["items"]},
        )
        assert changed.status_code == 409
        current = client.get(f"/api/agent/sessions/{session['id']}").json()
        assert current["customer_id"] == 1
        assert current["status"] == "waiting_approval"
        assert current["pending_action"]["name"] == "confirm_order"
        assert client.get("/api/orders").json() == []


def test_draft_creation_and_session_binding_share_review_lock(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as client:
        session = create_session(client, customer_id=1)
        loop = client.app.state.agent_loop
        original_apply = loop._apply_draft
        reached_binding = Event()
        release_binding = Event()

        def paused_apply(current, draft, budget):
            reached_binding.set()
            assert release_binding.wait(timeout=5)
            return original_apply(current, draft, budget)

        loop._apply_draft = paused_apply
        with ThreadPoolExecutor(max_workers=2) as pool:
            message_future = pool.submit(
                client.post,
                f"/api/agent/sessions/{session['id']}/messages",
                json={"content": "番茄1斤"},
            )
            assert reached_binding.wait(timeout=5)
            draft = client.get("/api/drafts").json()[0]
            put_future = pool.submit(
                client.put,
                f"/api/drafts/{draft['id']}",
                json={"customer_id": 2, "items": draft["items"]},
            )
            with pytest.raises(FutureTimeoutError):
                put_future.result(timeout=0.2)
            release_binding.set()
            message = message_future.result(timeout=5)
            edited = put_future.result(timeout=5)
        assert message.status_code == 200
        assert edited.status_code == 409
        current = client.get(f"/api/agent/sessions/{session['id']}").json()
        persisted = client.get(f"/api/drafts/{draft['id']}").json()
        assert current["customer_id"] == persisted["customer_id"] == 1


def test_event_pagination_and_monotonic_sequences(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as client:
        session = create_session(client, customer_id=1)
        for index in range(4):
            send_message(client, session["id"], "有哪些商品？", f"query-{index}")
        page = client.get(
            f"/api/agent/sessions/{session['id']}?limit=3"
        ).json()
        assert len(page["events"]) == 3
        assert page["has_more_events"] is True
        after = client.get(
            f"/api/agent/sessions/{session['id']}?after_sequence=0&limit=200"
        ).json()
        sequences = [event["sequence"] for event in after["events"]]
        assert sequences == sorted(sequences)
        assert len(sequences) == len(set(sequences))
        assert after["summary_through_sequence"] > 0
        assert after["context_summary"]


def test_concurrent_resume_creates_only_one_order(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as client:
        session = create_session(client, customer_id=1)
        send_message(client, session["id"], "番茄1斤")

        def approve(index: int):
            return client.post(
                f"/api/agent/sessions/{session['id']}/resume",
                json={"approved": True},
                headers={"Idempotency-Key": f"concurrent-{index}"},
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(approve, range(2)))
        assert [response.status_code for response in responses] == [200, 200]
        assert len(client.get("/api/orders").json()) == 1
        restored = client.get(f"/api/agent/sessions/{session['id']}").json()
        sequences = [event["sequence"] for event in restored["events"]]
        assert len(sequences) == len(set(sequences))


def test_concurrent_order_messages_create_only_one_current_draft(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as client:
        session = create_session(client, customer_id=1)

        def submit(index: int):
            return client.post(
                f"/api/agent/sessions/{session['id']}/messages",
                json={"content": "番茄1斤"},
                headers={"Idempotency-Key": f"message-{index}"},
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(submit, range(2)))
        assert [response.status_code for response in responses] == [200, 200]
        assert len(client.get("/api/drafts").json()) == 1
        assert len(client.get("/api/orders").json()) == 0
        current = client.get(f"/api/agent/sessions/{session['id']}").json()
        sequences = [event["sequence"] for event in current["events"]]
        assert len(sequences) == len(set(sequences))


def test_deepseek_tool_call_is_executed_and_synthesized(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_HARNESS_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    responses = [
        _model_payload(tool_name="customer_list", arguments={}),
        _model_payload(content="模型汇总了客户列表。"),
    ]
    monkeypatch.setattr(
        "app.agent_runtime.planner.httpx.Client",
        lambda **_kwargs: _FakeClient(responses),
    )
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as client:
        session = create_session(client)
        result = send_message(client, session["id"], "请介绍可用客户")

    assert result["last_message"].startswith("客户：")
    assert "惠民餐厅" in result["last_message"]
    assert "模型汇总" not in result["last_message"]
    planner_events = [
        event for event in result["events"] if event["name"] == "model_planner"
    ]
    assert planner_events
    assert planner_events[0]["payload"]["tool_names"] == ["customer_list"]


def test_deepseek_natural_language_customer_prompt_enters_local_state_machine(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("AGENT_HARNESS_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    responses = [
        _model_payload(content="请先告诉我客户名称。"),
        _model_payload(
            tool_name="customer_search", arguments={"query": "惠民餐厅"}
        ),
    ]
    monkeypatch.setattr(
        "app.agent_runtime.planner.httpx.Client",
        lambda **_kwargs: _FakeClient(responses),
    )
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as client:
        session = create_session(client)
        waiting = send_message(client, session["id"], "帮我录入西红柿5斤")
        assert waiting["status"] == "waiting_input"
        assert waiting["pending_action"]["name"] == "provide_customer"
        assert waiting["pending_action"]["arguments"]["order_text"] == (
            "西红柿5斤"
        )

        result = send_message(client, session["id"], "客户是惠民餐厅")

    assert result["customer_id"] == 1
    assert result["current_draft_id"] is not None
    assert result["status"] == "waiting_approval"
    assert result["pending_action"]["name"] == "confirm_order"


def test_missing_customer_state_ignores_model_success_claim(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_HARNESS_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    responses = [_model_payload(content="订单已经创建成功。")] 
    monkeypatch.setattr(
        "app.agent_runtime.planner.httpx.Client",
        lambda **_kwargs: _FakeClient(responses),
    )
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as client:
        session = create_session(client)
        result = send_message(client, session["id"], "帮我录入西红柿5斤")
        assert client.get("/api/orders").json() == []

    assert result["status"] == "waiting_input"
    assert result["pending_action"]["name"] == "provide_customer"
    assert result["last_message"] == (
        "请先告诉我客户的完整名称，再继续生成订单草稿。"
    )


def test_model_draft_call_without_customer_uses_local_clean_order_text(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("AGENT_HARNESS_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    responses = [_model_payload(
        tool_name="draft_recognize",
        arguments={"text": "帮我录入西红柿5斤"},
    )]
    monkeypatch.setattr(
        "app.agent_runtime.planner.httpx.Client",
        lambda **_kwargs: _FakeClient(responses),
    )
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as client:
        session = create_session(client)
        result = send_message(client, session["id"], "帮我录入西红柿5斤")

    assert result["status"] == "waiting_input"
    assert result["pending_action"]["name"] == "provide_customer"
    assert result["pending_action"]["arguments"]["order_text"] == "西红柿5斤"


def test_deepseek_sku_agent_has_read_only_tool_subset(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_HARNESS_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    responses = [
        _model_payload(
            tool_name="draft_recognize", arguments={"text": "榴莲1箱"}
        ),
        _model_payload(
            tool_name="catalog_search",
            arguments={"query": "榴莲", "active_only": True},
        ),
    ]
    monkeypatch.setattr(
        "app.agent_runtime.planner.httpx.Client",
        lambda **_kwargs: _FakeClient(responses),
    )
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as client:
        session = create_session(client, customer_id=1)
        result = send_message(client, session["id"], "请处理榴莲1箱")

    assert result["status"] == "waiting_input"
    child_tools = [
        event for event in result["events"]
        if event["type"] == "tool"
        and event["actor"] == "sku_resolution_agent"
    ]
    assert child_tools
    assert {event["name"] for event in child_tools} == {"catalog_search"}
    delegation = next(
        event for event in result["events"] if event["type"] == "delegation"
    )
    assert delegation["payload"]["planner_fallback"] is False
    assert delegation["payload"]["planner_metrics"]["total_tokens"] == 15


def test_model_failure_and_unknown_tool_fall_back_to_rules(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_HARNESS_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setattr(
        "app.agent_runtime.planner.httpx.Client",
        lambda **_kwargs: _FailingClient(),
    )
    app = create_app(str(tmp_path / "failure.db"))
    with TestClient(app) as client:
        session = create_session(client)
        result = send_message(client, session["id"], "有哪些客户？")
    assert "惠民餐厅" in result["last_message"]
    assert any(event["status"] == "fallback" for event in result["events"])

    responses = [_model_payload(tool_name="draft_confirm", arguments={})]
    monkeypatch.setattr(
        "app.agent_runtime.planner.httpx.Client",
        lambda **_kwargs: _FakeClient(responses),
    )
    app = create_app(str(tmp_path / "unknown.db"))
    with TestClient(app) as client:
        session = create_session(client)
        result = send_message(client, session["id"], "有哪些客户？")
    assert "惠民餐厅" in result["last_message"]
    assert any(event["status"] == "rejected" for event in result["events"])
    assert client.get("/api/orders").json() == []


def test_invalid_tool_on_last_model_round_still_gets_rule_fallback(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("AGENT_HARNESS_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("AGENT_HARNESS_MAX_ROUNDS", "1")
    responses = [_model_payload(tool_name="draft_confirm", arguments={})]
    monkeypatch.setattr(
        "app.agent_runtime.planner.httpx.Client",
        lambda **_kwargs: _FakeClient(responses),
    )
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as client:
        session = create_session(client)
        result = send_message(client, session["id"], "有哪些客户？")
    assert "惠民餐厅" in result["last_message"]
    assert not any(event["name"] == "round_budget" for event in result["events"])


def test_unexpected_tool_exception_has_failed_terminal_event(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as client:
        session = create_session(client)

        def fail():
            raise RuntimeError("private database detail")

        client.app.state.agent_loop.gateway.list_customers = fail
        result = send_message(client, session["id"], "有哪些客户？")

    customer_events = [
        event for event in result["events"]
        if event["type"] == "tool" and event["name"] == "customer_list"
    ]
    assert [event["status"] for event in customer_events] == ["started", "failed"]
    assert "暂时不可用" in result["last_message"]
    assert "private database detail" not in str(result)


def test_model_cannot_claim_an_unpersisted_order(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_HARNESS_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    responses = [_model_payload(content="订单已创建，建单成功。")]
    monkeypatch.setattr(
        "app.agent_runtime.planner.httpx.Client",
        lambda **_kwargs: _FakeClient(responses),
    )
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as client:
        session = create_session(client)
        result = send_message(client, session["id"], "随便聊聊")
        assert client.get("/api/orders").json() == []
    assert result["last_message"] == (
        "模型未执行可验证的本地操作；当前没有已创建订单。"
    )


def test_model_tool_arguments_are_locally_validated(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_HARNESS_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    responses = [_model_payload(
        tool_name="catalog_search",
        arguments={"query": "", "active_only": True, "site_id": 999},
    )]
    monkeypatch.setattr(
        "app.agent_runtime.planner.httpx.Client",
        lambda **_kwargs: _FakeClient(responses),
    )
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as client:
        session = create_session(client)
        result = send_message(client, session["id"], "有哪些商品？")
    assert "西红柿" in result["last_message"]
    assert any(event["status"] == "rejected" for event in result["events"])


def test_tool_call_budget_stops_before_execution(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_HARNESS_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("AGENT_HARNESS_MAX_TOOL_CALLS", "1")
    responses = [_model_payload(tool_calls=[
        ("customer_list", {}), ("catalog_search", {"query": ""}),
    ])]
    monkeypatch.setattr(
        "app.agent_runtime.planner.httpx.Client",
        lambda **_kwargs: _FakeClient(responses),
    )
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as client:
        session = create_session(client)
        result = send_message(client, session["id"], "有哪些客户和商品？")
    assert "安全上限" in result["last_message"]
    assert any(event["name"] == "tool_budget" for event in result["events"])


def test_skill_loader_rejects_decorative_or_invalid_skill(tmp_path):
    root = Path(tmp_path)
    directory = root / "broken"
    directory.mkdir()
    (directory / "SKILL.md").write_text("# no frontmatter", encoding="utf-8")
    try:
        SkillLoader(root).load("broken", "broken")
    except SkillLoadError:
        pass
    else:
        raise AssertionError("invalid skill should fail")


def test_missing_agent_session_returns_404(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as client:
        response = client.get("/api/agent/sessions/missing")
    assert response.status_code == 404


def test_hard_caps_and_auto_continuation_share_one_tool_budget(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("AGENT_HARNESS_MAX_ROUNDS", "99")
    monkeypatch.setenv("AGENT_HARNESS_MAX_TOOL_CALLS", "1")
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as client:
        loop = client.app.state.agent_loop
        assert loop.max_rounds == 6
        assert loop.max_tool_calls == 1
        session = create_session(client)
        send_message(client, session["id"], "帮我录番茄1斤")
        result = send_message(client, session["id"], "客户是惠民餐厅")
        assert result["current_draft_id"] is None
        assert any(event["name"] == "tool_budget" for event in result["events"])


def test_sku_agent_uses_at_most_three_catalog_attempts_without_group_retry(
    tmp_path
):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as client:
        session = create_session(client, customer_id=1)
        calls: list[str] = []
        original = client.app.state.agent_loop.gateway.catalog_snapshot

        def flaky(site_id: int):
            calls.append(str(site_id))
            if len(calls) == 1:
                raise RuntimeError("private catalog failure")
            return original(site_id)

        client.app.state.agent_loop.gateway.catalog_snapshot = flaky
        result = send_message(
            client, session["id"], "榴莲1箱，香蕉1箱，芒果1箱，菠萝1箱"
        )

    child_started = [
        event for event in result["events"]
        if event["type"] == "tool"
        and event["actor"] == "sku_resolution_agent"
        and event["status"] == "started"
    ]
    assert len(child_started) <= 3
    assert len(calls) <= 3


def test_same_draft_fingerprint_is_delegated_only_once(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as client:
        session = create_session(client, customer_id=1)
        first = send_message(client, session["id"], "榴莲1箱")
        first_delegations = sum(
            event["name"] == "sku_resolution_agent" for event in first["events"]
        )
        second = send_message(client, session["id"], "重新检查")
        second_delegations = sum(
            event["name"] == "sku_resolution_agent" for event in second["events"]
        )
    assert first_delegations == 1
    assert second_delegations == 1


def test_rejected_tool_attempt_consumes_shared_budget(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_HARNESS_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("AGENT_HARNESS_MAX_TOOL_CALLS", "1")
    monkeypatch.setattr(
        "app.agent_runtime.planner.httpx.Client",
        lambda **_kwargs: _FakeClient([
            _model_payload(tool_name="draft_confirm", arguments={})
        ]),
    )
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as client:
        session = create_session(client)
        result = send_message(client, session["id"], "有哪些客户？")
    assert any(event["name"] == "tool_budget" for event in result["events"])
    assert not any(
        event["name"] == "customer_list" and event["status"] == "started"
        for event in result["events"]
    )


def test_message_placeholder_survives_failure_and_retries_without_duplicate_draft(
    tmp_path
):
    database_path = str(tmp_path / "test.db")
    app = create_app(database_path)
    with TestClient(app, raise_server_exceptions=False) as client:
        session = create_session(client, customer_id=1)
        store = client.app.state.agent_loop.store
        original = store.complete_request
        failed = False

        def fail_once(*args, **kwargs):
            nonlocal failed
            if not failed:
                failed = True
                raise RuntimeError("simulated process exit")
            return original(*args, **kwargs)

        store.complete_request = fail_once
        first = client.post(
            f"/api/agent/sessions/{session['id']}/messages",
            json={"content": "番茄1斤"},
            headers={"Idempotency-Key": "crash-message-key"},
        )
        assert first.status_code == 500
        store.complete_request = original
        conflict = client.post(
            f"/api/agent/sessions/{session['id']}/messages",
            json={"content": "苹果1箱"},
            headers={"Idempotency-Key": "crash-message-key"},
        )
        assert conflict.status_code == 409
        retry = client.post(
            f"/api/agent/sessions/{session['id']}/messages",
            json={"content": "番茄1斤"},
            headers={"Idempotency-Key": "crash-message-key"},
        )
        assert retry.status_code == 200
        assert len(client.get("/api/drafts").json()) == 1
        dialogue = [
            event for event in retry.json()["events"]
            if event["type"] in {"user", "assistant"}
        ]
        assert [event["type"] for event in dialogue] == ["user", "assistant"]
        assert dialogue[0]["payload"]["content"] == "番茄1斤"
    with sqlite3.connect(database_path) as connection:
        request_row = connection.execute(
            """
            SELECT request_key_digest, payload_digest, status
            FROM agent_requests WHERE operation = 'message'
            """
        ).fetchone()
    assert request_row is not None
    assert request_row[2] == "completed"
    assert len(request_row[0]) == 64
    assert len(request_row[1]) == 64
    assert "crash-message-key" not in " ".join(request_row)


def test_descriptive_sku_query_returns_candidate_without_automatic_match(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as client:
        session = create_session(client, customer_id=1)
        result = send_message(client, session["id"], "大土豆10斤")
        draft = client.get(f"/api/drafts/{result['current_draft_id']}").json()
        denied = client.post(
            f"/api/agent/sessions/{session['id']}/resume",
            json={"approved": True},
        )

    candidate_results = [
        event["payload"]["result"] for event in result["events"]
        if event["type"] == "tool"
        and event["name"] == "catalog_search"
        and event["status"] == "completed"
    ]
    assert any(
        candidate["name"] == "土豆"
        for candidates in candidate_results
        for candidate in candidates
    )
    assert result["pending_action"]["name"] == "review_sku"
    assert draft["items"][0]["matched"] is False
    assert denied.status_code == 409


def test_resume_placeholder_reconciles_order_after_completion_write_failure(
    tmp_path
):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app, raise_server_exceptions=False) as client:
        session = create_session(client, customer_id=1)
        ready = send_message(client, session["id"], "番茄1斤")
        store = client.app.state.agent_loop.store
        original = store.complete_request
        failed = False

        def fail_once(*args, **kwargs):
            nonlocal failed
            if not failed:
                failed = True
                raise RuntimeError("simulated process exit")
            return original(*args, **kwargs)

        store.complete_request = fail_once
        first = client.post(
            f"/api/agent/sessions/{session['id']}/resume",
            json={"approved": True},
            headers={"Idempotency-Key": "crash-resume-key"},
        )
        assert first.status_code == 500
        store.complete_request = original
        retry = client.post(
            f"/api/agent/sessions/{session['id']}/resume",
            json={"approved": True},
            headers={"Idempotency-Key": "crash-resume-key"},
        )
        assert retry.status_code == 200
        assert retry.json()["status"] == "completed"
        assert retry.json()["current_draft_id"] == ready["current_draft_id"]
        assert len(client.get("/api/orders").json()) == 1


def test_message_retry_after_response_stage_failure_does_not_duplicate_dialogue(
    tmp_path
):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app, raise_server_exceptions=False) as client:
        session = create_session(client)
        store = client.app.state.agent_loop.store
        original = store.stage_request_response
        failed = False

        def fail_once(*args, **kwargs):
            nonlocal failed
            if not failed:
                failed = True
                raise RuntimeError("simulated stage failure")
            return original(*args, **kwargs)

        store.stage_request_response = fail_once
        first = client.post(
            f"/api/agent/sessions/{session['id']}/messages",
            json={"content": "帮我录番茄1斤"},
            headers={"Idempotency-Key": "stage-crash-key"},
        )
        assert first.status_code == 500
        store.stage_request_response = original
        retry = client.post(
            f"/api/agent/sessions/{session['id']}/messages",
            json={"content": "帮我录番茄1斤"},
            headers={"Idempotency-Key": "stage-crash-key"},
        )

    assert retry.status_code == 200
    dialogue = [
        event for event in retry.json()["events"]
        if event["type"] in {"user", "assistant"}
    ]
    assert [event["type"] for event in dialogue] == ["user", "assistant"]
    assert dialogue[0]["payload"]["content"] == "帮我录番茄1斤"


def test_message_retry_after_reply_boundary_failure_does_not_rerun_tools(
    tmp_path
):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app, raise_server_exceptions=False) as client:
        session = create_session(client, customer_id=1)
        loop = client.app.state.agent_loop
        original = loop._ensure_assistant_event
        failed = False

        def fail_once(*args, **kwargs):
            nonlocal failed
            if not failed:
                failed = True
                raise RuntimeError("simulated reply boundary failure")
            return original(*args, **kwargs)

        loop._ensure_assistant_event = fail_once
        first = client.post(
            f"/api/agent/sessions/{session['id']}/messages",
            json={"content": "番茄1斤"},
            headers={"Idempotency-Key": "reply-crash-key"},
        )
        assert first.status_code == 500
        before_retry = client.get(
            f"/api/agent/sessions/{session['id']}?after_sequence=0&limit=200"
        ).json()
        loop._ensure_assistant_event = original
        retry = client.post(
            f"/api/agent/sessions/{session['id']}/messages",
            json={"content": "番茄1斤"},
            headers={"Idempotency-Key": "reply-crash-key"},
        )

    assert retry.status_code == 200
    dialogue = [
        event for event in retry.json()["events"]
        if event["type"] in {"user", "assistant"}
    ]
    assert [event["type"] for event in dialogue] == ["user", "assistant"]
    assert "不会重复执行工具" in dialogue[1]["payload"]["content"]
    before_non_dialogue = [
        event["id"] for event in before_retry["events"]
        if event["type"] not in {"user", "assistant"}
    ]
    after_non_dialogue = [
        event["id"] for event in retry.json()["events"]
        if event["type"] not in {"user", "assistant"}
    ]
    assert after_non_dialogue == before_non_dialogue


def test_old_confirm_endpoint_is_reconciled_into_agent_session(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as client:
        session = create_session(client, customer_id=1)
        ready = send_message(client, session["id"], "番茄1斤")
        confirmed = client.post(
            f"/api/drafts/{ready['current_draft_id']}/confirm",
            headers={"Idempotency-Key": "legacy-confirm"},
        )
        assert confirmed.status_code == 200
        restored = client.get(f"/api/agent/sessions/{session['id']}").json()
        assert restored["status"] == "completed"
        assert restored["current_order_id"] == confirmed.json()["id"]
        assert restored["last_message"] == (
            f"订单 {confirmed.json()['order_no']} 已创建。"
        )


def test_idempotent_message_replay_reconciles_old_confirm(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as client:
        session = create_session(client, customer_id=1)
        ready = send_message(
            client, session["id"], "番茄1斤", "message-before-old-confirm"
        )
        confirmed = client.post(
            f"/api/drafts/{ready['current_draft_id']}/confirm",
            headers={"Idempotency-Key": "old-confirm-after-message"},
        )
        assert confirmed.status_code == 200
        replay = client.post(
            f"/api/agent/sessions/{session['id']}/messages",
            json={"content": "番茄1斤"},
            headers={"Idempotency-Key": "message-before-old-confirm"},
        )
        assert replay.status_code == 200
        assert replay.json()["status"] == "completed"
        assert replay.json()["current_order_id"] == confirmed.json()["id"]


def test_idempotent_resume_replay_reconciles_old_confirm(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as client:
        session = create_session(client, customer_id=1)
        ready = send_message(client, session["id"], "番茄1斤")
        rejected = client.post(
            f"/api/agent/sessions/{session['id']}/resume",
            json={"approved": False},
            headers={"Idempotency-Key": "resume-before-old-confirm"},
        )
        assert rejected.status_code == 200
        confirmed = client.post(
            f"/api/drafts/{ready['current_draft_id']}/confirm",
            headers={"Idempotency-Key": "old-confirm-after-resume"},
        )
        assert confirmed.status_code == 200
        replay = client.post(
            f"/api/agent/sessions/{session['id']}/resume",
            json={"approved": False},
            headers={"Idempotency-Key": "resume-before-old-confirm"},
        )
        assert replay.status_code == 200
        assert replay.json()["status"] == "completed"
        assert replay.json()["current_order_id"] == confirmed.json()["id"]


def test_existing_draft_prevents_customer_switch(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as client:
        session = create_session(client, customer_id=1)
        send_message(client, session["id"], "番茄1斤")
        rejected = client.post(
            f"/api/agent/sessions/{session['id']}/resume",
            json={"approved": False},
        )
        assert rejected.status_code == 200
        switched = client.post(
            f"/api/agent/sessions/{session['id']}/messages",
            json={"content": "客户是阳光幼儿园"},
        )
        assert switched.status_code == 409


def test_agent_request_models_forbid_extras_and_limit_idempotency_header(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as client:
        assert client.post(
            "/api/agent/sessions", json={"site_id": 1, "extra": True}
        ).status_code == 422
        session = create_session(client)
        assert client.post(
            f"/api/agent/sessions/{session['id']}/messages",
            json={"content": "有哪些客户？", "extra": True},
        ).status_code == 422
        assert client.post(
            f"/api/agent/sessions/{session['id']}/resume",
            json={"approved": True, "extra": True},
        ).status_code == 422
        assert client.post(
            f"/api/agent/sessions/{session['id']}/messages",
            json={"content": "有哪些客户？"},
            headers={"Idempotency-Key": "x" * 201},
        ).status_code == 422


def test_missing_session_with_idempotency_key_still_returns_404(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as client:
        message = client.post(
            "/api/agent/sessions/missing/messages",
            json={"content": "有哪些客户？"},
            headers={"Idempotency-Key": "missing-message"},
        )
        resume = client.post(
            "/api/agent/sessions/missing/resume",
            json={"approved": True},
            headers={"Idempotency-Key": "missing-resume"},
        )
    assert message.status_code == 404
    assert resume.status_code == 404


def test_event_storage_and_model_projection_remove_internal_fields(tmp_path):
    database_path = str(tmp_path / "test.db")
    app = create_app(database_path)
    with TestClient(app) as client:
        session = create_session(client)
        client.app.state.agent_loop.store.append_event(
            session["id"], "tool", "test", "malicious", "completed", "safe",
            {
                "nested": {
                    "idempotency_key": "plain-secret",
                    "Idempotency-Key": "hyphen-secret",
                    "requestKey": "camel-secret",
                    "payload_md5": "payload-secret",
                    "error_message": "private error",
                    "errorType": "PrivateDatabaseException",
                    "status": "ok",
                }
            },
        )
    with sqlite3.connect(database_path) as connection:
        raw_events = " ".join(
            row[0] for row in connection.execute(
                "SELECT payload_json FROM agent_events"
            ).fetchall()
        )
    assert "plain-secret" not in raw_events
    assert "hyphen-secret" not in raw_events
    assert "camel-secret" not in raw_events
    assert "payload-secret" not in raw_events
    assert "private error" not in raw_events
    assert "PrivateDatabaseException" not in raw_events
    assert _model_result("recognition_task_get", {
        "id": "task-1",
        "status": "succeeded",
        "idempotency_key": "task-secret",
        "payload_md5": "payload-secret",
        "text": "full private body",
    }) == {"id": "task-1", "status": "succeeded"}


def test_legacy_agent_event_table_adds_internal_request_digest_column(tmp_path):
    database_path = str(tmp_path / "legacy-events.db")
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute(
            """
            CREATE TABLE agent_events (
                id INTEGER PRIMARY KEY,
                session_id TEXT NOT NULL,
                sequence INTEGER NOT NULL
            )
            """
        )
        AgentSessionStore._migrate_agent_events(connection)
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(agent_events)")
        }
        indexes = {
            row["name"]
            for row in connection.execute("PRAGMA index_list(agent_events)")
        }

    assert "request_key_digest" in columns
    assert "idx_agent_events_request" in indexes


def test_legacy_agent_request_rows_migrate_to_hashed_keys(tmp_path):
    database_path = str(tmp_path / "test.db")
    app = create_app(database_path)
    with TestClient(app) as client:
        session = create_session(client)
        response_json = json.dumps(session, ensure_ascii=False, separators=(",", ":"))
    payload = {"content": "有哪些客户？"}
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    legacy_md5 = hashlib.md5(
        canonical.encode("utf-8"), usedforsecurity=False
    ).hexdigest()
    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP TABLE agent_requests")
        connection.execute(
            """
            CREATE TABLE agent_requests (
                session_id TEXT NOT NULL, request_key TEXT NOT NULL,
                operation TEXT NOT NULL, payload_md5 TEXT NOT NULL,
                response_json TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY (session_id, request_key, operation)
            )
            """
        )
        connection.execute(
            "INSERT INTO agent_requests VALUES (?, ?, 'message', ?, ?, ?)",
            (session["id"], "legacy-plain-key", legacy_md5, response_json, "now"),
        )
    restarted = create_app(database_path)
    with TestClient(restarted) as client:
        replay = client.post(
            f"/api/agent/sessions/{session['id']}/messages",
            json=payload,
            headers={"Idempotency-Key": "legacy-plain-key"},
        )
        assert replay.status_code == 200
    with sqlite3.connect(database_path) as connection:
        columns = {
            row[1] for row in connection.execute(
                "PRAGMA table_info(agent_requests)"
            ).fetchall()
        }
        raw = " ".join(
            str(value)
            for row in connection.execute("SELECT * FROM agent_requests").fetchall()
            for value in row
        )
    assert "request_key" not in columns
    assert "request_key_digest" in columns
    assert "legacy-plain-key" not in raw


def test_legacy_request_migration_rolls_back_and_recovers_after_failure(
    monkeypatch, tmp_path
):
    database_path = str(tmp_path / "test.db")
    app = create_app(database_path)
    with TestClient(app) as client:
        session = create_session(client)
    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP TABLE agent_requests")
        connection.execute(
            """
            CREATE TABLE agent_requests (
                session_id TEXT NOT NULL, request_key TEXT NOT NULL,
                operation TEXT NOT NULL, payload_md5 TEXT NOT NULL,
                response_json TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY (session_id, request_key, operation)
            )
            """
        )
        connection.execute(
            "INSERT INTO agent_requests VALUES (?, ?, 'message', ?, ?, ?)",
            (session["id"], "migration-crash-key", "legacy-md5", "{}", "now"),
        )

    original = AgentSessionStore._copy_legacy_request_rows

    def fail_copy(_connection):
        raise RuntimeError("simulated migration crash")

    monkeypatch.setattr(
        AgentSessionStore, "_copy_legacy_request_rows", staticmethod(fail_copy)
    )
    broken = create_app(database_path)
    with pytest.raises(RuntimeError, match="simulated migration crash"):
        with TestClient(broken):
            pass
    with sqlite3.connect(database_path) as connection:
        columns_after_failure = {
            row[1] for row in connection.execute(
                "PRAGMA table_info(agent_requests)"
            ).fetchall()
        }
        orphan = connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'agent_requests_legacy'
            """
        ).fetchone()
    assert "request_key" in columns_after_failure
    assert orphan is None

    monkeypatch.setattr(
        AgentSessionStore, "_copy_legacy_request_rows", staticmethod(original)
    )
    recovered = create_app(database_path)
    with TestClient(recovered):
        pass
    with sqlite3.connect(database_path) as connection:
        columns_after_recovery = {
            row[1] for row in connection.execute(
                "PRAGMA table_info(agent_requests)"
            ).fetchall()
        }
        raw = " ".join(
            str(value)
            for row in connection.execute("SELECT * FROM agent_requests").fetchall()
            for value in row
        )
    assert "request_key_digest" in columns_after_recovery
    assert "migration-crash-key" not in raw


def test_malformed_provider_response_falls_back_to_local_rules(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("AGENT_HARNESS_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setattr(
        "app.agent_runtime.planner.httpx.Client",
        lambda **_kwargs: _FakeClient([{"choices": "not-a-list"}]),
    )
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as client:
        session = create_session(client)
        result = send_message(client, session["id"], "有哪些客户？")
    assert "惠民餐厅" in result["last_message"]
    assert any(event["status"] == "fallback" for event in result["events"])


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, responses):
        self._responses = responses

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def post(self, *_args, **_kwargs):
        return _FakeResponse(self._responses.pop(0))


class _FailingClient(_FakeClient):
    def __init__(self):
        super().__init__([])

    def post(self, *_args, **_kwargs):
        raise RuntimeError("network down")


def _model_payload(
    content: str = "",
    tool_name: str | None = None,
    arguments: dict | None = None,
    tool_calls: list[tuple[str, dict]] | None = None,
):
    requested = tool_calls or ([(tool_name, arguments or {})] if tool_name else [])
    calls = [
        {
            "id": f"call-{index}",
            "type": "function",
            "function": {"name": name, "arguments": __import__("json").dumps(args)},
        }
        for index, (name, args) in enumerate(requested, start=1)
    ]
    return {
        "choices": [{"message": {"content": content, "tool_calls": calls}}],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
        },
    }
