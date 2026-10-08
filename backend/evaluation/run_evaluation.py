from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import tempfile
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.database import Database
from app.llm_provider import PROMPTS, PROMPT_VERSION
from app.settings import Settings
from app.workflow import OrderWorkflow


DEFAULT_FIXTURE = Path(__file__).parents[1] / "tests" / "fixtures" / "order_recognition_cases.jsonl"
DEFAULT_RESULTS = Path(__file__).parent / "results"


def load_cases(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def score_case(expected: dict[str, Any], actual: dict[str, Any]) -> dict[str, Any]:
    expected_items = expected["expected_items"]
    actual_items = actual["items"]
    aligned_count = max(len(expected_items), len(actual_items))
    product_correct = 0
    quantity_unit_correct = 0
    known_expected = 0
    catalog_matched = 0
    retrieved_top3 = 0
    reciprocal_rank_sum = 0.0
    exact_items = len(expected_items) == len(actual_items)

    for index in range(aligned_count):
        wanted = expected_items[index] if index < len(expected_items) else None
        got = actual_items[index] if index < len(actual_items) else None
        if wanted and wanted["product_id"] is not None:
            known_expected += 1
        if not wanted or not got:
            exact_items = False
            continue
        product_ok = wanted["product_id"] == got.get("product_id")
        quantity_unit_ok = (
            wanted["quantity"] == got.get("quantity")
            and wanted["unit"] == got.get("unit")
        )
        if product_ok:
            product_correct += 1
        if quantity_unit_ok:
            quantity_unit_correct += 1
        if wanted["product_id"] is not None and product_ok:
            catalog_matched += 1
        if wanted["product_id"] is not None:
            evidence = next(
                (
                    row for row in actual.get("retrieval", [])
                    if row.get("line_no") == index + 1
                ),
                {},
            )
            candidate_ids = [
                candidate.get("product_id")
                for candidate in evidence.get("candidates", [])[:3]
            ]
            if wanted["product_id"] in candidate_ids:
                rank = candidate_ids.index(wanted["product_id"]) + 1
                retrieved_top3 += 1
                reciprocal_rank_sum += 1 / rank
        exact_items = exact_items and all((
            product_ok,
            quantity_unit_ok,
            wanted["note"] == got.get("note"),
            wanted["matched"] == got.get("matched"),
        ))

    warning_ok = len(actual["warnings"]) == expected["expected_warning_count"]
    actual_confirmable = bool(actual_items) and not actual["warnings"]
    confirmable_ok = actual_confirmable == expected["should_be_confirmable"]
    return {
        "id": expected["id"],
        "case_type": expected["case_type"],
        "aligned_count": aligned_count,
        "product_correct": product_correct,
        "quantity_unit_correct": quantity_unit_correct,
        "known_expected": known_expected,
        "catalog_matched": catalog_matched,
        "retrieved_top3": retrieved_top3,
        "reciprocal_rank_sum": reciprocal_rank_sum,
        "warning_ok": warning_ok,
        "confirmable_ok": confirmable_ok,
        "manual_resolution_required": bool(actual["warnings"]),
        "exact_case": exact_items and warning_ok and confirmable_ok,
    }


def percentile95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)]


def evaluate(cases: list[dict[str, Any]], workflow: OrderWorkflow) -> dict[str, Any]:
    scored = []
    total_durations = []
    parse_durations = []
    fallback_count = 0
    by_type: dict[str, list[bool]] = defaultdict(list)

    for case in cases:
        started = time.perf_counter()
        draft = workflow.recognize(
            case["input_text"], case["customer_id"], site_id=1
        )
        total_ms = (time.perf_counter() - started) * 1000
        parse_ms = next(
            (step["duration_ms"] for step in draft["trace"] if step["step"] == 1),
            0,
        )
        result = score_case(case, draft)
        result.update({
            "provider": draft["provider"],
            "total_duration_ms": round(total_ms, 2),
            "parse_duration_ms": parse_ms,
        })
        scored.append(result)
        total_durations.append(total_ms)
        parse_durations.append(float(parse_ms))
        fallback_count += int("fallback" in draft["provider"])
        by_type[case["case_type"]].append(result["exact_case"])

    aligned = sum(row["aligned_count"] for row in scored)
    known = sum(row["known_expected"] for row in scored)
    count = len(scored)
    model_metrics = workflow.database.get_metrics_overview()
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "case_count": count,
        "run": {
            "provider": workflow.settings.ai_provider,
            "model": workflow.settings.deepseek_model
            if workflow.settings.ai_provider == "deepseek" else "rules",
            "prompt_version": workflow.settings.deepseek_prompt_version,
        },
        "metrics": {
            "product_recognition_accuracy": _ratio(sum(row["product_correct"] for row in scored), aligned),
            "quantity_unit_accuracy": _ratio(sum(row["quantity_unit_correct"] for row in scored), aligned),
            "catalog_match_success_rate": _ratio(sum(row["catalog_matched"] for row in scored), known),
            "retrieval_recall_at_3": _ratio(
                sum(row["retrieved_top3"] for row in scored), known
            ),
            "retrieval_mrr": round(
                sum(row["reciprocal_rank_sum"] for row in scored) / known, 4
            ) if known else 1.0,
            "warning_count_accuracy": _ratio(sum(row["warning_ok"] for row in scored), count),
            "confirmable_accuracy": _ratio(sum(row["confirmable_ok"] for row in scored), count),
            "exact_case_accuracy": _ratio(sum(row["exact_case"] for row in scored), count),
            "model_fallback_rate": _ratio(fallback_count, count),
            "human_review_required_rate": 1.0 if count else 0.0,
            "manual_resolution_rate": _ratio(
                sum(row["manual_resolution_required"] for row in scored), count
            ),
            "average_response_ms": round(sum(total_durations) / count, 2) if count else 0.0,
            "p95_response_ms": round(percentile95(total_durations), 2),
            "average_parse_ms": round(sum(parse_durations) / count, 2) if count else 0.0,
            "p95_parse_ms": round(percentile95(parse_durations), 2),
            "total_tokens": model_metrics["total_tokens"],
            "average_tokens_per_case": round(
                model_metrics["total_tokens"] / count, 2
            ) if count else 0.0,
            "estimated_cost_total": model_metrics["estimated_cost"],
            "estimated_cost_per_case": round(
                model_metrics["estimated_cost"] / count, 8
            ) if count else 0.0,
        },
        "by_case_type": {
            case_type: {
                "count": len(values),
                "exact_case_accuracy": _ratio(sum(values), len(values)),
            }
            for case_type, values in sorted(by_type.items())
        },
        "cases": scored,
    }


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 1.0


def render_markdown(report: dict[str, Any], label: str) -> str:
    metrics = report["metrics"]
    lines = [
        f"# 订单识别离线评测：{label}",
        "",
        f"- 样本数：{report['case_count']}",
        f"- 生成时间：{report['generated_at']}",
        f"- Provider：{report['run']['provider']}",
        f"- 模型：{report['run']['model']}",
        f"- Prompt 版本：{report['run']['prompt_version']}",
        "",
        "## 核心指标",
        "",
        "| 指标 | 结果 |",
        "|---|---:|",
    ]
    for name, value in metrics.items():
        display = _format_metric(name, value)
        lines.append(f"| {name} | {display} |")
    lines.extend(["", "## 分场景精确通过率", "", "| 场景 | 数量 | 精确率 |", "|---|---:|---:|"])
    for case_type, values in report["by_case_type"].items():
        lines.append(
            f"| {case_type} | {values['count']} | {values['exact_case_accuracy'] * 100:.2f}% |"
        )
    lines.append("")
    return "\n".join(lines)


def _format_metric(name: str, value: float | int) -> str:
    if any(token in name for token in ("accuracy", "rate", "recall", "mrr")):
        return f"{value * 100:.2f}%"
    if "cost" in name:
        return f"{value:.8f}"
    if "tokens" in name:
        return f"{value:,.2f}" if isinstance(value, float) else f"{value:,}"
    return f"{value:.2f}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run local order-recognition evaluation")
    parser.add_argument("--provider", choices=("rules", "deepseek"), default="rules")
    parser.add_argument(
        "--prompt-version",
        choices=tuple(sorted(PROMPTS)),
        default=os.getenv("DEEPSEEK_PROMPT_VERSION", PROMPT_VERSION),
    )
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_RESULTS)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument(
        "--per-type", type=int, default=0,
        help="Take the first N cases from every case_type for a stratified sample",
    )
    parser.add_argument("--label", default="")
    args = parser.parse_args()

    cases = load_cases(args.fixture)
    if args.per_type:
        selected = []
        counts: dict[str, int] = defaultdict(int)
        for case in cases:
            if counts[case["case_type"]] < args.per_type:
                selected.append(case)
                counts[case["case_type"]] += 1
        cases = selected
    if args.limit:
        cases = cases[:args.limit]
    os.environ["AI_PROVIDER"] = args.provider
    os.environ["DEEPSEEK_PROMPT_VERSION"] = args.prompt_version
    os.environ["ELASTICSEARCH_URL"] = ""
    logging.getLogger("agent.workflow").setLevel(logging.WARNING)

    with tempfile.TemporaryDirectory(prefix="ai-order-eval-") as temp_dir:
        database_path = str(Path(temp_dir) / "evaluation.db")
        settings = Settings.from_env(database_path)
        database = Database(database_path)
        database.initialize()
        workflow = OrderWorkflow(database, settings)
        try:
            report = evaluate(cases, workflow)
        finally:
            workflow.close()

    report["dataset"] = {
        "fixture": str(args.fixture),
        "sha256": hashlib.sha256(
            json.dumps(
                cases, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest(),
    }
    label = args.label or (
        f"{args.provider}-{args.prompt_version}-{len(cases)}"
        if args.provider == "deepseek" else f"{args.provider}-{len(cases)}"
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.output_dir / f"{label}.json"
    md_path = args.output_dir / f"{label}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(report, label), encoding="utf-8")
    print(json.dumps({"json": str(json_path), "markdown": str(md_path), **report["metrics"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
