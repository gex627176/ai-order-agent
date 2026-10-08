import threading
import time
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.main import create_app
from app.database import Database
from app.workflow import OrderWorkflow


TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}


def wait_for_task(client: TestClient, task_id: str, timeout: float = 6.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/recognition-tasks/{task_id}")
        assert response.status_code == 200
        task = response.json()
        if task["status"] in TERMINAL_STATUSES:
            return task
        time.sleep(0.03)
    raise AssertionError(f"识别任务未在 {timeout} 秒内结束")


def configure_fast_rules(monkeypatch) -> None:
    monkeypatch.setenv("AI_PROVIDER", "rules")
    monkeypatch.setenv("RECOGNITION_TASK_POLL_INTERVAL", "0.01")


def test_async_task_is_idempotent_and_creates_review_draft(tmp_path, monkeypatch):
    configure_fast_rules(monkeypatch)
    app = create_app(str(tmp_path / "test.db"))

    with TestClient(app) as client:
        payload = {"customer_id": 1, "site_id": 1, "text": "番茄5斤"}
        first = client.post(
            "/api/recognition-tasks",
            json=payload,
            headers={"Idempotency-Key": "recognize-demo-1"},
        )
        replay = client.post(
            "/api/recognition-tasks",
            json=payload,
            headers={"Idempotency-Key": "recognize-demo-1"},
        )
        conflict = client.post(
            "/api/recognition-tasks",
            json={"customer_id": 1, "site_id": 1, "text": "苹果2箱"},
            headers={"Idempotency-Key": "recognize-demo-1"},
        )

        assert first.status_code == 202
        assert replay.status_code == 202
        assert replay.json()["id"] == first.json()["id"]
        assert conflict.status_code == 409

        task = wait_for_task(client, first.json()["id"])
        assert task["status"] == "succeeded"
        assert task["attempt_count"] == 1
        assert task["draft_id"]

        draft = client.get(f"/api/drafts/{task['draft_id']}")
        assert draft.status_code == 200
        assert draft.json()["status"] == "needs_review"
        assert client.get("/api/orders").json() == []
        assert any(
            item["id"] == task["id"]
            for item in client.get("/api/recognition-tasks").json()
        )


def test_async_task_retries_transient_failure(tmp_path, monkeypatch):
    configure_fast_rules(monkeypatch)
    monkeypatch.setenv("RECOGNITION_TASK_MAX_ATTEMPTS", "2")
    original_recognize = OrderWorkflow.recognize
    calls = 0

    def fail_once(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary failure")
        return original_recognize(self, *args, **kwargs)

    monkeypatch.setattr(OrderWorkflow, "recognize", fail_once)
    app = create_app(str(tmp_path / "test.db"))

    with TestClient(app) as client:
        created = client.post(
            "/api/recognition-tasks",
            json={"customer_id": 1, "site_id": 1, "text": "番茄5斤"},
        ).json()
        task = wait_for_task(client, created["id"])

    assert task["status"] == "succeeded"
    assert task["attempt_count"] == 2
    assert calls == 2


def test_failed_task_can_be_retried_manually(tmp_path, monkeypatch):
    configure_fast_rules(monkeypatch)
    monkeypatch.setenv("RECOGNITION_TASK_MAX_ATTEMPTS", "1")
    original_recognize = OrderWorkflow.recognize
    should_fail = True

    def controlled_recognize(self, *args, **kwargs):
        if should_fail:
            raise RuntimeError("manual retry case")
        return original_recognize(self, *args, **kwargs)

    monkeypatch.setattr(OrderWorkflow, "recognize", controlled_recognize)
    app = create_app(str(tmp_path / "test.db"))

    with TestClient(app) as client:
        created = client.post(
            "/api/recognition-tasks",
            json={"customer_id": 1, "site_id": 1, "text": "苹果2箱"},
        ).json()
        failed = wait_for_task(client, created["id"])
        assert failed["status"] == "failed"
        assert failed["error_type"] == "RuntimeError"

        should_fail = False
        retried = client.post(
            f"/api/recognition-tasks/{created['id']}/retry"
        )
        assert retried.status_code == 200
        succeeded = wait_for_task(client, created["id"])

    assert succeeded["status"] == "succeeded"
    assert succeeded["attempt_count"] == 1


def test_running_task_cancellation_never_creates_order(tmp_path, monkeypatch):
    configure_fast_rules(monkeypatch)
    original_recognize = OrderWorkflow.recognize
    entered = threading.Event()
    release = threading.Event()

    def blocked_recognize(self, *args, **kwargs):
        entered.set()
        assert release.wait(timeout=3)
        return original_recognize(self, *args, **kwargs)

    monkeypatch.setattr(OrderWorkflow, "recognize", blocked_recognize)
    app = create_app(str(tmp_path / "test.db"))

    with TestClient(app) as client:
        created = client.post(
            "/api/recognition-tasks",
            json={"customer_id": 1, "site_id": 1, "text": "胡萝卜1公斤"},
        ).json()
        assert entered.wait(timeout=2)

        cancelled = client.post(
            f"/api/recognition-tasks/{created['id']}/cancel"
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["cancel_requested"] is True
        release.set()
        task = wait_for_task(client, created["id"])

        assert task["status"] == "cancelled"
        assert client.get("/api/orders").json() == []


def test_non_retryable_failure_stops_after_first_attempt(tmp_path, monkeypatch):
    configure_fast_rules(monkeypatch)
    monkeypatch.setenv("RECOGNITION_TASK_MAX_ATTEMPTS", "3")

    def invalid_business_input(self, *args, **kwargs):
        raise ValueError("invalid catalog scope")

    monkeypatch.setattr(OrderWorkflow, "recognize", invalid_business_input)
    app = create_app(str(tmp_path / "test.db"))

    with TestClient(app) as client:
        created = client.post(
            "/api/recognition-tasks",
            json={"customer_id": 1, "site_id": 1, "text": "番茄5斤"},
        ).json()
        task = wait_for_task(client, created["id"])

    assert task["status"] == "failed"
    assert task["attempt_count"] == 1
    assert task["max_attempts"] == 3


def test_running_task_is_recovered_after_application_restart(tmp_path, monkeypatch):
    configure_fast_rules(monkeypatch)
    database_path = str(tmp_path / "test.db")
    database = Database(database_path)
    database.initialize()
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    task = database.create_recognition_task({
        "id": "restart-task",
        "idempotency_key": "restart-key",
        "payload_md5": "restart-payload",
        "text": "番茄5斤",
        "customer_id": 1,
        "site_id": 1,
        "input_type": "text",
        "source_filename": "",
        "max_attempts": 3,
        "created_at": now,
    })
    claimed = database.claim_next_recognition_task(now)
    assert task["status"] == "pending"
    assert claimed is not None and claimed["status"] == "running"

    restarted_app = create_app(database_path)
    with TestClient(restarted_app) as client:
        recovered = wait_for_task(client, "restart-task")

    assert recovered["status"] == "succeeded"
    assert recovered["attempt_count"] == 2
