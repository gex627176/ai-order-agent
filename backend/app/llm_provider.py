from __future__ import annotations

import json
import math
from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class PromptSpec:
    version: str
    system_prompt: str
    user_template: str


PROMPT_V1 = """你是订单文本抽取器。只提取用户明确写出的信息，不猜测商品、数量、单位或业务规则。
必须返回 JSON 对象，格式如下：
{"items":[{"product_name":"番茄","quantity":5,"unit":"斤","note":""}]}
items 必须是数组；quantity 必须是大于 0 的数字；unit 保留用户原意；note 只放加工要求等备注。
如果无法识别，返回 {"items":[]}。不要输出 Markdown 或解释。"""

PROMPT_V2 = """你是严格的中文订单字段抽取器，只转换原文，不补全、不纠错、不查询商品或价格。
输出必须是一个 JSON 对象，且只能包含 items 数组：
{"items":[{"product_name":"番茄","quantity":5,"unit":"斤","note":""}]}

规则：
1. 按原文顺序逐项输出；逗号、顿号、分号、换行或连续书写都可能分隔商品。
2. product_name 只保留商品称呼，不包含数量、单位和加工备注。
3. quantity 必须来自原文且大于 0；不得把包装规格、日期或序号当成数量。
4. unit 保留原意；kg 或千克统一写成公斤，其他单位不要推断或换算。
5. note 只保留切丁、切丝、去皮、不要辣等明确要求；没有则为空字符串。
6. 缺少商品、数量或单位的片段不输出；不确定时宁可遗漏，不得猜测。
7. 无法识别任何完整商品时返回 {"items":[]}。

不要输出 Markdown、解释、候选商品、价格或额外字段。"""

PROMPTS: dict[str, PromptSpec] = {
    "order-extraction-v1": PromptSpec(
        version="order-extraction-v1",
        system_prompt=PROMPT_V1,
        user_template="请把下面订单转换成 JSON：\n{text}",
    ),
    "order-extraction-v2": PromptSpec(
        version="order-extraction-v2",
        system_prompt=PROMPT_V2,
        user_template="订单原文：\n{text}",
    ),
}
PROMPT_VERSION = "order-extraction-v1"


def get_prompt_spec(version: str) -> PromptSpec:
    try:
        return PROMPTS[version]
    except KeyError as exc:
        supported = ", ".join(sorted(PROMPTS))
        raise ValueError(
            f"不支持的 Prompt 版本 {version!r}，可选值：{supported}"
        ) from exc


class DeepSeekExtractor:
    def __init__(
        self, api_key: str, base_url: str, model: str,
        prompt_version: str = PROMPT_VERSION,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.prompt = get_prompt_spec(prompt_version)
        self.prompt_version = self.prompt.version
        self.last_usage = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }

    def extract(self, text: str) -> list[dict]:
        if not self.api_key:
            raise RuntimeError("未配置 DEEPSEEK_API_KEY")
        with httpx.Client(timeout=30) as client:
            response = client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": self.prompt.system_prompt},
                        {
                            "role": "user",
                            "content": self.prompt.user_template.format(text=text),
                        },
                    ],
                    "response_format": {"type": "json_object"},
                    "temperature": 0,
                    "max_tokens": 1200,
                },
            )
            response.raise_for_status()
        response_payload = response.json()
        usage = response_payload.get("usage") or {}
        self.last_usage = {
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "total_tokens": int(usage.get("total_tokens") or 0),
        }
        content = response_payload["choices"][0]["message"]["content"]
        if not content or not content.strip():
            raise ValueError("模型返回空内容")
        payload = json.loads(content)
        raw_items = payload.get("items")
        if not isinstance(raw_items, list):
            raise ValueError("模型 JSON 缺少 items 数组")

        if len(raw_items) > 200:
            raise ValueError("模型返回的商品行数超过 200 条")

        def normalize_item(entry: tuple[int, dict]) -> dict:
            line_no, raw = entry
            if not isinstance(raw, dict):
                raise ValueError(f"模型返回的第 {line_no} 行不是对象")
            name = str(raw.get("product_name", "")).strip()
            unit = str(raw.get("unit", "")).strip()
            quantity = float(raw.get("quantity", 0))
            if (
                not name
                or not unit
                or not math.isfinite(quantity)
                or quantity <= 0
                or quantity > 1_000_000
            ):
                raise ValueError(f"模型返回的第 {line_no} 行不完整")
            if unit.lower() in {"kg", "千克"}:
                unit = "公斤"
            return {
                "line_no": line_no,
                "raw_product_name": name,
                "product_id": None,
                "product_name": name,
                "quantity": quantity,
                "unit": unit,
                "unit_price": 0.0,
                "note": str(raw.get("note", "")).strip(),
                "matched": False,
            }

        return list(map(normalize_item, enumerate(raw_items, start=1)))
