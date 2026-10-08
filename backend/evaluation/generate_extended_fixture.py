from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from app.database import PRODUCTS


BASE_FIXTURE = (
    Path(__file__).parents[1] / "tests" / "fixtures" /
    "order_recognition_cases.jsonl"
)
DEFAULT_OUTPUT = (
    Path(__file__).parents[1] / "tests" / "fixtures" /
    "order_recognition_extended_315.jsonl"
)


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _product(index: int) -> dict[str, Any]:
    row = PRODUCTS[index % len(PRODUCTS)]
    return {
        "id": row[0], "name": row[2], "aliases": row[3],
        "unit": row[4], "unit_price": row[5],
    }


def _item(
    product: dict[str, Any] | None,
    raw_name: str,
    quantity: float,
    unit: str,
    note: str = "",
    line_no: int = 1,
) -> dict[str, Any]:
    return {
        "line_no": line_no,
        "raw_product_name": raw_name,
        "product_id": product["id"] if product else None,
        "product_name": product["name"] if product else raw_name,
        "quantity": quantity,
        "unit": "公斤" if unit.lower() in {"kg", "千克"} else unit,
        "unit_price": product["unit_price"] if product else 0.0,
        "note": note,
        "matched": product is not None,
    }


def _case(
    case_id: str,
    case_type: str,
    text: str,
    items: list[dict[str, Any]],
    customer_id: int,
) -> dict[str, Any]:
    warnings = sum(not item["matched"] for item in items) + (0 if items else 1)
    return {
        "id": case_id,
        "case_type": case_type,
        "input_text": text,
        "customer_id": customer_id,
        "expected_items": items,
        "expected_warning_count": warnings,
        "should_be_confirmable": bool(items) and warnings == 0,
    }


def generate_extended_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    quantities = [0.5, 1.5, 3, 4.5, 7, 12, 18, 25]
    for index in range(40):
        product = _product(index)
        quantity = quantities[index // len(PRODUCTS)]
        text = f"{product['name']}{quantity:g}{product['unit']}"
        cases.append(_case(
            f"standard_extended-{index + 1:03d}", "standard_extended", text,
            [_item(product, product["name"], quantity, product["unit"])],
            index % 3 + 1,
        ))

    for index in range(30):
        product = _product(index)
        raw_name = product["aliases"][index % len(product["aliases"])]
        quantity = index % 6 + 1
        text = f"{raw_name}{quantity}{product['unit']}"
        cases.append(_case(
            f"alias_extended-{index + 1:03d}", "alias_extended", text,
            [_item(product, raw_name, quantity, product["unit"])],
            index % 3 + 1,
        ))

    notes = ["切丁", "切丝", "去皮", "洗净", "不要辣", "分装"]
    for index in range(30):
        product = _product(index)
        note = notes[index // len(PRODUCTS)]
        quantity = index % 5 + 1
        text = f"{product['name']}{quantity}{product['unit']}{note}"
        cases.append(_case(
            f"note_extended-{index + 1:03d}", "note_extended", text,
            [_item(product, product["name"], quantity, product["unit"], note)],
            index % 3 + 1,
        ))

    delimiters = ["，", ",", "；", ";", "\n"]
    for index in range(40):
        item_count = 2 if index < 20 else 3
        segments = []
        expected = []
        for offset in range(item_count):
            product = _product(index + offset)
            quantity = (index + offset) % 5 + 1
            segments.append(f"{product['name']}{quantity}{product['unit']}")
            expected.append(_item(
                product, product["name"], quantity, product["unit"],
                line_no=offset + 1,
            ))
        text = delimiters[index % len(delimiters)].join(segments)
        cases.append(_case(
            f"multi_item_extended-{index + 1:03d}",
            "multi_item_extended", text, expected, index % 3 + 1,
        ))

    for index in range(20):
        first = _product(index)
        second = _product(index + 2)
        first_quantity = index % 4 + 1
        second_quantity = index % 3 + 1
        separator = " ； " if index % 2 == 0 else "\n"
        text = (
            f"  {first['name']}  {first_quantity} {first['unit']}"
            f"{separator}{second['name']} {second_quantity}{second['unit']}  "
        )
        cases.append(_case(
            f"format_extended-{index + 1:03d}", "format_extended", text,
            [
                _item(first, first["name"], first_quantity, first["unit"], line_no=1),
                _item(second, second["name"], second_quantity, second["unit"], line_no=2),
            ],
            index % 3 + 1,
        ))

    unknown_names = [
        "白菜", "青椒", "黄瓜", "茄子", "冬瓜", "南瓜", "生菜", "芹菜",
        "香菜", "豆角", "番茄酱", "苹果醋", "土豆粉", "洋葱圈", "胡萝卜汁",
        "红薯", "山药", "莲藕", "大蒜", "生姜",
    ]
    for index, name in enumerate(unknown_names):
        quantity = index % 5 + 1
        unit = ["斤", "公斤", "箱", "袋"][index % 4]
        text = f"{name}{quantity}{unit}"
        cases.append(_case(
            f"unknown_extended-{index + 1:03d}", "unknown_extended", text,
            [_item(None, name, quantity, unit)], index % 3 + 1,
        ))

    typo_names = [
        "西红市", "西红柿子", "蕃茄", "翻茄", "胡罗卜", "胡萝补", "红罗卜",
        "苹菓", "平果", "红富士果", "洋忽", "洋葱头", "园葱", "土豆子",
        "士豆", "马铃署", "番茄汁", "苹果干", "萝卜干", "土豆片",
    ]
    for index, name in enumerate(typo_names):
        quantity = index % 4 + 1
        unit = ["斤", "公斤", "箱", "袋"][index % 4]
        text = f"{name}{quantity}{unit}"
        cases.append(_case(
            f"typo_unmatched-{index + 1:03d}", "typo_unmatched", text,
            [_item(None, name, quantity, unit)], index % 3 + 1,
        ))

    if len(cases) != 200:
        raise AssertionError(f"扩展样本必须为 200 条，实际 {len(cases)} 条")
    return cases


def render_fixture() -> str:
    combined = [*_load_jsonl(BASE_FIXTURE), *generate_extended_cases()]
    if len({case["id"] for case in combined}) != len(combined):
        raise AssertionError("扩展评测样本 ID 重复")
    return "\n".join(
        json.dumps(case, ensure_ascii=False, separators=(",", ":"))
        for case in combined
    ) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate 315 synthetic eval cases")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    rendered = render_fixture()
    if args.check:
        if not args.output.exists() or args.output.read_text(encoding="utf-8") != rendered:
            raise SystemExit("扩展评测文件不是最新生成结果")
        print(f"ok: {args.output}")
        return
    args.output.write_text(rendered, encoding="utf-8")
    print(f"generated: {args.output} (315 cases)")


if __name__ == "__main__":
    main()
