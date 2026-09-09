from __future__ import annotations

from typing import Any

from .session import AgentSessionStore


class ContextAssembler:
    def __init__(self, recent_event_limit: int = 16, summary_limit: int = 2000):
        self.recent_event_limit = recent_event_limit
        self.summary_limit = summary_limit

    def compact(
        self, store: AgentSessionStore, session: dict[str, Any]
    ) -> dict[str, Any]:
        cutoff = session["last_event_sequence"] - self.recent_event_limit
        cursor = session["summary_through_sequence"]
        if cutoff <= cursor:
            return session
        events = store.events_between(session["id"], cursor + 1, cutoff)
        fragments = [event["summary"] for event in events if event["summary"]]
        addition = "；".join(fragments)
        summary = "；".join(
            part for part in (session["context_summary"], addition) if part
        )[-self.summary_limit:]
        return store.update(
            session["id"],
            expected_version=session["version"],
            context_summary=summary,
            summary_through_sequence=cutoff,
        )

    def assemble(self, session: dict[str, Any]) -> dict[str, Any]:
        messages: list[dict[str, str]] = []
        for event in session.get("events", [])[-self.recent_event_limit:]:
            if event["type"] not in {"user", "assistant"}:
                continue
            content = str(event.get("payload", {}).get("content") or event["summary"])
            messages.append({
                "role": "user" if event["type"] == "user" else "assistant",
                "content": content[:5000],
            })
        return {
            "session_id": session["id"],
            "site_id": session["site_id"],
            "customer_id": session.get("customer_id"),
            "current_draft_id": session.get("current_draft_id"),
            "pending_action": session.get("pending_action"),
            "summary": session.get("context_summary", ""),
            "messages": messages,
        }
