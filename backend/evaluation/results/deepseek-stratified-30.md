# 订单识别离线评测：deepseek-stratified-30

- 样本数：30
- 生成时间：2026-08-20T08:08:21+00:00

## 核心指标

| 指标 | 结果 |
|---|---:|
| product_recognition_accuracy | 100.00% |
| quantity_unit_accuracy | 100.00% |
| catalog_match_success_rate | 100.00% |
| warning_count_accuracy | 100.00% |
| confirmable_accuracy | 100.00% |
| exact_case_accuracy | 100.00% |
| model_fallback_rate | 3.33% |
| average_response_ms | 6478.25 |
| p95_response_ms | 9331.29 |
| average_parse_ms | 6415.37 |
| p95_parse_ms | 9247.00 |

## 分场景精确通过率

| 场景 | 数量 | 精确率 |
|---|---:|---:|
| alias | 3 | 100.00% |
| colloquial | 3 | 100.00% |
| confusing_unmatched | 3 | 100.00% |
| format_variation | 3 | 100.00% |
| missing_quantity_or_unit | 3 | 100.00% |
| multi_item | 3 | 100.00% |
| note | 3 | 100.00% |
| standard | 3 | 100.00% |
| unit_variant | 3 | 100.00% |
| unknown_product | 3 | 100.00% |
