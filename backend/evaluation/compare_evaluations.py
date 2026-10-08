from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


HIGHER_IS_BETTER = {
    "product_recognition_accuracy",
    "quantity_unit_accuracy",
    "catalog_match_success_rate",
    "retrieval_recall_at_3",
    "retrieval_mrr",
    "warning_count_accuracy",
    "confirmable_accuracy",
    "exact_case_accuracy",
}
LOWER_IS_BETTER = {
    "model_fallback_rate",
    "manual_resolution_rate",
    "average_response_ms",
    "p95_response_ms",
    "average_parse_ms",
    "p95_parse_ms",
    "average_tokens_per_case",
    "estimated_cost_per_case",
}


def compare_reports(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    max_accuracy_drop: float = 0.01,
    max_manual_resolution_increase: float = 0.02,
    max_token_increase_rate: float = 0.20,
) -> dict[str, Any]:
    baseline_hash = baseline.get("dataset", {}).get("sha256")
    candidate_hash = candidate.get("dataset", {}).get("sha256")
    compatible = bool(baseline_hash and baseline_hash == candidate_hash)
    shared_metrics = sorted(
        set(baseline["metrics"]) & set(candidate["metrics"])
    )
    metrics = {}
    for name in shared_metrics:
        before = baseline["metrics"][name]
        after = candidate["metrics"][name]
        metrics[name] = {
            "baseline": before,
            "candidate": after,
            "delta": round(after - before, 8),
            "direction": "higher" if name in HIGHER_IS_BETTER
            else "lower" if name in LOWER_IS_BETTER else "informational",
        }

    baseline_tokens = baseline["metrics"].get("average_tokens_per_case")
    candidate_tokens = candidate["metrics"].get("average_tokens_per_case")
    token_gate = True
    if baseline_tokens is not None and candidate_tokens is not None:
        token_gate = candidate_tokens <= baseline_tokens * (
            1 + max_token_increase_rate
        )
    gates = {
        "dataset_compatible": compatible,
        "exact_case_accuracy": metrics.get(
            "exact_case_accuracy", {"delta": -1.0}
        )["delta"] >= -max_accuracy_drop,
        "product_recognition_accuracy": metrics.get(
            "product_recognition_accuracy", {"delta": -1.0}
        )["delta"] >= -max_accuracy_drop,
        "quantity_unit_accuracy": metrics.get(
            "quantity_unit_accuracy", {"delta": -1.0}
        )["delta"] >= -max_accuracy_drop,
        "manual_resolution_rate": metrics.get(
            "manual_resolution_rate", {"delta": 1.0}
        )["delta"] <= max_manual_resolution_increase,
        "average_tokens_per_case": token_gate,
    }
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "baseline": baseline.get("run", {}),
        "candidate": candidate.get("run", {}),
        "dataset_sha256": candidate_hash if compatible else None,
        "compatible": compatible,
        "gates": gates,
        "passed": all(gates.values()),
        "metrics": metrics,
    }


def render_markdown(comparison: dict[str, Any], label: str) -> str:
    lines = [
        f"# Prompt 评测对比：{label}",
        "",
        f"- 生成时间：{comparison['generated_at']}",
        f"- 数据集一致：{'是' if comparison['compatible'] else '否'}",
        f"- 回归门禁：{'通过' if comparison['passed'] else '未通过'}",
        "",
        "## 指标差异",
        "",
        "| 指标 | 基线 | 候选 | 差异 | 方向 |",
        "|---|---:|---:|---:|---|",
    ]
    for name, metric in comparison["metrics"].items():
        lines.append(
            f"| {name} | {metric['baseline']:.6f} | "
            f"{metric['candidate']:.6f} | {metric['delta']:+.6f} | "
            f"{metric['direction']} |"
        )
    lines.extend(["", "## 回归门禁", "", "| 门禁 | 结果 |", "|---|---|"])
    for name, passed in comparison["gates"].items():
        lines.append(f"| {name} | {'通过' if passed else '失败'} |")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare two evaluation reports")
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--label", default="prompt-comparison")
    parser.add_argument("--max-accuracy-drop", type=float, default=0.01)
    parser.add_argument(
        "--max-manual-resolution-increase", type=float, default=0.02
    )
    parser.add_argument("--max-token-increase-rate", type=float, default=0.20)
    args = parser.parse_args()

    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    candidate = json.loads(args.candidate.read_text(encoding="utf-8"))
    comparison = compare_reports(
        baseline,
        candidate,
        max_accuracy_drop=args.max_accuracy_drop,
        max_manual_resolution_increase=args.max_manual_resolution_increase,
        max_token_increase_rate=args.max_token_increase_rate,
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.output_dir / f"{args.label}.json"
    md_path = args.output_dir / f"{args.label}.md"
    json_path.write_text(
        json.dumps(comparison, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    md_path.write_text(
        render_markdown(comparison, args.label), encoding="utf-8"
    )
    print(json.dumps({
        "json": str(json_path),
        "markdown": str(md_path),
        "passed": comparison["passed"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
