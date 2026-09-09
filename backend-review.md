# Backend 生产级评审（第三轮，供修复 Agent 直接执行）

把本文完整交给修复 Agent。不要再口头转述。

- 这是第三轮执行清单。第二轮 P0 已落地；第二轮复审里的 **R2-3 已撤回，不要再按原文加 `BEGIN IMMEDIATE`**。
- 评审对象：`E:\agent_order_myself\backend\app\` 当前代码。
- **只修本文「必须修」。** 不要重做已关闭项，不要顺手重构，不要动 R2-7 锁范围，不要做 R2-3。
- 执行边界以本节和各条「修完后应满足」为准；文档里的异常类型只是示例，不是封闭名单。
- 改完后复跑（必须带可写 basetemp）：

```powershell
cd E:\agent_order_myself\backend
E:\agent_order_myself\.venv\Scripts\python.exe -m pytest -q --basetemp=E:\agent_order_myself\tmp\pytest-review
```

接口或前端请求字段若变了，再跑：

```powershell
cd E:\agent_order_myself\frontend
npm.cmd run build
```

基线：`64 passed`（2026-08-31，`--basetemp=E:\agent_order_myself\tmp\pytest-review`）。不得为了绿灯删测试。

---

## 项目硬约束（违反即视为修错）

1. 识别只产生草稿，必须人工确认后才建单。未匹配商品不能落单。
2. DeepSeek 只做文本结构化抽取；商品事实、价格、校验、落库由本地 Python 控制。
3. 外部模型失败、空响应、JSON 无效必须回退本地规则。
4. 不把 `.env` 中的 API Key 写进源码、日志、README、测试或响应。
5. 日志只记步骤、状态、耗时、可解释摘要。
6. HTTP `detail` 只能是用户可理解的中文，禁止堆栈、codec 原文、SQLite 约束名、httpx/ES 原文。
7. 不虚构新业务规则。目录价允许人工覆盖、别名需审核后才进版本。
8. `products` 仍是全局表，不要拆真多租户。
9. 目录写入成功但索引失败，禁止再返回 500。
10. 不要 `docker compose down -v`，不要删 volume，不要引入 Redis/Celery，不要重写整个 `database.py`。

---

## 已关闭（不要回退、不要重做）

上一轮已落地：分块上传、`to_thread`、`safe_index_sync` / `index_synced`、`bulk` 检查 `errors`、`draft_id=task_id` 且有草稿则 complete、Checkpointer `RLock`、目录写接口 `site_id` 必填、确认看 `needs_review`、抽取层 NaN 降级、导入单价校验、WAL、`list_orders` 非 N+1、指标 SQL、rejected 别名不翻回、ES close、像素/文本上限。

### R2-3 已撤回（禁止按第二轮原文改）

第二轮曾要求给 `create_product` / `update_product` / `import_products` 补 `BEGIN IMMEDIATE`。这不是当前版本号并发漏洞。

**事实（已在本机 3.12.10 核实）：**

- `sqlite3.connect(...)` 未改隔离级别，实际是 `isolation_level=''`、`autocommit=LEGACY_TRANSACTION_CONTROL(-1)`。
- 该模式下，第一条 `INSERT/UPDATE/DELETE` 会**隐式开启**写事务。`create/update/import` 都是先 DML，再 `_publish_catalog_locked` 里 `MAX(version)+1`，因此 `MAX(version)` 已在同一写事务中。
- SQLite 同时只允许一个写事务；WAL 下写者仍串行。
- 在现有 DML 之后再执行 `BEGIN IMMEDIATE` 会触发 `cannot start a transaction within a transaction`（已用最小脚本复现）。

`create_sku_version` 里的 `BEGIN IMMEDIATE`（`database.py:485`）是因为它在**任何 DML 之前**就要读版本。不要把这套套到已经先 UPDATE/INSERT 的路径上。

若只想让意图更明显，必须把 `BEGIN IMMEDIATE` 移到**第一条写语句之前**。那是可读性，不是本轮正确性修复。**本轮不要做。**

### R2-7 本轮不要动

`graph.invoke()` 期间 Checkpointer 会使用共享 SQLite 连接。缩小锁范围会重新引入并发错误。本地演示可保留现状。真要优化需要换连接策略或 Checkpointer 架构，超出本轮。

### 明确降为非阻断（本轮不要做）

- 文件识别 Content-Length 预检一律 8MB
- xlsx zip 申报 `file_size` 可谎报
- 拒绝别名时 `index_synced=True` 默认值（前端只在 approved 时读取）
- 启动同步写死 `site_id=1`
- parser 数字商品名、health 不探 ES、CORS、`max_tokens=1200`

---

## 【必须修】本轮只做这些

### R2-1 JSON 识别仍默认 `site_id=1`

| 项 | 内容 |
|---|---|
| 位置 | `backend/app/schemas.py:116` `RecognitionRequest.site_id = Field(default=1, ge=1)` |
| 同类 | `frontend/src/api.ts:61-67` `createRecognitionTask` 只发 `{ text, customer_id }`，不传 `site_id`。同步识别请求同样要显式带上。 |
| 范围（验收标准，不要扩大） | **只强制两个 JSON 识别入口必须传 `site_id`：** `POST /api/drafts/recognize`、`POST /api/recognition-tasks`。`GET /api/metrics/business`、`GET /api/catalog/versions/current` 的 `site_id=1` **保持演示配置，本轮不要改。** |
| 后果 | 目录写接口已强制显式站点，文本识别漏传仍进站点 1，可能串站。 |
| 修完后应满足 | `RecognitionRequest.site_id` 必填、`ge=1`、无默认。前端同步识别和 `createRecognitionTask` 显式传 `site_id`（演示站传 `1`）。上述两个 POST 不传 `site_id` → 422。现有测试缺该字段的补 `site_id: 1`，不要把默认值加回去。不要顺手改两个 GET。 |

### R2-2 上传流只在超限时关闭

| 项 | 内容 |
|---|---|
| 位置 | `backend/app/main.py:192-203` `read_upload_limited` |
| 后果 | 超限才 `await file.close()`。成功或 `read()` 中途异常没有 `finally`，可能留下 `SpooledTemporaryFile`。 |
| 修完后应满足 | `try/finally` 关闭 `UploadFile`；超限立即停读并关流；成功路径也关；重复 close 可容忍。现有超限测试继续通过，并覆盖成功读完也会 close。 |

### R2-4 全局 422 吃掉字段级错误

| 项 | 内容 |
|---|---|
| 位置 | `backend/app/main.py:299-310` `RequestValidationError` 一律「请求参数不完整或格式错误」 |
| 后果 | 缺 `site_id`、空明细、负单价、行号重复，前端无法区分。 |
| 修完后应满足 | 按 Pydantic 每条错误的 `loc` 和错误类型（如 `missing`、`greater_than`、`less_than`、`finite_number`）映射成短中文。缺 `site_id` 必须能看出是站点参数问题。 |
| 禁止返回 | `input` 原值、`ctx.error`、整份 `error.errors()`、Pydantic 英文原文、堆栈、codec、sqlite、httpx。`detail` 可以是一句中文，或「字段名 + 中文原因」的短列表，但内容必须是自己映射出来的，不能把校验框架的内部结构转发出去。 |

### R2-5 无草稿但有半截 checkpoint 时再 `invoke`

| 项 | 内容 |
|---|---|
| 位置 | `task_runner.py:109-128` 只在 `get_draft(task_id)` 命中时短路 |
| 同类 | `workflow.py:102-109` 无草稿就对同一 `thread_id`（`task_id`）再 `invoke` |
| 已覆盖 | `tests/test_backend_hardening.py:114-130` 只测了草稿已存在 |
| 后果 | `save_draft` 之前崩溃：不会复制草稿，但第二次 `invoke` 撞半截 checkpoint，行为未定义。 |
| 修完后应满足 | 有草稿 → 只 complete，不重跑（保持）。无草稿但 checkpoint 已存在 → **先 `delete_thread()` 清掉该 thread，再重新 `invoke`**，不要赌续跑语义。 |
| 锁 | 「检查是否有 checkpoint → `delete_thread()` → 重新 `invoke`」必须全部放在**同一个** `_checkpoint_lock` 内。禁止检查完放锁、再删、再 invoke。删和 invoke 之间若穿插其它线程的 `get_state`/`invoke`，会把孤儿 checkpoint 的竞争再引进来。 |
| 草稿二次检查 | 锁外可以先查一次草稿做快路径，但**取得 `_checkpoint_lock` 之后必须再查一次草稿**。防止：锁外第一次看到「无草稿」→ 等待锁期间另一个执行已经 `save_draft` → 本执行再 `delete_thread()` + `invoke` 会清掉刚写好的 checkpoint 并重复建草稿。锁内若已有草稿：直接返回/complete，不要删 thread、不要再 invoke。 |
| 测试 | 有 checkpoint、无 drafts 行时，同一 `task_id` 再执行只产生一份草稿、任务能结束、不自动建单。取消后可留待审草稿、绝不自动建单，语义不变。 |

### R2-8a 确认建单补有限数防御

| 项 | 内容 |
|---|---|
| 位置 | `backend/app/database.py:937-938` 只判断 `quantity <= 0`（单价只判断 `< 0`） |
| 事实 | Python 中 `float('nan') <= 0` 为 False，历史草稿里的 NaN 能绕过。抽取层已挡住新数据，落单层仍应兜底。 |
| 修完后应满足 | `confirm_draft` 对 `quantity`、`unit_price` 使用 `math.isfinite`，非有限或越界与 `<=0` / `<0` 同样拒绝，422 中文，不建单。 |

### R2-8b 异常映射收口（与 R2-4 一起做，不要另开重构）

| 位置 | 问题 |
|---|---|
| `main.py:122` | 解析 xlsx 的 `except Exception` 过宽，`MemoryError` 会变成 422「无法解析」 |
| `main.py:388` | 创建商品 `ValueError` 用 `str(exc)` |
| `main.py:417` | 更新商品 `LookupError` 用 `str(exc)` |
| `main.py:511-513` | 别名审核 `str(exc)` |
| `main.py:598, 622, 626` | 识别 / 建任务 `str(exc)` |
| `main.py:864-868` | 确认建单 `str(exc)` |
| `task_runner.py:177-178` | `ValueError` 原样写入任务 `error_message` |

修完后：业务 `ValueError`/`LookupError` 的中文可以给前端；其余走固定中文。任务表 `error_message` 对非业务异常用固定中文。导入/文件上传已有 `public_error()`，其它入口对齐同一套白名单，不要复制多套逻辑。

xlsx **不要裸 `except Exception`**，但上面列出的 `BadZipFile` / `InvalidFileException` / `ValueError` / `TypeError` **只是示例，不是封闭名单**。收窄之后必须用测试兜底，避免原来的友好 422 变成 500。至少覆盖：

1. 损坏 ZIP（不是 zip / 截断 zip）→ 422，不是 500。
2. 合法 ZIP 但内部 XML/工作表损坏（能被 `ZipFile` 打开、`load_workbook` 失败）→ 422，不是 500。
3. 现有合法 xlsx 导入测试仍 200。

若测试发现还有 openpyxl 专用异常漏到 500，把它纳入捕获并映射为「XLSX 无法解析」，不要为了名单好看而漏捕。`MemoryError` 不要装成 422。

---

## 明确不要动

- 不要回退 `safe_index_sync`、`index_synced`、`draft_id=task_id`、WAL、导入 `ProductCreate` 校验、抽取层 NaN 降级。
- **不要给 `create_product` / `update_product` / `import_products` 在现有 DML 之后加 `BEGIN IMMEDIATE`。**
- **不要缩小 `workflow.py` 的 `_checkpoint_lock` 范围。**
- 不要让未匹配商品能 `confirm`。
- 不要删除人工确认 / LangGraph interrupt。
- 不要把 DeepSeek 接到匹配、定价、落库。
- 不要引入 Redis/Celery，不要拆 `products` 表。
- 前端只改：识别/建任务补 `site_id`；422 `detail` 仍展示给用户。

---

## 验证

```powershell
cd E:\agent_order_myself\backend
E:\agent_order_myself\.venv\Scripts\python.exe -m pytest -q --basetemp=E:\agent_order_myself\tmp\pytest-review
```

保住上一轮 64 通过，并覆盖：

1. JSON `POST /api/drafts/recognize` 与 `POST /api/recognition-tasks` 不传 `site_id` → 422。两个 GET 的默认站点行为保持不变。
2. 前端实际 payload（含 `site_id: 1`）：同步识别接口仍返回 200；异步建任务接口仍返回 202。
3. `read_upload_limited` 成功读完也会 close；超限仍 close 且不再继续 read。
4. 缺 `site_id` 或负单价的 422，`detail` 是映射后的中文，能区分问题类型；响应中不得出现 `input`、`ctx`、英文 Pydantic 结构、sqlite/httpx/堆栈。
5. 有 checkpoint、无 drafts 行时，同一 `task_id` 再执行只产生一份草稿、不建单；检查 checkpoint / `delete_thread()` / `invoke` 在同一把 `_checkpoint_lock` 里，且**加锁后再次确认草稿仍不存在**才允许删除并重跑。
6. 确认建单时数量/单价为 NaN 或 Inf → 422，不落单。
7. 损坏 ZIP、以及合法 ZIP 但损坏 XML 的 xlsx 导入 → 422，不是 500；合法目录 xlsx 仍 200。

改完后说明：测了什么、是否仍 64+、有无误改 R2-3/R2-7、有无改两个 GET 的 `site_id`。
