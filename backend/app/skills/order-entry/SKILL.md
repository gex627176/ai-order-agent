---
name: order-entry
description: 将自然语言订单变成待人工审核草稿，并在明确批准后确认建单。
---

# Order Entry

1. 先确认 `site_id` 和客户；客户不明确时只追问，不猜测。
2. 使用 `draft_recognize` 调用既有确定性录单工作流。
3. 展示草稿、警告与本地目录事实；识别阶段不得创建正式订单。
4. `draft_confirm` 不向模型开放；只有 Harness 收到 `/resume` 的显式人工批准后才可调用。
5. 未匹配商品必须转交 `sku-resolution`，人工通过旧草稿入口修正后，使用 `draft_get` 重新检查。

任何工具失败都应保留已持久化的会话事件，并向调用方返回可解释错误摘要。
