# 订单识别离线评测：rules-baseline-115

- 样本数：115
- 生成时间：2026-08-20T08:03:52+00:00

## 核心指标

| 指标 | 结果 |
|---|---:|
| product_recognition_accuracy | 92.48% |
| quantity_unit_accuracy | 99.25% |
| catalog_match_success_rate | 91.15% |
| warning_count_accuracy | 91.30% |
| confirmable_accuracy | 91.30% |
| exact_case_accuracy | 91.30% |
| model_fallback_rate | 0.00% |
| average_response_ms | 53.69 |
| p95_response_ms | 68.25 |
| average_parse_ms | 0.00 |
| p95_parse_ms | 0.00 |

## 分场景精确通过率

| 场景 | 数量 | 精确率 |
|---|---:|---:|
| alias | 15 | 100.00% |
| colloquial | 10 | 0.00% |
| confusing_unmatched | 10 | 100.00% |
| format_variation | 10 | 100.00% |
| missing_quantity_or_unit | 10 | 100.00% |
| multi_item | 15 | 100.00% |
| note | 10 | 100.00% |
| standard | 15 | 100.00% |
| unit_variant | 10 | 100.00% |
| unknown_product | 10 | 100.00% |
