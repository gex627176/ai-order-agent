from __future__ import annotations

import hashlib
import json
from _thread import RLock as DraftReviewLock
from contextlib import contextmanager
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from ..database import Database
from ..task_runner import RecognitionTaskRunner
from ..workflow import OrderWorkflow
from .models import ApprovalGrant, ToolExecutionContext

class HarnessBusinessGateway:
    """Typed boundary from the Harness into existing application capabilities."""

    def __init__(
        self,
        database: Database,
        workflow: OrderWorkflow,
        task_runner: RecognitionTaskRunner,
        draft_review_lock: DraftReviewLock,
    ):
        self._database = database
        self._workflow = workflow
        self._task_runner = task_runner
        self._draft_review_lock = draft_review_lock

    def validate_session_scope(self, site_id: int, customer_id: int | None) -> None:
        self._database.get_active_sku_version(site_id)
        if customer_id is not None and not self._database.customer_exists(customer_id):
            raise ValueError("客户不存在")

    def list_customers(self) -> list[dict[str, Any]]:
        return self._database.list_customers()

    def customer_preferences(self, customer_id: int) -> list[dict[str, Any]]:
        if not self._database.customer_exists(customer_id):
            raise LookupError("客户不存在")
        return self._database.list_customer_preferences(customer_id)

    def catalog_snapshot(self, site_id: int) -> list[dict[str, Any]]:
        version = self._database.get_active_sku_version(site_id)
        return self._database.list_sku_snapshot(site_id, version)

    def recognize_draft(
        self, context: ToolExecutionContext, text: str
    ) -> dict[str, Any]:
        if context.customer_id is None:
            raise ValueError("请先选择客户")
        draft_id = None
        if context.request_key:
            draft_id = str(uuid5(
                NAMESPACE_URL,
                f"agent-draft:{context.session_id}:{context.request_key}",
            ))
            existing = self._database.get_draft(draft_id)
            if existing is not None:
                if (
                    existing["site_id"] != context.site_id
                    or existing["customer_id"] != context.customer_id
                    or existing["source_text"] != text
                ):
                    raise ValueError("幂等草稿与当前请求范围不一致")
                return existing
        return self._workflow.recognize(
            text,
            context.customer_id,
            context.site_id,
            draft_id=draft_id,
        )

    def order_for_draft(self, draft_id: str | None) -> dict[str, Any] | None:
        if not draft_id:
            return None
        return self._database.get_order_by_draft(draft_id)

    def verified_order(self, order_id: int | None) -> dict[str, Any] | None:
        if order_id is None:
            return None
        return self._database.get_order(order_id)

    @contextmanager
    def draft_review_guard(self):
        with self._draft_review_lock:
            yield

    def draft_for_binding(
        self,
        draft_id: str,
        site_id: int,
        customer_id: int | None,
    ) -> dict[str, Any]:
        draft = self._database.get_draft(draft_id)
        if draft is None:
            raise LookupError("草稿不存在")
        if draft["site_id"] != site_id or draft["customer_id"] != customer_id:
            raise DraftApprovalStale("草稿与当前 Agent 会话范围不一致")
        return draft

    def get_current_draft(self, context: ToolExecutionContext) -> dict[str, Any]:
        if not context.current_draft_id:
            raise LookupError("当前会话没有草稿")
        draft = self._database.get_draft(context.current_draft_id)
        if draft is None:
            raise LookupError("草稿不存在")
        if draft["site_id"] != context.site_id:
            raise LookupError("草稿不属于当前站点")
        if (
            context.customer_id is not None
            and draft["customer_id"] != context.customer_id
        ):
            raise LookupError("草稿客户与当前会话不一致")
        return draft

    def get_task(self, context: ToolExecutionContext, task_id: str) -> dict[str, Any]:
        task = self._database.get_recognition_task(task_id)
        if task is None or task["site_id"] != context.site_id:
            raise LookupError("识别任务不存在")
        if context.customer_id is not None and task["customer_id"] != context.customer_id:
            raise LookupError("识别任务不存在")
        return task

    def confirm_draft(
        self,
        context: ToolExecutionContext,
        grant: ApprovalGrant,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        if (
            not context.current_draft_id
            or grant.session_id != context.session_id
            or grant.draft_id != context.current_draft_id
            or grant.session_version != context.session_version
        ):
            raise PermissionError("审批凭证与当前会话状态不匹配")
        with self._draft_review_lock:
            try:
                draft = self.get_current_draft(context)
            except LookupError as exc:
                raise DraftApprovalStale(
                    "草稿在批准前已离开当前会话范围，请重新检查并审批"
                ) from exc
            if self.draft_fingerprint(draft) != grant.draft_fingerprint:
                raise DraftApprovalStale("草稿在批准前已发生变化，请重新检查并审批")
            return self._workflow.resume(
                context.current_draft_id,
                approved=True,
                idempotency_key=(
                    idempotency_key or f"agent-session:{context.session_id}"
                ),
            )

    @staticmethod
    def draft_fingerprint(draft: dict[str, Any]) -> str:
        payload = {
            "id": draft.get("id"),
            "status": draft.get("status"),
            "customer_id": draft.get("customer_id"),
            "site_id": draft.get("site_id"),
            "sku_version": draft.get("sku_version"),
            "items": draft.get("items", []),
            "warnings": draft.get("warnings", []),
        }
        canonical = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class DraftApprovalStale(ValueError):
    pass
