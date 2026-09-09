from __future__ import annotations

import re
from typing import Any

from ..agent_runtime.models import PlannerToolCall, PlannerTurn
from ..agent_runtime.skills import LoadedSkill


class MainAgent:
    def __init__(self, skill: LoadedSkill):
        self.skill = skill

    def system_message(self, context: dict[str, Any]) -> str:
        return f"""你是本地录单系统的主 Agent。你可以动态选择提供的工具，但本地工具结果才是客户、商品、价格和草稿的唯一事实来源。
不得虚构 ID、SKU、价格或工具参数；不得声称订单已创建；正式建单只能由独立 resume API 完成人工批准。
当前会话范围：site_id={context['site_id']}，customer_id={context.get('customer_id')}，current_draft_id={context.get('current_draft_id')}。
待处理动作：{context.get('pending_action')}。
较早上下文摘要：{context.get('summary', '')}

技能说明：
{self.skill.instructions}"""

    @staticmethod
    def order_text(message: str) -> str:
        return _order_text(message.strip())

    def fallback(self, message: str, context: dict[str, Any]) -> PlannerTurn:
        normalized = message.strip()
        pending = context.get("pending_action") or {}
        pending_name = pending.get("name")
        if pending_name == "provide_customer":
            return _tool_turn("customer_search", {"query": _customer_query(normalized)})
        if pending_name == "review_sku":
            if any(word in normalized for word in ("重新检查", "检查草稿", "已经修改", "已修改", "改好了")):
                return _tool_turn("draft_get", {})
            return PlannerTurn(
                content="当前草稿仍需在原草稿审核入口人工修正商品，然后告诉我“重新检查”。",
                tool_calls=[],
            )
        if pending_name == "confirm_order":
            return PlannerTurn(
                content="草稿已准备好；请通过 resume 接口明确批准或拒绝建单。",
                tool_calls=[],
            )
        if context.get("current_draft_id") and any(
            word in normalized
            for word in ("重新检查", "检查草稿", "已经修改", "已修改", "改好了")
        ):
            return _tool_turn("draft_get", {})
        asks_list = any(word in normalized for word in ("哪些", "有哪些", "列出", "查看", "查询"))
        calls: list[PlannerToolCall] = []
        if asks_list and "客户" in normalized:
            calls.append(_call("customer_list", {}))
        if asks_list and any(word in normalized.lower() for word in ("商品", "目录", "sku")):
            calls.append(_call("catalog_search", {"query": "", "active_only": True}))
        if "偏好" in normalized and context.get("customer_id") is not None:
            calls.append(_call("customer_preferences", {}))
        task_match = re.search(r"(?:任务|task)[：:\s]*([\w-]{8,100})", normalized, re.I)
        if task_match:
            calls.append(_call("recognition_task_get", {"task_id": task_match.group(1)}))
        if calls:
            return PlannerTurn(content="", tool_calls=calls)
        if context.get("customer_id") is None:
            for prefix in ("客户是", "客户：", "客户:"):
                if normalized.startswith(prefix):
                    return _tool_turn(
                        "customer_search", {"query": normalized[len(prefix):].strip()}
                    )
        return _tool_turn("draft_recognize", {"text": _order_text(normalized)})


def _call(name: str, arguments: dict[str, Any]) -> PlannerToolCall:
    return PlannerToolCall(id=f"fallback-{name}", name=name, arguments=arguments)


def _tool_turn(name: str, arguments: dict[str, Any]) -> PlannerTurn:
    return PlannerTurn(content="", tool_calls=[_call(name, arguments)])


def _customer_query(content: str) -> str:
    query = content.strip()
    for prefix in ("客户是", "客户：", "客户:", "给", "为"):
        if query.startswith(prefix):
            return query[len(prefix):].strip() or query
    return query


def _order_text(content: str) -> str:
    for prefix in (
        "请帮我录入", "帮我录入", "请帮我录单", "帮我录单",
        "请帮我录", "帮我录", "录入", "录一下", "请下单", "下单",
    ):
        if content.startswith(prefix):
            candidate = content[len(prefix):].lstrip("：:，, ")
            return candidate or content
    return content
