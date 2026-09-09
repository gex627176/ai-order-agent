from app.llm_provider import PROMPTS, DeepSeekExtractor, get_prompt_spec
from evaluation.compare_evaluations import compare_reports


def report(prompt_version: str, dataset_hash: str, **metrics):
    return {
        "run": {"provider": "deepseek", "prompt_version": prompt_version},
        "dataset": {"sha256": dataset_hash},
        "metrics": metrics,
    }


def test_prompt_registry_is_explicit_and_rejects_unknown_version():
    assert set(PROMPTS) == {"order-extraction-v1", "order-extraction-v2"}
    extractor = DeepSeekExtractor(
        "test-key", "https://example.invalid", "test-model",
        "order-extraction-v2",
    )
    assert extractor.prompt_version == "order-extraction-v2"
    assert "不得把包装规格" in extractor.prompt.system_prompt

    try:
        get_prompt_spec("unregistered-prompt")
    except ValueError as exc:
        assert "不支持的 Prompt 版本" in str(exc)
    else:
        raise AssertionError("未知 Prompt 版本必须被拒绝")


def test_comparison_passes_compatible_non_regression():
    baseline = report(
        "order-extraction-v1", "same-data",
        exact_case_accuracy=0.90,
        product_recognition_accuracy=0.92,
        quantity_unit_accuracy=0.98,
        manual_resolution_rate=0.10,
        average_tokens_per_case=400.0,
    )
    candidate = report(
        "order-extraction-v2", "same-data",
        exact_case_accuracy=0.91,
        product_recognition_accuracy=0.93,
        quantity_unit_accuracy=0.98,
        manual_resolution_rate=0.11,
        average_tokens_per_case=450.0,
    )

    comparison = compare_reports(baseline, candidate)

    assert comparison["passed"] is True
    assert comparison["metrics"]["exact_case_accuracy"]["delta"] == 0.01


def test_comparison_rejects_dataset_mismatch_and_accuracy_regression():
    baseline = report(
        "order-extraction-v1", "data-a",
        exact_case_accuracy=0.90,
        product_recognition_accuracy=0.92,
        quantity_unit_accuracy=0.98,
        manual_resolution_rate=0.10,
    )
    candidate = report(
        "order-extraction-v2", "data-b",
        exact_case_accuracy=0.85,
        product_recognition_accuracy=0.88,
        quantity_unit_accuracy=0.94,
        manual_resolution_rate=0.15,
    )

    comparison = compare_reports(baseline, candidate)

    assert comparison["compatible"] is False
    assert comparison["passed"] is False
    assert comparison["gates"]["exact_case_accuracy"] is False


def test_comparison_rejects_excessive_token_growth():
    baseline = report(
        "order-extraction-v1", "same-data",
        exact_case_accuracy=1.0,
        product_recognition_accuracy=1.0,
        quantity_unit_accuracy=1.0,
        manual_resolution_rate=0.1,
        average_tokens_per_case=100.0,
    )
    candidate = report(
        "order-extraction-v2", "same-data",
        exact_case_accuracy=1.0,
        product_recognition_accuracy=1.0,
        quantity_unit_accuracy=1.0,
        manual_resolution_rate=0.1,
        average_tokens_per_case=130.0,
    )

    comparison = compare_reports(baseline, candidate)

    assert comparison["passed"] is False
    assert comparison["gates"]["average_tokens_per_case"] is False
