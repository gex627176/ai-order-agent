from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from app.database import CUSTOMERS, PRODUCTS
from app.schemas import DraftItem


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "order_recognition_cases.jsonl"
EXTENDED_FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "order_recognition_extended_315.jsonl"
)
REQUIRED_TOP_LEVEL_FIELDS = {
    "id",
    "case_type",
    "input_text",
    "customer_id",
    "expected_items",
    "expected_warning_count",
    "should_be_confirmable",
}
REQUIRED_CASE_TYPES = {
    "standard",
    "multi_item",
    "alias",
    "unit_variant",
    "note",
    "colloquial",
    "format_variation",
    "unknown_product",
    "missing_quantity_or_unit",
    "confusing_unmatched",
}


def load_cases() -> list[dict]:
    lines = FIXTURE_PATH.read_text(encoding="utf-8").splitlines()
    assert lines, "评测数据文件不能为空"
    return [json.loads(line) for line in lines if line.strip()]


def test_order_recognition_fixture_is_valid_jsonl_and_complete():
    cases = load_cases()

    assert len(cases) >= 100
    assert len({case["id"] for case in cases}) == len(cases)
    assert {case["case_type"] for case in cases} == REQUIRED_CASE_TYPES

    customer_ids = {row[0] for row in CUSTOMERS}
    item_fields = set(DraftItem.model_fields)
    for case in cases:
        assert set(case) == REQUIRED_TOP_LEVEL_FIELDS
        assert isinstance(case["id"], str) and case["id"]
        assert isinstance(case["input_text"], str) and case["input_text"]
        assert case["customer_id"] in customer_ids
        assert isinstance(case["expected_items"], list)
        assert (
            isinstance(case["expected_warning_count"], int)
            and not isinstance(case["expected_warning_count"], bool)
            and case["expected_warning_count"] >= 0
        )
        assert isinstance(case["should_be_confirmable"], bool)

        for item in case["expected_items"]:
            assert set(item) == item_fields
            try:
                DraftItem.model_validate(item)
            except ValidationError as exc:
                raise AssertionError(f"{case['id']} 的 expected_items 不符合 DraftItem") from exc


def test_fixture_uses_only_seed_catalog_facts_and_current_validation_rules():
    cases = load_cases()
    products_by_id = {
        row[0]: {
            "name": row[2],
            "aliases": row[3],
            "unit_price": row[5],
        }
        for row in PRODUCTS
    }
    normalized_units = {"公斤", "斤", "箱", "克", "个", "袋"}

    for case in cases:
        unmatched_count = 0
        for expected_line, item in enumerate(case["expected_items"], start=1):
            assert item["line_no"] == expected_line
            assert item["unit"] in normalized_units
            if item["matched"]:
                product = products_by_id[item["product_id"]]
                assert item["product_name"] == product["name"]
                assert item["raw_product_name"] in {
                    product["name"],
                    *product["aliases"],
                }
                assert item["unit_price"] == product["unit_price"]
            else:
                unmatched_count += 1
                assert item["product_id"] is None
                assert item["product_name"] == item["raw_product_name"]
                assert item["unit_price"] == 0.0

        expected_warnings = unmatched_count + (1 if not case["expected_items"] else 0)
        assert case["expected_warning_count"] == expected_warnings
        assert case["should_be_confirmable"] == (
            bool(case["expected_items"]) and expected_warnings == 0
        )


def test_fixture_contains_both_easy_and_intentionally_hard_cases():
    cases = load_cases()
    counts = {}
    for case in cases:
        counts[case["case_type"]] = counts.get(case["case_type"], 0) + 1

    assert all(counts[case_type] >= 10 for case_type in REQUIRED_CASE_TYPES)
    assert any(case["should_be_confirmable"] for case in cases)
    assert any(not case["should_be_confirmable"] for case in cases)
    assert any("\n" in case["input_text"] for case in cases)
    assert any(
        case["case_type"] == "colloquial"
        and case["should_be_confirmable"]
        for case in cases
    )


def test_extended_fixture_is_reproducible_and_contains_315_cases():
    from evaluation.generate_extended_fixture import render_fixture

    expected = render_fixture()
    assert EXTENDED_FIXTURE_PATH.read_text(encoding="utf-8") == expected
    cases = [json.loads(line) for line in expected.splitlines() if line.strip()]
    assert len(cases) == 315
    assert len({case["id"] for case in cases}) == 315
    assert sum(case["should_be_confirmable"] for case in cases) >= 200
    assert {"unknown_extended", "typo_unmatched"} <= {
        case["case_type"] for case in cases
    }
