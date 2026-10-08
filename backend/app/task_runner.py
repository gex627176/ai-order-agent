from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from .database import Database
from .error_handling import public_error
from .settings import Settings
from .task_events import RecognitionTaskEventBroker
from .workflow import OrderWorkflow


logger = logging.getLogger("agent.workflow")


class RecognitionTaskRunner:
    """Durable single-worker recognition queue for the local application."""

    def __init__(
        self,
        database: Database,
        workflow: OrderWorkflow,
        settings: Settings,
        event_broker: RecognitionTaskEventBroker | None = None,
    ):
        self.database = database
        self.workflow = workflow
        self.settings = settings
        self.event_broker = event_broker
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="recognition-task-worker",
            daemon=True,
        )

    def start(self) -> None:
        now = self._now()
        recovered = self.database.recover_interrupted_recognition_tasks(now)
        self._thread.start()
        self._wake.set()
        logger.info(
            "TASK action=worker_start status=completed recovered=%d", recovered
        )

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=35)
        if self._thread.is_alive():
            logger.warning("TASK action=worker_stop status=timeout")
        else:
            logger.info("TASK action=worker_stop status=completed")

    def create_task(
        self,
        text: str,
        customer_id: int | None,
        site_id: int,
        idempotency_key: str | None = None,
        input_type: str = "text",
        source_filename: str = "",
        input_confidence: float | None = None,
        input_warnings: list[str] | None = None,
    ) -> dict[str, Any]:
        task_id = str(uuid4())
        resolved_key = idempotency_key or f"task:{task_id}"
        payload = {
            "text": text.strip(),
            "customer_id": customer_id,
            "site_id": site_id,
            "input_type": input_type,
            "source_filename": source_filename,
            "input_confidence": input_confidence,
            "input_warnings": input_warnings or [],
        }
        now = self._now()
        task = self.database.create_recognition_task({
            "id": task_id,
            "idempotency_key": resolved_key,
            "payload_md5": self._payload_md5(payload),
            **payload,
            "max_attempts": self.settings.recognition_task_max_attempts,
            "created_at": now,
        })
        self._publish(task)
        self._wake.set()
        return task

    def wake(self) -> None:
        self._wake.set()

    def _run_loop(self) -> None:
        poll_interval = max(self.settings.recognition_task_poll_interval, 0.05)
        while not self._stop.is_set():
            task = self.database.claim_next_recognition_task(self._now())
            if task is None:
                self._wake.wait(poll_interval)
                self._wake.clear()
                continue
            self._publish(task)
            self._execute(task)

    def _execute(self, task: dict[str, Any]) -> None:
        task_id = task["id"]
        logger.info(
            "TASK id=%s status=running attempt=%d/%d",
            task_id, task["attempt_count"], task["max_attempts"],
        )
        try:
            existing_draft = self.database.get_draft(task_id)
            if existing_draft is not None:
                completed = self.database.complete_recognition_task(
                    task_id, existing_draft["id"], self._now()
                )
                logger.info(
                    "TASK id=%s status=%s draft_id=%s recovered=true",
                    task_id, completed["status"], existing_draft["id"],
                )
                self._publish(completed)
                return
            draft = self.workflow.recognize(
                task["text"],
                task["customer_id"],
                task["site_id"],
                input_type=task["input_type"],
                source_filename=task["source_filename"],
                input_confidence=task.get("input_confidence"),
                input_warnings=task.get("input_warnings", []),
                task_id=task_id,
                draft_id=task_id,
            )
        except Exception as exc:
            delay_seconds = min(2 ** max(task["attempt_count"] - 1, 0), 8)
            retryable = not isinstance(
                exc, (LookupError, ValueError, sqlite3.IntegrityError)
            )
            now_value = datetime.now(timezone.utc)
            failed = self.database.fail_recognition_task(
                task_id,
                type(exc).__name__,
                self._public_error_message(exc),
                now_value.isoformat(timespec="milliseconds"),
                (now_value + timedelta(seconds=delay_seconds)).isoformat(
                    timespec="milliseconds"
                ),
                retryable=retryable,
            )
            logger.warning(
                "TASK id=%s status=%s attempt=%d reason=%s retry_in_seconds=%d",
                task_id, failed["status"], failed["attempt_count"],
                type(exc).__name__, delay_seconds if retryable else 0,
            )
            self._publish(failed)
            return
        completed = self.database.complete_recognition_task(
            task_id, draft["id"], self._now()
        )
        logger.info(
            "TASK id=%s status=%s draft_id=%s",
            task_id, completed["status"], draft["id"],
        )
        self._publish(completed)

    def publish(self, task: dict[str, Any]) -> None:
        self._publish(task)

    def _publish(self, task: dict[str, Any]) -> None:
        if self.event_broker is not None:
            self.event_broker.publish(task)

    @staticmethod
    def _payload_md5(payload: dict[str, Any]) -> str:
        canonical = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.md5(
            canonical.encode("utf-8"), usedforsecurity=False
        ).hexdigest()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds")

    @staticmethod
    def _public_error_message(error: Exception) -> str:
        if isinstance(error, LookupError):
            return "识别所需的客户或站点不存在"
        if isinstance(error, ValueError):
            return public_error(error, "订单内容无法识别")
        return "识别暂时失败，请稍后重试"
