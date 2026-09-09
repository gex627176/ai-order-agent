# 订单识别离线评测：rules-extended-315

- 样本数：315
- 生成时间：2026-08-26T13:47:57+00:00
- Provider：rules
- 模型：rules
- Prompt 版本：order-extraction-v1

## 核心指标

| 指标 | 结果 |
|---|---:|
| product_recognition_accuracy | 97.58% |
| quantity_unit_accuracy | 99.76% |
| catalog_match_success_rate | 97.17% |
| retrieval_recall_at_3 | 97.17% |
| retrieval_mrr | 97.17% |
| warning_count_accuracy | 96.83% |
| confirmable_accuracy | 96.83% |
| exact_case_accuracy | 96.83% |
| model_fallback_rate | 0.00% |
| human_review_required_rate | 100.00% |
| manual_resolution_rate | 25.40% |
| average_response_ms | 20.96 |
| p95_response_ms | 24.44 |
| average_parse_ms | 0.03 |
| p95_parse_ms | 0.00 |
| total_tokens | 0 |
| average_tokens_per_case | 0.00 |
| estimated_cost_total | 0.00000000 |
| estimated_cost_per_case | 0.00000000 |

## 分场景精确通过率

| 场景 | 数量 | 精确率 |
|---|---:|---:|
| alias | 15 | 100.00% |
| alias_extended | 30 | 100.00% |
| colloquial | 10 | 0.00% |
| confusing_unmatched | 10 | 100.00% |
| format_extended | 20 | 100.00% |
| format_variation | 10 | 100.00% |
| missing_quantity_or_unit | 10 | 100.00% |
| multi_item | 15 | 100.00% |
| multi_item_extended | 40 | 100.00% |
| note | 10 | 100.00% |
| note_extended | 30 | 100.00% |
| standard | 15 | 100.00% |
| standard_extended | 40 | 100.00% |
| typo_unmatched | 20 | 100.00% |
| unit_variant | 10 | 100.00% |
| unknown_extended | 20 | 100.00% |
| unknown_product | 10 | 100.00% |
