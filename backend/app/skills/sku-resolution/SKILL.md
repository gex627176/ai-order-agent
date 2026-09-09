---
name: sku-resolution
description: 为未匹配订单行整理本地目录候选和检索证据，不自动改写草稿。
---

# SKU Resolution

1. 只处理当前草稿中 `matched=false` 的订单行。
2. 只使用 `catalog_search` 查询当前站点生效的正式目录快照，并结合草稿已有 retrieval evidence。
3. 输出候选、来源和需要人工核对的行号；不得创建商品、修改目录或替人选择。
4. 没有可靠候选时明确返回空候选，不得虚构 SKU、价格或单位。
5. 子 Agent 无权调用 `draft_confirm` 或任何写工具。
