from __future__ import annotations

from typing import Any


BLOCKED_ERROR_FRAGMENTS = (
    "http://",
    "https://",
    "sqlite",
    "codec",
    "traceback",
    "bearer",
    "api_key",
    "sk-",
)

FIELD_LABELS = {
    "site_id": "站点参数",
    "customer_id": "客户参数",
    "text": "订单文本",
    "items": "订单明细",
    "line_no": "明细行号",
    "quantity": "数量",
    "unit_price": "单价",
    "unit": "单位",
    "sku": "SKU",
    "name": "商品名称",
    "aliases": "商品别名",
    "decision": "审核决定",
    "file": "上传文件",
}

VALIDATION_REASONS = {
    "missing": "缺少必填值",
    "greater_than": "必须大于允许的最小值",
    "greater_than_equal": "不能小于允许的最小值",
    "less_than": "必须小于允许的最大值",
    "less_than_equal": "不能超过允许的最大值",
    "finite_number": "必须是有限数字",
    "string_too_short": "不能为空或长度不足",
    "string_too_long": "长度超过限制",
    "too_short": "内容数量不足",
    "too_long": "内容数量超过限制",
    "string_pattern_mismatch": "格式不符合要求",
    "int_parsing": "必须是整数",
    "float_parsing": "必须是数字",
    "bool_parsing": "必须是布尔值",
    "list_type": "必须是列表",
}


def public_error(error: object, fallback: str) -> str:
    message = str(error).strip()
    lowered = message.lower()
    has_chinese = any("\u4e00" <= character <= "\u9fff" for character in message)
    if (
        message
        and len(message) <= 200
        and has_chinese
        and not any(fragment in lowered for fragment in BLOCKED_ERROR_FRAGMENTS)
    ):
        return message
    return fallback


def validation_error_detail(errors: list[dict[str, Any]]) -> str:
    messages: list[str] = []
    for error in errors:
        location = [
            str(part)
            for part in error.get("loc", ())
            if part not in {"body", "query", "path", "header", "cookie"}
        ]
        field = next(
            (part for part in reversed(location) if part in FIELD_LABELS),
            "request",
        )
        label = FIELD_LABELS.get(field, "请求参数")
        error_type = str(error.get("type", ""))
        if error_type == "value_error" and field == "request":
            label = "订单明细"
            reason = "行号不能重复或内容不符合要求"
        else:
            reason = VALIDATION_REASONS.get(error_type, "格式或取值不符合要求")
        message = f"{label}：{reason}"
        if message not in messages:
            messages.append(message)
    return "；".join(messages) or "请求参数不完整或格式错误"
