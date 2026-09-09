from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from ..agents import MainAgent, SkuResolutionAgent
from ..database import Database, IdempotencyConflictError
from ..settings import Settings
from ..task_runner import RecognitionTaskRunner
from ..tools import (
    register_catalog_tools,
    register_customer_tools,
    register_draft_tools,
    register_task_tools,
)
from ..workflow import OrderWorkflow
from .context import ContextAssembler
from .errors import (
    AgentIdempotencyConflict,
    AgentStateConflict,
    PlannerError,
    ToolArgumentsInvalid,
    ToolExecutionFailed,
    ToolNotFound,
)
from .gateway import DraftApprovalStale, DraftReviewLock, HarnessBusinessGateway
from .models import (
    ApprovalGrant,
    PendingAction,
    PendingActionName,
    PermissionLevel,
    PlannerTurn,
    SessionStatus,
    ToolExecutionContext,
)
from .permission import PermissionManager
from .planner import DeepSeekToolPlanner
from .session import AgentSessionStore
from .skills import SkillLoader
from .tool_registry import ToolRegistry


logger = logging.getLogger("agent.workflow")


class _ToolBudgetExceeded(RuntimeError):
    pass


@dataclass
class _ExecutionBudget:
    tool_limit: int
    tool_attempts: int = 0
    catalog_attempts: int = 0
    sku_fingerprints: set[str] = field(default_factory=set)

    def consume(self, *, catalog: bool = False) -> bool:
        if self.tool_attempts >= self.tool_limit:
            return False
        if catalog and self.catalog_attempts >= 3:
            return False
        self.tool_attempts += 1
        if catalog:
            self.catalog_attempts += 1
        return True


class AgentLoop:
    """Persistent, bounded Harness around the existing deterministic workflow."""

    def __init__(
        self,
        database: Database,
        workflow: OrderWorkflow,
        task_runner: RecognitionTaskRunner,
        settings: Settings,
        draft_review_lock: DraftReviewLock,
    ):
        if settings.agent_harness_provider not in {"rules", "deepseek"}:
            raise ValueError("AGENT_HARNESS_PROVIDER 只支持 rules 或 deepseek")
        self.store = AgentSessionStore(database)
        self.gateway = HarnessBusinessGateway(
            database, workflow, task_runner, draft_review_lock
        )
        self.context = ContextAssembler()
        self.permission_manager = PermissionManager()
        self.tools = ToolRegistry(self.permission_manager)
        register_customer_tools(self.tools, self.gateway)
        register_catalog_tools(self.tools, self.gateway)
        register_draft_tools(self.tools, self.gateway)
        register_task_tools(self.tools, self.gateway)
        loader = SkillLoader()
        self.main_agent = MainAgent(loader.load("order-entry", "order-entry"))
        self.sku_agent = SkuResolutionAgent(
            loader.load("sku-resolution", "sku-resolution")
        )
        self.planner = DeepSeekToolPlanner(settings)
        self.max_rounds = min(settings.agent_harness_max_rounds, 6)
        self.max_tool_calls = min(settings.agent_harness_max_tool_calls, 8)
        self._session_locks = tuple(threading.RLock() for _ in range(64))

    def initialize(self) -> None:
        self.store.initialize()

    def create_session(
        self, site_id: int, customer_id: int | None = None
    ) -> dict[str, Any]:
        self.gateway.validate_session_scope(site_id, customer_id)
        return self.store.create(site_id, customer_id)

    def get_session(
        self,
        session_id: str,
        after_sequence: int | None = None,
        event_limit: int = 100,
    ) -> dict[str, Any]:
        with self._session_lock(session_id):
            self._reconcile_completed_order(session_id)
            return self.store.get(session_id, after_sequence, event_limit)

    def handle_message(
        self,
        session_id: str,
        content: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        with self._session_lock(session_id):
            payload = {"content": content.strip()}
            request_digest, replay, _was_in_progress = self.store.begin_request(
                session_id,
                idempotency_key,
                "message",
                payload,
            )
            session = self._reconcile_completed_order(session_id)
            if replay is not None:
                if session["status"] == SessionStatus.COMPLETED.value:
                    self.store.complete_request(
                        session_id, request_digest, "message", payload, session
                    )
                    return session
                return replay
            if session["status"] == SessionStatus.COMPLETED.value:
                raise AgentStateConflict("已完成会话不能继续发送消息")
            message = content.strip()
            requested_customer = _requested_customer(message)
            if session.get("current_draft_id") and requested_customer:
                matches = [
                    customer for customer in self.gateway.list_customers()
                    if requested_customer in customer["name"].lower()
                    or requested_customer in customer.get("contact", "").lower()
                ]
                if (
                    len(matches) == 1
                    and session.get("customer_id") is not None
                    and matches[0]["id"] != session["customer_id"]
                ):
                    raise AgentStateConflict("已有草稿时不能切换到其他客户")
            self.store.append_event(
                session_id,
                "user",
                "user",
                "message_received",
                "completed",
                message,
                {"content": message},
            )
            session = self.context.compact(self.store, self.store.get(session_id))
            response = self._run_message(
                session, message, request_digest, _ExecutionBudget(self.max_tool_calls)
            )
            self.store.complete_request(
                session_id, request_digest, "message", payload, response
            )
            return response

    def resume(
        self,
        session_id: str,
        approved: bool,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        with self._session_lock(session_id):
            payload = {"approved": approved}
            request_digest, replay, was_in_progress = self.store.begin_request(
                session_id, idempotency_key, "resume", payload
            )
            session = self._reconcile_completed_order(session_id)
            if replay is not None:
                if session["status"] == SessionStatus.COMPLETED.value:
                    self.store.complete_request(
                        session_id, request_digest, "resume", payload, session
                    )
                    return session
                return replay
            if approved and session["status"] == SessionStatus.COMPLETED.value:
                self.store.complete_request(
                    session_id, request_digest, "resume", payload, session
                )
                return session
            pending = session.get("pending_action") or {}
            if was_in_progress and not approved and not pending:
                self.store.complete_request(
                    session_id, request_digest, "resume", payload, session
                )
                return session
            if pending.get("name") != PendingActionName.CONFIRM_ORDER.value:
                raise AgentStateConflict("当前没有可恢复的确认建单动作")
            if not approved:
                self.store.append_event(
                    session_id,
                    "permission",
                    "user",
                    "draft_confirm",
                    "rejected",
                    "人工拒绝确认建单",
                    {"approved": False},
                )
                response = self.store.update(
                    session_id,
                    expected_version=session["version"],
                    status=SessionStatus.ACTIVE.value,
                    pending_action=None,
                    last_message="已拒绝建单，草稿仍保留为待审核状态。",
                )
                self.store.complete_request(
                    session_id, request_digest, "resume", payload, response
                )
                return response

            context = self._tool_context(session, request_digest)
            grant = ApprovalGrant(
                session_id=session_id,
                draft_id=session["current_draft_id"],
                session_version=session["version"],
                draft_fingerprint=str(pending.get("arguments", {}).get(
                    "draft_fingerprint", ""
                )),
            )
            self.store.append_event(
                session_id,
                "permission",
                "user",
                "draft_confirm",
                "approved",
                "人工批准确认建单",
                {"approved": True},
            )
            budget = _ExecutionBudget(self.max_tool_calls)
            if not budget.consume():
                raise AgentStateConflict("确认建单工具预算不足")
            try:
                result = self._call_tool(
                    session_id,
                    "draft_confirm",
                    {"idempotency_key": request_digest},
                    context,
                    actor="harness",
                    grant=grant,
                )
            except ToolExecutionFailed as exc:
                if isinstance(exc.__cause__, IdempotencyConflictError):
                    raise AgentIdempotencyConflict(str(exc)) from exc
                if isinstance(exc.__cause__, DraftApprovalStale):
                    current = self.store.get(session_id)
                    self.store.update(
                        session_id,
                        expected_version=current["version"],
                        status=SessionStatus.WAITING_INPUT.value,
                        pending_action=PendingAction(
                            name=PendingActionName.REVIEW_SKU,
                            permission=PermissionLevel.READ,
                            arguments={"draft_id": current["current_draft_id"]},
                        ).model_dump(mode="json"),
                        last_message=(
                            "草稿在批准前已发生变化，请重新检查后再次审批。"
                        ),
                    )
                    raise AgentStateConflict(
                        "草稿在批准前已发生变化，请重新检查后再次审批"
                    ) from exc
                raise
            order = result.data
            self.store.append_event(
                session_id,
                "assistant",
                "main_agent",
                "order_completed",
                "completed",
                f"订单 {order['order_no']} 已创建",
                {"content": f"订单 {order['order_no']} 已创建。", "order_id": order["id"]},
            )
            response = self.store.update(
                session_id,
                expected_version=session["version"],
                status=SessionStatus.COMPLETED.value,
                current_order_id=order["id"],
                pending_action=None,
                last_message=f"订单 {order['order_no']} 已创建。",
            )
            self.store.complete_request(
                session_id, request_digest, "resume", payload, response
            )
            return response

    def _run_message(
        self,
        session: dict[str, Any],
        message: str,
        request_key: str | None,
        budget: _ExecutionBudget,
    ) -> dict[str, Any]:
        context = self.context.assemble(session)
        pending_name = (session.get("pending_action") or {}).get("name")
        if pending_name == PendingActionName.CONFIRM_ORDER.value:
            return self._reply(
                session,
                "草稿已准备好；请通过 resume 接口明确批准或拒绝建单。",
            )

        allowed = self._allowed_tools(pending_name)
        model_messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.main_agent.system_message(context)},
            *context["messages"],
        ]
        fallback_used = False
        collected: list[tuple[str, Any]] = []
        last_turn: PlannerTurn | None = None

        for round_no in range(1, self.max_rounds + 1):
            use_model = self.planner.enabled and not fallback_used
            try:
                turn = (
                    self.planner.complete(
                        model_messages, self.tools.model_tools(allowed)
                    )
                    if use_model
                    else self.main_agent.fallback(message, context)
                )
                self._record_planner_event(session["id"], turn, round_no, fallback_used)
            except PlannerError as exc:
                fallback_used = True
                self.store.append_event(
                    session["id"],
                    "planner",
                    "main_agent",
                    "model_planner",
                    "fallback",
                    "模型规划失败，已切换本地规则",
                    {"error_code": "planner_unavailable"},
                )
                turn = self.main_agent.fallback(message, context)
            last_turn = turn
            if not turn.tool_calls:
                if (
                    session.get("customer_id") is None
                    and _looks_like_order_request(message)
                ):
                    pending = PendingAction(
                        name=PendingActionName.PROVIDE_CUSTOMER,
                        permission=PermissionLevel.READ,
                        arguments={
                            "order_text": self.main_agent.order_text(message)
                        },
                    ).model_dump(mode="json")
                    return self.store.update(
                        session["id"],
                        expected_version=session["version"],
                        status=SessionStatus.WAITING_INPUT.value,
                        pending_action=pending,
                        last_message=(
                            "请先告诉我客户的完整名称，再继续生成订单草稿。"
                        ),
                    )
                text = (
                    self._format_results(collected)
                    if use_model and collected
                    else (
                        "模型未执行可验证的本地操作；当前没有已创建订单。"
                        if use_model
                        else (turn.content or self._format_results(collected))
                    )
                )
                return self._reply(self.store.get(session["id"]), text)

            assistant_calls: list[dict[str, Any]] = []
            tool_messages: list[dict[str, Any]] = []
            for call in turn.tool_calls:
                if not budget.consume(catalog=call.name == "catalog_search"):
                    return self._budget_reply(session["id"], budget)
                if call.name not in allowed:
                    fallback_used = True
                    self._record_rejected_tool(session["id"], call.name, "not_allowed")
                    break
                if call.name == "draft_recognize" and session.get("customer_id") is None:
                    pending = PendingAction(
                        name=PendingActionName.PROVIDE_CUSTOMER,
                        permission=PermissionLevel.READ,
                        arguments={
                            "order_text": self.main_agent.order_text(message)
                        },
                    ).model_dump(mode="json")
                    return self.store.update(
                        session["id"],
                        expected_version=session["version"],
                        status=SessionStatus.WAITING_INPUT.value,
                        pending_action=pending,
                        last_message="请先告诉我客户的完整名称，再继续生成订单草稿。",
                    )
                try:
                    current = self.store.get(session["id"])
                    if call.name in {"draft_recognize", "draft_get"}:
                        with self.gateway.draft_review_guard():
                            result = self._call_tool(
                                session["id"], call.name, call.arguments,
                                self._tool_context(current, request_key),
                            )
                            transition = self._apply_tool_result(
                                self.store.get(session["id"]), call.name,
                                result.data, request_key, budget,
                            )
                    else:
                        result = self._call_tool(
                            session["id"], call.name, call.arguments,
                            self._tool_context(current, request_key),
                        )
                        transition = self._apply_tool_result(
                            self.store.get(session["id"]), call.name,
                            result.data, request_key, budget,
                        )
                except (ToolNotFound, ToolArgumentsInvalid, ToolExecutionFailed) as exc:
                    if not use_model:
                        return self._reply(
                            self.store.get(session["id"]),
                            f"无法安全执行本次请求：{str(exc)[:120]}",
                        )
                    fallback_used = True
                    self._record_rejected_tool(
                        session["id"], call.name, "invalid_or_failed"
                    )
                    break
                collected.append((call.name, result.data))
                if transition is not None:
                    return transition
                assistant_calls.append({
                    "id": call.id or f"call-{budget.tool_attempts}",
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False),
                    },
                })
                tool_messages.append({
                    "role": "tool",
                    "tool_call_id": call.id or f"call-{budget.tool_attempts}",
                    "content": json.dumps(
                        _model_result(call.name, result.data), ensure_ascii=False
                    ),
                })
            else:
                if not self.planner.enabled or fallback_used:
                    return self._reply(
                        self.store.get(session["id"]), self._format_results(collected)
                    )
                model_messages.append({
                    "role": "assistant",
                    "content": turn.content or None,
                    "tool_calls": assistant_calls,
                })
                model_messages.extend(tool_messages)
                continue

            if fallback_used:
                return self._execute_rule_fallback(
                    session["id"], message, context, allowed, request_key,
                    budget, collected,
                )

        self.store.append_event(
            session["id"], "planner", "harness", "round_budget", "stopped",
            "Agent 循环达到安全轮次上限", {"limit": self.max_rounds},
        )
        return self._reply(
            self.store.get(session["id"]),
            (last_turn.content if last_turn else "")
            or "本轮处理达到安全轮次上限，请缩小问题范围后重试。",
        )

    def _apply_tool_result(
        self,
        session: dict[str, Any],
        name: str,
        data: Any,
        request_key: str | None,
        budget: _ExecutionBudget,
    ) -> dict[str, Any] | None:
        if name == "customer_search":
            if len(data) != 1:
                return self._reply(
                    session, "没有唯一匹配到客户，请提供完整客户名称。"
                )
            customer = data[0]
            if (
                session.get("current_draft_id")
                and session.get("customer_id") is not None
                and customer["id"] != session["customer_id"]
            ):
                raise AgentStateConflict("已有草稿时不能切换到其他客户")
            pending = session.get("pending_action") or {}
            order_text = pending.get("arguments", {}).get("order_text", "")
            session = self.store.update(
                session["id"],
                expected_version=session["version"],
                customer_id=customer["id"],
                status=SessionStatus.ACTIVE.value,
                pending_action=None,
                last_message=f"已选择客户 {customer['name']}。",
            )
            if order_text:
                if not budget.consume():
                    return self._budget_reply(session["id"], budget)
                with self.gateway.draft_review_guard():
                    result = self._call_tool(
                        session["id"],
                        "draft_recognize",
                        {"text": order_text},
                        self._tool_context(session, request_key),
                    )
                    return self._apply_draft(session, result.data, budget)
            return session
        if name in {"draft_recognize", "draft_get"}:
            return self._apply_draft(session, data, budget)
        return None

    def _apply_draft(
        self,
        session: dict[str, Any],
        draft: dict[str, Any],
        budget: _ExecutionBudget,
    ) -> dict[str, Any]:
        with self.gateway.draft_review_guard():
            try:
                draft = self.gateway.draft_for_binding(
                    draft["id"], session["site_id"], session.get("customer_id")
                )
            except DraftApprovalStale as exc:
                raise AgentStateConflict(str(exc)) from exc
            return self._bind_draft(session, draft, budget)

    def _bind_draft(
        self,
        session: dict[str, Any],
        draft: dict[str, Any],
        budget: _ExecutionBudget,
    ) -> dict[str, Any]:
        has_unmatched = not draft.get("items") or any(
            not item.get("matched") for item in draft.get("items", [])
        )
        if has_unmatched:
            fingerprint = self.gateway.draft_fingerprint(draft)
            session_with_draft = self.store.update(
                session["id"],
                expected_version=session["version"],
                status=SessionStatus.WAITING_INPUT.value,
                current_draft_id=draft["id"],
                pending_action=PendingAction(
                    name=PendingActionName.REVIEW_SKU,
                    permission=PermissionLevel.DRAFT_WRITE,
                    arguments={"draft_id": draft["id"]},
                ).model_dump(mode="json"),
                last_message="草稿含未匹配商品，正在整理候选证据。",
            )

            if (
                fingerprint in budget.sku_fingerprints
                or not self.store.reserve_sku_delegation(session["id"], fingerprint)
            ):
                return self.store.update(
                    session["id"],
                    expected_version=session_with_draft["version"],
                    last_message=(
                        "草稿仍含未匹配商品；候选分析已执行过，"
                        "请先人工修正草稿后再重新检查。"
                    ),
                )
            budget.sku_fingerprints.add(fingerprint)

            def catalog_search(arguments: dict[str, Any]) -> list[dict[str, Any]]:
                if not budget.consume(catalog=True):
                    raise _ToolBudgetExceeded("SKU 目录检索预算已耗尽")
                current = self.store.get(session["id"])
                return self._call_tool(
                    session["id"],
                    "catalog_search",
                    arguments,
                    self._tool_context(current, None),
                    actor="sku_resolution_agent",
                ).data

            resolution = self.sku_agent.resolve(
                draft,
                catalog_search,
                self.planner,
                self.tools.model_tools({"catalog_search"}),
            )
            self.store.append_event(
                session["id"],
                "delegation",
                "main_agent",
                "sku_resolution_agent",
                "completed",
                "已委派 SKU 候选分析，结果只供人工核对",
                _safe_result(resolution),
            )
            return self.store.update(
                session["id"],
                expected_version=session_with_draft["version"],
                last_message=(
                    "草稿含未匹配商品，已生成候选证据；"
                    "请在原草稿审核入口人工修正后告诉我“重新检查”。"
                ),
            )

        return self.store.update(
            session["id"],
            expected_version=session["version"],
            status=SessionStatus.WAITING_APPROVAL.value,
            current_draft_id=draft["id"],
            pending_action=PendingAction(
                name=PendingActionName.CONFIRM_ORDER,
                permission=PermissionLevel.TRANSACTION_WRITE,
                arguments={
                    "draft_id": draft["id"],
                    "draft_fingerprint": self.gateway.draft_fingerprint(draft),
                },
            ).model_dump(mode="json"),
            last_message="订单草稿已生成并通过目录校验，等待人工批准后建单。",
        )

    def _execute_rule_fallback(
        self,
        session_id: str,
        message: str,
        context: dict[str, Any],
        allowed: set[str],
        request_key: str | None,
        budget: _ExecutionBudget,
        collected: list[tuple[str, Any]],
    ) -> dict[str, Any]:
        turn = self.main_agent.fallback(message, context)
        if not turn.tool_calls:
            return self._reply(
                self.store.get(session_id),
                turn.content or "无法安全执行本次请求，请换一种说法。",
            )
        for call in turn.tool_calls:
            if not budget.consume(catalog=call.name == "catalog_search"):
                return self._budget_reply(session_id, budget)
            if call.name not in allowed:
                self._record_rejected_tool(
                    session_id, call.name, "not_allowed"
                )
                return self._reply(
                    self.store.get(session_id), "无法安全执行本次请求，请换一种说法。"
                )
            try:
                current = self.store.get(session_id)
                if call.name in {"draft_recognize", "draft_get"}:
                    with self.gateway.draft_review_guard():
                        result = self._call_tool(
                            session_id, call.name, call.arguments,
                            self._tool_context(current, request_key),
                        )
                        transition = self._apply_tool_result(
                            self.store.get(session_id), call.name, result.data,
                            request_key, budget,
                        )
                else:
                    result = self._call_tool(
                        session_id, call.name, call.arguments,
                        self._tool_context(current, request_key),
                    )
                    transition = self._apply_tool_result(
                        self.store.get(session_id), call.name, result.data,
                        request_key, budget,
                    )
            except (ToolNotFound, ToolArgumentsInvalid, ToolExecutionFailed):
                return self._reply(
                    self.store.get(session_id),
                    "本地业务工具暂时不可用，请稍后重试。",
                )
            collected.append((call.name, result.data))
            if transition is not None:
                return transition
        return self._reply(
            self.store.get(session_id), self._format_results(collected)
        )

    def _call_tool(
        self,
        session_id: str,
        name: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
        actor: str = "main_agent",
        grant: ApprovalGrant | None = None,
    ):
        definition = self.tools.definition(name)
        started = time.perf_counter()
        self.store.append_event(
            session_id,
            "tool",
            actor,
            name,
            "started",
            f"调用工具 {name}",
            {"arguments": _safe_arguments(arguments), "permission": definition.permission.value},
        )
        try:
            result = self.tools.execute(name, arguments, context, grant)
        except (ToolArgumentsInvalid, ToolExecutionFailed) as exc:
            self.store.append_event(
                session_id,
                "tool",
                actor,
                name,
                "failed",
                f"工具 {name} 执行失败",
                {
                    "error_code": exc.code,
                    "duration_ms": round((time.perf_counter() - started) * 1000),
                },
            )
            raise
        self.store.append_event(
            session_id,
            "tool",
            actor,
            name,
            result.status,
            result.summary,
            {
                "result": _safe_result(result.data),
                "duration_ms": round((time.perf_counter() - started) * 1000),
            },
        )
        return result

    def _record_planner_event(
        self,
        session_id: str,
        turn: PlannerTurn,
        round_no: int,
        fallback: bool,
    ) -> None:
        self.store.append_event(
            session_id,
            "planner",
            "main_agent",
            "rule_planner" if fallback or not self.planner.enabled else "model_planner",
            "completed",
            f"完成第 {round_no} 轮规划",
            {
                "round": round_no,
                "tool_names": [call.name for call in turn.tool_calls],
                "duration_ms": turn.duration_ms,
                "prompt_tokens": turn.prompt_tokens,
                "completion_tokens": turn.completion_tokens,
                "total_tokens": turn.total_tokens,
            },
        )

    def _record_rejected_tool(
        self, session_id: str, name: str, reason_code: str
    ) -> None:
        self.store.append_event(
            session_id,
            "tool",
            "harness",
            name or "unknown_tool",
            "rejected",
            "工具调用被 Harness 拒绝",
            {"error_code": "tool_rejected", "reason_code": reason_code},
        )

    def _reply(
        self, session: dict[str, Any], message: str
    ) -> dict[str, Any]:
        order = self.gateway.verified_order(session.get("current_order_id"))
        text = (
            f"订单 {order['order_no']} 已创建。"
            if order is not None
            else (message.strip() or "请求已处理；当前没有已创建订单。")
        )
        self.store.append_event(
            session["id"],
            "assistant",
            "main_agent",
            "message_sent",
            "completed",
            text,
            {"content": text},
        )
        return self.store.update(
            session["id"],
            expected_version=session["version"],
            last_message=text,
        )

    def _budget_reply(
        self, session_id: str, budget: _ExecutionBudget
    ) -> dict[str, Any]:
        self.store.append_event(
            session_id,
            "planner",
            "harness",
            "tool_budget",
            "stopped",
            "工具调用达到安全上限",
            {
                "limit": budget.tool_limit,
                "attempts": budget.tool_attempts,
                "catalog_attempts": budget.catalog_attempts,
            },
        )
        return self._reply(
            self.store.get(session_id),
            "本轮工具调用已达到安全上限，请缩小问题范围后重试。",
        )

    def _reconcile_completed_order(self, session_id: str) -> dict[str, Any]:
        session = self.store.get(session_id)
        if session.get("current_order_id"):
            return session
        if session.get("current_draft_id"):
            try:
                self.gateway.get_current_draft(self._tool_context(session, None))
            except LookupError as exc:
                raise AgentStateConflict(
                    "当前草稿与 Agent 会话范围不一致"
                ) from exc
        order = self.gateway.order_for_draft(session.get("current_draft_id"))
        if order is None:
            return session
        if (
            order["site_id"] != session["site_id"]
            or (
                session.get("customer_id") is not None
                and order["customer_id"] != session["customer_id"]
            )
        ):
            raise AgentStateConflict("已确认订单与 Agent 会话范围不一致")
        self.store.append_event(
            session_id,
            "system",
            "harness",
            "order_reconciled",
            "completed",
            "已根据本地持久订单事实收敛会话状态",
            {"order_id": order["id"]},
        )
        return self.store.update(
            session_id,
            expected_version=session["version"],
            status=SessionStatus.COMPLETED.value,
            current_order_id=order["id"],
            pending_action=None,
            last_message=f"订单 {order['order_no']} 已创建。",
        )

    def assert_draft_customer_change_allowed(
        self, draft_id: str, customer_id: int | None
    ) -> None:
        self.store.assert_draft_customer_change_allowed(draft_id, customer_id)

    @staticmethod
    def _format_results(results: list[tuple[str, Any]]) -> str:
        sections: list[str] = []
        for name, data in results:
            if name == "customer_list":
                sections.append("客户：" + "、".join(item["name"] for item in data))
            elif name == "catalog_search":
                sections.append("商品：" + "、".join(
                    f"{item['name']}（{item['unit_price']}/{item['unit']}）"
                    for item in data
                ))
            elif name == "customer_preferences":
                sections.append(f"已记录 {len(data)} 条客户偏好证据")
            elif name == "recognition_task_get":
                sections.append(f"识别任务状态：{data['status']}")
        return "；".join(sections) or "请求已处理。"

    @staticmethod
    def _allowed_tools(pending_name: str | None) -> set[str]:
        if pending_name == PendingActionName.PROVIDE_CUSTOMER.value:
            return {"customer_search"}
        if pending_name == PendingActionName.REVIEW_SKU.value:
            return {"draft_get"}
        return {
            "customer_list",
            "customer_search",
            "customer_preferences",
            "catalog_search",
            "draft_recognize",
            "draft_get",
            "recognition_task_get",
        }

    @staticmethod
    def _tool_context(
        session: dict[str, Any], request_key: str | None
    ) -> ToolExecutionContext:
        return ToolExecutionContext(
            session_id=session["id"],
            site_id=session["site_id"],
            customer_id=session.get("customer_id"),
            current_draft_id=session.get("current_draft_id"),
            session_version=session["version"],
            request_key=request_key,
        )

    def _session_lock(self, session_id: str) -> DraftReviewLock:
        digest = hashlib.blake2s(session_id.encode("utf-8"), digest_size=1).digest()
        return self._session_locks[digest[0] % len(self._session_locks)]


def _safe_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value for key, value in arguments.items()
        if key not in {"idempotency_key", "api_key", "authorization"}
    }


def _looks_like_order_request(message: str) -> bool:
    normalized = message.strip().lower()
    if any(
        phrase in normalized
        for phrase in ("录单", "录入", "下单", "订单", "订购", "帮我录", "请录")
    ):
        return True
    return bool(re.search(
        r"\d+(?:\.\d+)?\s*(?:斤|公斤|千克|克|箱|件|袋|包|瓶|盒|桶|个|份)",
        normalized,
    ))


def _requested_customer(message: str) -> str:
    normalized = message.strip()
    for prefix in ("客户是", "客户：", "客户:"):
        if normalized.startswith(prefix):
            return normalized[len(prefix):].strip().lower()
    return ""


def _safe_result(data: Any) -> Any:
    if isinstance(data, list):
        return [_safe_result(item) for item in data[:20]]
    if not isinstance(data, dict):
        return data
    if "items" in data and "source_text" in data:
        return {
            "id": data.get("id"),
            "status": data.get("status"),
            "item_count": len(data.get("items", [])),
            "unmatched_count": sum(
                1 for item in data.get("items", []) if not item.get("matched")
            ),
            "warnings": data.get("warnings", [])[:10],
            "site_id": data.get("site_id"),
            "sku_version": data.get("sku_version"),
        }
    allowed = {
        "id", "order_no", "name", "sku", "aliases", "unit", "unit_price",
        "active", "status", "task_id", "draft_id", "requires_manual_edit",
        "planner_fallback", "unresolved", "line_no", "raw_product_name",
        "catalog_candidates", "retrieval_evidence", "order_id",
        "planner_metrics", "duration_ms", "prompt_tokens",
        "completion_tokens", "total_tokens", "tool_names",
    }
    return {
        key: _safe_result(value)
        for key, value in data.items()
        if key in allowed
    }


def _model_result(tool_name: str, data: Any) -> Any:
    fields_by_tool = {
        "customer_list": {"id", "name"},
        "customer_search": {"id", "name"},
        "customer_preferences": {
            "preference_key", "preference_value", "evidence_count",
        },
        "catalog_search": {
            "id", "sku", "name", "aliases", "unit", "unit_price", "active",
        },
        "draft_recognize": {
            "id", "status", "site_id", "sku_version", "warnings",
            "item_count", "unmatched_count",
        },
        "draft_get": {
            "id", "status", "site_id", "sku_version", "warnings",
            "item_count", "unmatched_count",
        },
        "recognition_task_get": {
            "id", "status", "draft_id", "attempt_count", "max_attempts",
            "requires_manual_edit",
        },
    }
    allowed = fields_by_tool.get(tool_name, set())

    def project(value: Any) -> Any:
        if isinstance(value, list):
            return [project(item) for item in value[:20]]
        if not isinstance(value, dict):
            return value
        if tool_name in {"draft_recognize", "draft_get"} and "items" in value:
            value = {
                **value,
                "item_count": len(value.get("items", [])),
                "unmatched_count": sum(
                    1 for item in value.get("items", []) if not item.get("matched")
                ),
            }
        return {key: project(item) for key, item in value.items() if key in allowed}

    return project(data)
