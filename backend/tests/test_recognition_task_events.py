from __future__ import annotations

import asyncio
import threading

from fastapi.testclient import TestClient
import pytest

from app.main import create_app, recognition_task_event_stream
from app.task_events import RecognitionTaskEventBroker


def test_broker_publishes_across_threads_and_keeps_latest_snapshot() -> None:
    async def scenario() -> None:
        broker = RecognitionTaskEventBroker(queue_size=1)
        subscription = broker.subscribe("task-1")

        def publish_updates() -> None:
            broker.publish({"id": "task-1", "status": "running"})
            broker.publish({"id": "task-1", "status": "succeeded"})

        worker = threading.Thread(target=publish_updates)
        worker.start()
        worker.join(timeout=1)
        await asyncio.sleep(0)

        event = await subscription.get(timeout=1)
        assert event.task["status"] == "succeeded"
        assert broker.subscriber_count("task-1") == 1
        subscription.close()
        assert broker.subscriber_count("task-1") == 0

    asyncio.run(scenario())


def test_sse_returns_404_for_unknown_task(tmp_path) -> None:
    with TestClient(create_app(str(tmp_path / "sse-404.db"))) as client:
        response = client.get("/api/recognition-tasks/missing/events")

    assert response.status_code == 404
    assert response.json()["detail"] == "识别任务不存在"


def test_sse_sends_sqlite_snapshot_and_closes_for_terminal_task(tmp_path) -> None:
    app = create_app(str(tmp_path / "sse-terminal.db"))
    with TestClient(app) as client:
        task = client.post(
            "/api/recognition-tasks",
            json={"text": "番茄2斤", "customer_id": 1, "site_id": 1},
        ).json()
        runner = client.app.state.task_runner
        runner.close()
        database = client.app.state.database
        current = database.get_recognition_task(task["id"])
        if current["status"] not in {"succeeded", "failed", "cancelled"}:
            current = database.cancel_recognition_task(task["id"], current["updated_at"])

        response = client.get(f"/api/recognition-tasks/{task['id']}/events")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"
    assert "event: task" in response.text
    assert f"id: {current['updated_at']}" in response.text
    assert f'"status":"{current["status"]}"' in response.text
    assert response.text.endswith("\n\n")
    assert client.app.state.task_event_broker.subscriber_count(task["id"]) == 0


def test_event_stream_heartbeats_then_pushes_terminal_snapshot(tmp_path) -> None:
    class ConnectedRequest:
        async def is_disconnected(self) -> bool:
            return False

    app = create_app(str(tmp_path / "sse-heartbeat.db"))
    with TestClient(app) as client:
        client.app.state.task_runner.close()
        database = client.app.state.database
        broker = client.app.state.task_event_broker
        now = "2026-09-06T00:00:00.000+00:00"
        task = database.create_recognition_task({
            "id": "heartbeat-task",
            "idempotency_key": "heartbeat-key",
            "payload_md5": "payload",
            "text": "番茄2斤",
            "customer_id": 1,
            "site_id": 1,
            "created_at": now,
            "max_attempts": 3,
        })

        async def scenario() -> None:
            stream = recognition_task_event_stream(
                task["id"], ConnectedRequest(), database, broker, 0.01
            )
            first = await anext(stream)
            assert '"status":"pending"' in first
            assert broker.subscriber_count(task["id"]) == 1
            assert await anext(stream) == ": heartbeat\n\n"

            terminal = database.cancel_recognition_task(task["id"], now)
            publisher = threading.Thread(target=broker.publish, args=(terminal,))
            publisher.start()
            publisher.join(timeout=1)
            pushed = await anext(stream)
            while pushed.startswith(":"):
                pushed = await anext(stream)
            assert '"status":"cancelled"' in pushed
            with pytest.raises(StopAsyncIteration):
                await anext(stream)
            assert broker.subscriber_count(task["id"]) == 0

        asyncio.run(scenario())


def test_event_stream_disconnect_unregisters_subscription(tmp_path) -> None:
    class DisconnectingRequest:
        disconnected = False

        async def is_disconnected(self) -> bool:
            return self.disconnected

    app = create_app(str(tmp_path / "sse-disconnect.db"))
    with TestClient(app) as client:
        client.app.state.task_runner.close()
        database = client.app.state.database
        broker = client.app.state.task_event_broker
        now = "2026-09-06T00:00:00.000+00:00"
        task = database.create_recognition_task({
            "id": "disconnect-task",
            "idempotency_key": "disconnect-key",
            "payload_md5": "payload",
            "text": "番茄2斤",
            "customer_id": 1,
            "site_id": 1,
            "created_at": now,
            "max_attempts": 3,
        })
        request = DisconnectingRequest()

        async def scenario() -> None:
            stream = recognition_task_event_stream(
                task["id"], request, database, broker, 0.01
            )
            await anext(stream)
            request.disconnected = True
            with pytest.raises(StopAsyncIteration):
                await anext(stream)
            assert broker.subscriber_count(task["id"]) == 0

        asyncio.run(scenario())


def test_manual_cancel_and_retry_publish_complete_snapshots(tmp_path) -> None:
    app = create_app(str(tmp_path / "sse-actions.db"))
    with TestClient(app) as client:
        client.app.state.task_runner.close()
        database = client.app.state.database
        broker = client.app.state.task_event_broker
        now = "2026-09-06T00:00:00.000+00:00"

        pending = database.create_recognition_task({
            "id": "cancel-task", "idempotency_key": "cancel-key",
            "payload_md5": "one", "text": "番茄2斤", "customer_id": 1,
            "site_id": 1, "created_at": now, "max_attempts": 3,
        })
        failed = database.create_recognition_task({
            "id": "retry-task", "idempotency_key": "retry-key",
            "payload_md5": "two", "text": "番茄2斤", "customer_id": 1,
            "site_id": 1, "created_at": "2025-09-06T00:00:00.000+00:00",
            "max_attempts": 1,
        })
        database.claim_next_recognition_task(now)
        database.fail_recognition_task(
            failed["id"], "RuntimeError", "失败", now, now, retryable=False
        )
        async def scenario() -> None:
            cancel_subscription = broker.subscribe(pending["id"])
            response = client.post(
                f"/api/recognition-tasks/{pending['id']}/cancel"
            )
            assert response.status_code == 200
            cancel_event = await cancel_subscription.get(timeout=1)
            assert cancel_event.task["status"] == "cancelled"
            cancel_subscription.close()

            retry_subscription = broker.subscribe(failed["id"])
            response = client.post(
                f"/api/recognition-tasks/{failed['id']}/retry"
            )
            assert response.status_code == 200
            retry_event = await retry_subscription.get(timeout=1)
            assert retry_event.task["status"] == "pending"
            retry_subscription.close()

        asyncio.run(scenario())
