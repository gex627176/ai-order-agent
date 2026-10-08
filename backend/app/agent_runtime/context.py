from __future__ import annotations

from typing import Any

from ..memory import CustomerMemoryService
from .session import AgentSessionStore


class ContextAssembler:
    """Build bounded model context from structured state, not a flat chat dump."""

    def __init__(
        self,
        customer_memory: CustomerMemoryService,
        recent_event_limit: int = 16,
        summary_limit: int = 2000,
        milestone_limit: int = 8,
    ):
        self.customer_memory = customer_memory
        self.recent_event_limit = recent_event_limit
        self.summary_limit = summary_limit
        self.milestone_limit = milestone_limit

    def compact(
        self, store: AgentSessionStore, session: dict[str, Any]
    ) -> dict[str, Any]:
        cutoff = session["last_event_sequence"] - self.recent_event_limit
        memory = store.get_memory(session["id"])
        cursor = int(memory["through_sequence"])
        if cutoff <= cursor:
            return session
        events = store.events_between(session["id"], cursor + 1, cutoff)
        reduced = self._reduce(memory, events, session)
        next_memory = {
            **reduced,
            "through_sequence": cutoff,
        }
        return store.save_compaction(
            session["id"],
            expected_session_version=session["version"],
            through_sequence=cutoff,
            objective=reduced["objective"],
            recent_intent=reduced["recent_intent"],
            unresolved=reduced["unresolved"],
            milestones=reduced["milestones"],
            context_summary=self._compatibility_summary(next_memory),
        )

    def assemble(
        self, session: dict[str, Any], store: AgentSessionStore
    ) -> dict[str, Any]:
        messages: list[dict[str, str]] = []
        for event in session.get("events", [])[-self.recent_event_limit:]:
            if event["type"] not in {"user", "assistant"}:
                continue
            content = str(event.get("payload", {}).get("content") or event["summary"])
            messages.append({
                "role": "user" if event["type"] == "user" else "assistant",
                "content": content[:5000],
            })
        session_memory = store.get_memory(session["id"])
        facts = self.customer_memory.for_session(session)
        live_unresolved = self._pending_memory(session.get("pending_action"))
        return {
            "session_id": session["id"],
            "site_id": session["site_id"],
            "customer_id": session.get("customer_id"),
            "current_draft_id": session.get("current_draft_id"),
            "pending_action": session.get("pending_action"),
            "session_memory": {
                "objective": session_memory["objective"],
                "recent_intent": session_memory["recent_intent"],
                "unresolved": live_unresolved,
                "milestones": session_memory["milestones"],
                "through_sequence": session_memory["through_sequence"],
            },
            "business_memory": self.customer_memory.project_for_model(facts),
            "summary": self._compatibility_summary(session_memory),
            "messages": messages,
        }

    def _reduce(
        self,
        current: dict[str, Any],
        events: list[dict[str, Any]],
        session: dict[str, Any],
    ) -> dict[str, Any]:
        objective = str(current.get("objective", ""))
        recent_intent = str(current.get("recent_intent", ""))
        milestones = list(current.get("milestones", []))
        for event in events:
            summary = str(event.get("summary", "")).strip()
            if event.get("type") == "user" and summary:
                if not objective:
                    objective = summary[:1000]
                recent_intent = summary[:1000]
            if event.get("type") in {"tool", "permission", "subagent"}:
                milestones.append({
                    "sequence": int(event["sequence"]),
                    "name": str(event.get("name", ""))[:100],
                    "status": str(event.get("status", ""))[:40],
                    "summary": summary[:300],
                })
        unresolved = self._pending_memory(session.get("pending_action"))
        return {
            "objective": objective,
            "recent_intent": recent_intent,
            "unresolved": unresolved,
            "milestones": milestones[-self.milestone_limit:],
        }

    @staticmethod
    def _pending_memory(pending: dict[str, Any] | None) -> list[dict[str, str]]:
        if not pending:
            return []
        name = str(pending.get("name", ""))
        labels = {
            "provide_customer": "等待用户提供客户",
            "review_sku": "等待人工核对未匹配商品",
            "confirm_order": "等待人工批准或拒绝建单",
        }
        return [{"name": name, "summary": labels.get(name, "存在待处理动作")}]

    def _compatibility_summary(self, memory: dict[str, Any]) -> str:
        parts: list[str] = []
        if memory.get("objective"):
            parts.append(f"目标：{memory['objective']}")
        if memory.get("recent_intent"):
            parts.append(f"最近意图：{memory['recent_intent']}")
        unresolved = memory.get("unresolved") or []
        if unresolved:
            parts.append("待处理：" + "、".join(
                str(item.get("summary", "")) for item in unresolved
            ))
        milestones = memory.get("milestones") or []
        if milestones:
            parts.append("里程碑：" + "；".join(
                str(item.get("summary", "")) for item in milestones
            ))
        if not parts and int(memory.get("through_sequence", 0)) > 0:
            parts.append(f"已归纳至事件 {memory['through_sequence']}")
        return "；".join(parts)[-self.summary_limit:]
