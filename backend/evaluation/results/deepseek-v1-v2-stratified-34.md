# Prompt 评测对比：deepseek-v1-v2-stratified-34

- 生成时间：2026-08-26T13:59:20+00:00
- 数据集一致：是
- 回归门禁：未通过

## 指标差异

| 指标 | 基线 | 候选 | 差异 | 方向 |
|---|---:|---:|---:|---|
| average_parse_ms | 3991.590000 | 3955.680000 | -35.910000 | lower |
| average_response_ms | 4018.670000 | 3978.540000 | -40.130000 | lower |
| average_tokens_per_case | 379.350000 | 513.530000 | +134.180000 | lower |
| catalog_match_success_rate | 1.000000 | 1.000000 | +0.000000 | higher |
| confirmable_accuracy | 1.000000 | 1.000000 | +0.000000 | higher |
| estimated_cost_per_case | 0.000000 | 0.000000 | +0.000000 | lower |
| estimated_cost_total | 0.000000 | 0.000000 | +0.000000 | informational |
| exact_case_accuracy | 1.000000 | 1.000000 | +0.000000 | higher |
| human_review_required_rate | 1.000000 | 1.000000 | +0.000000 | informational |
| manual_resolution_rate | 0.294100 | 0.294100 | +0.000000 | lower |
| model_fallback_rate | 0.000000 | 0.000000 | +0.000000 | lower |
| p95_parse_ms | 10230.000000 | 6694.000000 | -3536.000000 | lower |
| p95_response_ms | 10249.640000 | 6714.030000 | -3535.610000 | lower |
| product_recognition_accuracy | 1.000000 | 1.000000 | +0.000000 | higher |
| quantity_unit_accuracy | 1.000000 | 1.000000 | +0.000000 | higher |
| retrieval_mrr | 1.000000 | 1.000000 | +0.000000 | higher |
| retrieval_recall_at_3 | 1.000000 | 1.000000 | +0.000000 | higher |
| total_tokens | 12898.000000 | 17460.000000 | +4562.000000 | informational |
| warning_count_accuracy | 1.000000 | 1.000000 | +0.000000 | higher |

## 回归门禁

| 门禁 | 结果 |
|---|---|
| dataset_compatible | 通过 |
| exact_case_accuracy | 通过 |
| product_recognition_accuracy | 通过 |
| quantity_unit_accuracy | 通过 |
| manual_resolution_rate | 通过 |
| average_tokens_per_case | 失败 |
