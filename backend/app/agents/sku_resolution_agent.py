from __future__ import annotations

from typing import Any, Callable

from ..agent_runtime.errors import PlannerError
from ..agent_runtime.planner import DeepSeekToolPlanner
from ..agent_runtime.skills import LoadedSkill


class SkuResolutionAgent:
    """Bounded read-only specialist; it never edits a draft or confirms an order."""

    def __init__(self, skill: LoadedSkill):
        self.skill = skill

    def resolve(
        self,
        draft: dict[str, Any],
        call_catalog: Callable[[dict[str, Any]], list[dict[str, Any]]],
        planner: DeepSeekToolPlanner,
        catalog_tool_schema: list[dict[str, Any]],
    ) -> dict[str, Any]:
        unresolved_items = [
            item for item in draft.get("items", []) if not item.get("matched")
        ]
        requested_arguments: list[dict[str, Any]] = []
        planner_fallback = False
        planner_metrics: dict[str, Any] = {}
        if planner.enabled and unresolved_items:
            try:
                turn = planner.complete(
                    [
                        {"role": "system", "content": self.skill.instructions},
                        {
                            "role": "user",
                            "content": (
                                "只为这些未匹配名称调用 catalog_search 查找候选，"
                                f"最多三次：{[item.get('raw_product_name', '') for item in unresolved_items]}"
                            ),
                        },
                    ],
                    catalog_tool_schema,
                )
                requested_arguments = [
                    call.arguments
                    for call in turn.tool_calls[:3]
                    if call.name == "catalog_search"
                ]
                planner_metrics = {
                    "duration_ms": turn.duration_ms,
                    "prompt_tokens": turn.prompt_tokens,
                    "completion_tokens": turn.completion_tokens,
                    "total_tokens": turn.total_tokens,
                    "tool_names": [call.name for call in turn.tool_calls],
                }
            except PlannerError:
                planner_fallback = True
        if not requested_arguments:
            requested_arguments = [
                {
                    "query": str(item.get("raw_product_name", "")).strip(),
                    "active_only": True,
                }
                for item in unresolved_items[:3]
            ]
        candidate_map: dict[str, list[dict[str, Any]]] = {}
        attempted_queries: set[str] = set()
        search_queue = [*requested_arguments]
        search_queue.extend(
            {
                "query": str(item.get("raw_product_name", "")).strip(),
                "active_only": True,
            }
            for item in unresolved_items
        )
        for arguments in search_queue:
            if len(attempted_queries) >= 3:
                break
            query = str(arguments.get("query", "")).strip()
            if not query or query in attempted_queries:
                continue
            attempted_queries.add(query)
            try:
                candidate_map[query] = call_catalog(arguments)
            except Exception:
                planner_fallback = True
                candidate_map.setdefault(query, [])
        unresolved: list[dict[str, Any]] = []
        for item in unresolved_items:
            query = str(item.get("raw_product_name", "")).strip()
            retrieval_candidates = [
                evidence for evidence in draft.get("retrieval", [])
                if evidence.get("line_no") == item.get("line_no")
            ]
            unresolved.append({
                "line_no": item.get("line_no"),
                "raw_product_name": query,
                "catalog_candidates": candidate_map.get(query, []),
                "retrieval_evidence": retrieval_candidates,
            })
        return {
            "draft_id": draft["id"],
            "unresolved": unresolved,
            "requires_manual_edit": True,
            "planner_fallback": planner_fallback,
            "planner_metrics": planner_metrics,
        }
