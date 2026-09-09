# 订单识别离线评测：deepseek-v1-stratified-34

- 样本数：34
- 生成时间：2026-08-26T13:53:32+00:00
- Provider：deepseek
- 模型：deepseek-v4-pro
- Prompt 版本：order-extraction-v1

## 核心指标

| 指标 | 结果 |
|---|---:|
| product_recognition_accuracy | 100.00% |
| quantity_unit_accuracy | 100.00% |
| catalog_match_success_rate | 100.00% |
| retrieval_recall_at_3 | 100.00% |
| retrieval_mrr | 100.00% |
| warning_count_accuracy | 100.00% |
| confirmable_accuracy | 100.00% |
| exact_case_accuracy | 100.00% |
| model_fallback_rate | 0.00% |
| human_review_required_rate | 100.00% |
| manual_resolution_rate | 29.41% |
| average_response_ms | 4018.67 |
| p95_response_ms | 10249.64 |
| average_parse_ms | 3991.59 |
| p95_parse_ms | 10230.00 |
| total_tokens | 12,898 |
| average_tokens_per_case | 379.35 |
| estimated_cost_total | 0.00000000 |
| estimated_cost_per_case | 0.00000000 |

## 分场景精确通过率

| 场景 | 数量 | 精确率 |
|---|---:|---:|
| alias | 2 | 100.00% |
| alias_extended | 2 | 100.00% |
| colloquial | 2 | 100.00% |
| confusing_unmatched | 2 | 100.00% |
| format_extended | 2 | 100.00% |
| format_variation | 2 | 100.00% |
| missing_quantity_or_unit | 2 | 100.00% |
| multi_item | 2 | 100.00% |
| multi_item_extended | 2 | 100.00% |
| note | 2 | 100.00% |
| note_extended | 2 | 100.00% |
| standard | 2 | 100.00% |
| standard_extended | 2 | 100.00% |
| typo_unmatched | 2 | 100.00% |
| unit_variant | 2 | 100.00% |
| unknown_extended | 2 | 100.00% |
| unknown_product | 2 | 100.00% |
