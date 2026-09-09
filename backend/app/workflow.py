from __future__ import annotations

import logging
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, TypedDict
from uuid import uuid4

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from .database import Database
from .llm_provider import DeepSeekExtractor
from .parser import parse_order_text
from .retrieval import SkuRetriever
from .settings import Settings


logger = logging.getLogger("agent.workflow")


class OrderState(TypedDict, total=False):
    task_id: str
    draft_id: str
    text: str
    customer_id: int | None
    site_id: int
    sku_version: int
    items: list[dict[str, Any]]
    warnings: list[str]
    trace: list[dict[str, Any]]
    provider: str
    retrieval: list[dict[str, Any]]
    preferences: list[dict[str, Any]]
    input_type: str
    source_filename: str
    input_confidence: float | None
    input_warnings: list[str]
    approved: bool
    idempotency_key: str
    order: dict[str, Any]


class OrderWorkflow:
    """LangGraph order workflow with a durable SQLite human-review checkpoint."""

    def __init__(
        self, database: Database, settings: Settings, retriever: SkuRetriever | None = None
    ):
        self.database = database
        self.settings = settings
        self.retriever = retriever or SkuRetriever(database, settings)
        checkpoint_path = Path(settings.checkpoint_path)
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        self._checkpoint_connection = sqlite3.connect(
            checkpoint_path, check_same_thread=False
        )
        self._checkpointer = SqliteSaver(self._checkpoint_connection)
        self._checkpointer.setup()
        self._checkpoint_lock = threading.RLock()
        self.graph = self._build_graph()

    def _build_graph(self):
        builder = StateGraph(OrderState)
        builder.add_node("parse_input", self._parse_node)
        builder.add_node("retrieve_sku", self._retrieve_node)
        builder.add_node("load_preferences", self._load_preferences_node)
        builder.add_node("validate_draft", self._validate_node)
        builder.add_node("save_draft", self._save_draft_node)
        builder.add_node("human_review", self._human_review_node)
        builder.add_node("confirm_order", self._confirm_order_node)
        builder.add_edge(START, "parse_input")
        builder.add_edge("parse_input", "retrieve_sku")
        builder.add_edge("retrieve_sku", "load_preferences")
        builder.add_edge("load_preferences", "validate_draft")
        builder.add_edge("validate_draft", "save_draft")
        builder.add_edge("save_draft", "human_review")
        builder.add_conditional_edges(
            "human_review",
            self._route_after_review,
            {"confirm": "confirm_order", "reject": END},
        )
        builder.add_edge("confirm_order", END)
        return builder.compile(checkpointer=self._checkpointer)

    def recognize(
        self,
        text: str,
        customer_id: int | None,
        site_id: int,
        input_type: str = "text",
        source_filename: str = "",
        input_confidence: float | None = None,
        input_warnings: list[str] | None = None,
        task_id: str | None = None,
        draft_id: str | None = None,
    ) -> dict[str, Any]:
        resolved_draft_id = draft_id or str(uuid4())
        existing = self.database.get_draft(resolved_draft_id)
        if existing is not None:
            return existing
        sku_version = self.database.get_active_sku_version(site_id)
        config = self._config(resolved_draft_id)
        with self._checkpoint_lock:
            existing = self.database.get_draft(resolved_draft_id)
            if existing is not None:
                return existing
            if self._checkpointer.get_tuple(config) is not None:
                self._checkpointer.delete_thread(resolved_draft_id)
            result = self.graph.invoke(
                {
                    "task_id": task_id or resolved_draft_id,
                    "draft_id": resolved_draft_id,
                    "text": text,
                    "customer_id": customer_id,
                    "site_id": site_id,
                    "sku_version": sku_version,
                    "input_type": input_type,
                    "source_filename": source_filename,
                    "input_confidence": input_confidence,
                    "input_warnings": input_warnings or [],
                    "trace": [],
                },
                config=config,
            )
        if "__interrupt__" not in result:
            raise RuntimeError("订单工作流未在人工审核节点暂停")
        draft = self.database.get_draft(resolved_draft_id)
        if draft is None:
            raise RuntimeError("工作流暂停后未找到订单草稿")
        return draft

    def resume(
        self, draft_id: str, approved: bool = True, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        existing = self.database.get_order_by_draft(draft_id)
        if existing:
            return existing
        with self._checkpoint_lock:
            snapshot = self.graph.get_state(self._config(draft_id))
        if not snapshot.values:
            raise LookupError("草稿没有可恢复的 LangGraph Checkpoint")
        with self._checkpoint_lock:
            result = self.graph.invoke(
                Command(resume={
                    "approved": approved,
                    "idempotency_key": idempotency_key or f"draft:{draft_id}",
                }),
                config=self._config(draft_id),
            )
        order = result.get("order")
        if not approved:
            raise ValueError("人工审核未通过")
        if not order:
            raise RuntimeError("工作流恢复后未创建订单")
        return order

    def checkpoint_status(self, draft_id: str) -> dict[str, Any]:
        with self._checkpoint_lock:
            snapshot = self.graph.get_state(self._config(draft_id))
        next_nodes = list(snapshot.next) if snapshot.values else []
        return {
            "thread_id": draft_id,
            "checkpoint_exists": bool(snapshot.values),
            "waiting_for_review": "human_review" in next_nodes,
            "next_nodes": next_nodes,
        }

    def close(self) -> None:
        with self._checkpoint_lock:
            self._checkpoint_connection.close()

    def _parse_node(self, state: OrderState) -> dict[str, Any]:
        actual_provider = "rules"

        def action():
            nonlocal actual_provider
            if self.settings.ai_provider == "deepseek":
                extractor = DeepSeekExtractor(
                    self.settings.deepseek_api_key,
                    self.settings.deepseek_base_url,
                    self.settings.deepseek_model,
                    self.settings.deepseek_prompt_version,
                    self.settings.deepseek_timeout_seconds,
                )
                model_started = time.perf_counter()
                try:
                    parsed = extractor.extract(state["text"])
                    model_duration_ms = round(
                        (time.perf_counter() - model_started) * 1000
                    )
                    self._record_llm_call(
                        state, extractor, "succeeded", model_duration_ms
                    )
                    actual_provider = "deepseek"
                    return parsed, f"DeepSeek 识别到 {len(parsed)} 条商品描述"
                except Exception as exc:
                    model_duration_ms = round(
                        (time.perf_counter() - model_started) * 1000
                    )
                    self._record_llm_call(
                        state, extractor, "fallback", model_duration_ms, exc
                    )
                    actual_provider = "rules(fallback)"
                    logger.warning(
                        "AGENT name=模型抽取 status=fallback reason=%s",
                        type(exc).__name__,
                    )
            parsed = parse_order_text(state["text"])
            return parsed, f"本地规则识别到 {len(parsed)} 条商品描述"

        items, trace = self._run_step(1, "解析订单文本", action, state)
        return {"items": items, "provider": actual_provider, "trace": trace}

    def _retrieve_node(self, state: OrderState) -> dict[str, Any]:
        def action():
            items, evidence, summary = self.retriever.enrich(
                state["items"], state["site_id"], state["sku_version"]
            )
            return (items, evidence), summary

        (items, evidence), trace = self._run_step(2, "站点级 SKU 混合召回", action, state)
        return {"items": items, "retrieval": evidence, "trace": trace}

    def _validate_node(self, state: OrderState) -> dict[str, Any]:
        warnings, trace = self._run_step(
            3,
            "校验订单草稿",
            lambda: self._validate(
                state.get("customer_id"), state["items"],
                state["site_id"], state["sku_version"],
            ),
            state,
        )
        return {
            "warnings": [*state.get("input_warnings", []), *warnings],
            "trace": trace,
        }

    def _load_preferences_node(self, state: OrderState) -> dict[str, Any]:
        customer_id = state.get("customer_id")
        preferences = (
            self.database.list_customer_preferences(customer_id)
            if customer_id is not None else []
        )
        logger.info(
            "AGENT name=加载客户偏好 status=completed customer_id=%s evidence=%d",
            customer_id, len(preferences),
        )
        return {"preferences": preferences}

    def _save_draft_node(self, state: OrderState) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        draft = {
            "id": state["draft_id"],
            "customer_id": state.get("customer_id"),
            "source_text": state["text"].strip(),
            "status": "needs_review",
            "items": state["items"],
            "warnings": state["warnings"],
            "trace": state["trace"],
            "provider": state["provider"],
            "site_id": state["site_id"],
            "sku_version": state["sku_version"],
            "retrieval": state.get("retrieval", []),
            "preferences": state.get("preferences", []),
            "input_type": state.get("input_type", "text"),
            "source_filename": state.get("source_filename", ""),
            "input_confidence": state.get("input_confidence"),
            "input_warnings": state.get("input_warnings", []),
            "created_at": now,
            "updated_at": now,
        }
        self.database.create_draft(draft)
        logger.info(
            "AGENT step=04 name=保存待确认草稿 status=completed draft_id=%s items=%d warnings=%d",
            state["draft_id"], len(state["items"]), len(state["warnings"]),
        )
        return {}

    @staticmethod
    def _human_review_node(state: OrderState) -> dict[str, Any]:
        decision = interrupt({
            "action": "review_order_draft",
            "draft_id": state["draft_id"],
            "item_count": len(state["items"]),
            "warnings": state["warnings"],
        })
        approved = decision.get("approved", False) if isinstance(decision, dict) else bool(decision)
        return {
            "approved": approved,
            "idempotency_key": decision.get("idempotency_key", f"draft:{state['draft_id']}")
            if isinstance(decision, dict) else f"draft:{state['draft_id']}",
        }

    @staticmethod
    def _route_after_review(state: OrderState) -> Literal["confirm", "reject"]:
        return "confirm" if state.get("approved") else "reject"

    def _confirm_order_node(self, state: OrderState) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        order = self.database.confirm_draft(
            state["draft_id"], now, state.get("idempotency_key")
        )
        logger.info(
            "AGENT step=05 name=人工确认并创建订单 status=completed order_no=%s",
            order["order_no"],
        )
        return {"order": order}

    def _run_step(self, step: int, name: str, action, state: OrderState):
        started = time.perf_counter()
        logger.info("AGENT step=%02d name=%s status=started", step, name)
        result, summary = action()
        duration_ms = round((time.perf_counter() - started) * 1000)
        logger.info(
            "AGENT step=%02d name=%s status=completed duration_ms=%d summary=%s",
            step, name, duration_ms, summary,
        )
        trace = [*state.get("trace", []), {
            "step": step,
            "name": name,
            "status": "completed",
            "summary": summary,
            "duration_ms": duration_ms,
        }]
        return result, trace

    def _record_llm_call(
        self,
        state: OrderState,
        extractor: DeepSeekExtractor,
        status: str,
        duration_ms: int,
        error: Exception | None = None,
    ) -> None:
        usage = extractor.last_usage
        prompt_tokens = int(usage.get("prompt_tokens", 0))
        completion_tokens = int(usage.get("completion_tokens", 0))
        total_tokens = int(usage.get("total_tokens", 0)) or (
            prompt_tokens + completion_tokens
        )
        estimated_cost = (
            prompt_tokens * self.settings.deepseek_input_cost_per_million
            + completion_tokens * self.settings.deepseek_output_cost_per_million
        ) / 1_000_000
        try:
            self.database.record_llm_call({
                "task_id": state.get("task_id", state["draft_id"]),
                "draft_id": state["draft_id"],
                "provider": "deepseek",
                "model": self.settings.deepseek_model,
                "prompt_version": extractor.prompt_version,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
                "estimated_cost": estimated_cost,
                "duration_ms": duration_ms,
                "status": status,
                "error_type": type(error).__name__ if error else "",
                "fallback_reason": (
                    "外部模型调用失败，已回退本地规则" if error else ""
                ),
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            })
        except Exception as record_error:
            logger.error(
                "METRICS action=record_llm_call status=failed reason=%s",
                type(record_error).__name__,
            )

    def _validate(
        self, customer_id: int | None, items: list[dict], site_id: int, sku_version: int
    ) -> tuple[list[str], str]:
        warnings = validate_edited_items(
            self.database, customer_id, items, site_id, sku_version
        )
        empty_message = "订单至少需要一条商品"
        if empty_message in warnings:
            warnings[warnings.index(empty_message)] = "未识别出商品，请点击“新增商品行”人工补录"
        return warnings, f"发现 {len(warnings)} 个待处理问题"

    @staticmethod
    def _config(thread_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": thread_id}}


def validate_edited_items(
    database: Database,
    customer_id: int | None,
    items: list[dict],
    site_id: int | None = None,
    sku_version: int | None = None,
) -> list[str]:
    warnings = []
    if customer_id is None:
        warnings.append("请选择客户后再确认订单")
    elif not database.customer_exists(customer_id):
        warnings.append("所选客户不存在")
    if not items:
        warnings.append("订单至少需要一条商品")
    for item in items:
        product = database.get_product(
            item.get("product_id"), site_id, sku_version
        ) if item.get("product_id") else None
        if not product or not product["active"]:
            warnings.append(f"第 {item['line_no']} 行商品未匹配")
    return warnings
