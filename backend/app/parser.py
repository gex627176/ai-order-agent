from __future__ import annotations

import re


SEGMENT_PATTERN = re.compile(
    r"^(?P<name>[\u4e00-\u9fffA-Za-z]+?)\s*"
    r"(?P<quantity>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>公斤|千克|kg|KG|斤|箱|克|个|袋)"
    r"(?P<note>.*)$"
)


def parse_order_text(text: str) -> list[dict]:
    segments = [part.strip() for part in re.split(r"[,，;；\n]+", text) if part.strip()]

    def parse_segment(entry: tuple[int, str]) -> dict | None:
        line_no, segment = entry
        match = SEGMENT_PATTERN.match(segment)
        if not match:
            return None
        unit = match.group("unit").lower()
        if unit in {"kg", "千克"}:
            unit = "公斤"
        return {
            "line_no": line_no,
            "raw_product_name": match.group("name"),
            "product_name": match.group("name"),
            "quantity": float(match.group("quantity")),
            "unit": unit,
            "unit_price": 0.0,
            "note": match.group("note").strip(" ，,"),
            "matched": False,
            "product_id": None,
        }

    return list(filter(None, map(parse_segment, enumerate(segments, start=1))))
