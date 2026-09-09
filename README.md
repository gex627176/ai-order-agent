# 拾单：本地 AI 录单 Agent

一个为学习和面试演示制作的前后端分离项目。输入自然语言或上传 TXT/CSV/XLSX、订单图片、PDF，Agent 抽取商品与数量，再用站点级版本目录匹配、补价、校验，人工确认后写入 SQLite。

## 最小流程

1. 选择演示客户。
2. 输入 `番茄5斤，胡萝卜3公斤切丁，苹果2箱`，或上传 UTF-8 TXT、CSV、XLSX、JPG/PNG、PDF。
3. 查看并修改识别草稿与 Agent 执行轨迹；空结果可人工新增商品行。
4. 点击“确认并创建订单”。
5. 在最近订单中查看持久化结果；刷新页面后仍然存在。

文本与文件识别都通过本地持久化任务队列执行，页面通过 SSE 实时展示排队、运行、重试、失败、取消和完成状态，不再每 600ms 轮询。页面还提供“最近草稿”区域。刷新或重启后端后，前端会恢复未完成任务和最近一条待审核草稿，也可以手动切换查看其他待审核或已确认草稿。SSE 断线时会先通过单任务 GET 对账，随后有限重连并最终降级为 3 秒低频有限轮询。人工修改商品、数量、单位、单价、备注或客户后会在 800ms 无新输入时自动保存；页面显示等待、保存中、已保存或失败状态。已确认草稿为只读状态，不能重复编辑或重复确认。

识别和建单是两个独立动作。LangGraph 在人工审核点保存 Checkpoint 并暂停；任何未匹配商品都不能直接落单。

仓库提供纯合成文件示例 `examples/demo_order.csv`，可以直接在页面的文件入口上传。图片与扫描 PDF 使用本地 Tesseract 分别生成 `chi_sim` 与 `eng` 候选，再按文字脚本和置信度择优；文本型 PDF 优先直接提取文字。图片/PDF 最大 8MB，PDF 最多 5 页，OCR 平均置信度低于 70% 时草稿会显式要求逐项人工核对。

## Docker 启动

确保 Docker Desktop 已启动，然后在项目根目录执行：

```powershell
docker compose up --build
```

打开 <http://localhost:8080>。停止服务：

```powershell
docker compose down
```

订单数据保存在 Docker 命名卷 `order_data` 中，普通 `down` 不会删除。只有明确执行 `docker compose down -v` 才会删除演示数据。

## AI 模式

复制 `.env.example` 为 `.env`，按需选择：

- `AI_PROVIDER=rules`：完全离线、无需密钥，适合稳定演示。
- `AI_PROVIDER=deepseek`：调用 DeepSeek JSON 模式做文本抽取；失败会自动回退规则解析。
- `AGENT_HARNESS_PROVIDER=rules|deepseek`：控制独立 Harness 的编排器；默认继承 `AI_PROVIDER`，也可单独固定为离线规则模式。

Prompt 通过 `DEEPSEEK_PROMPT_VERSION` 从代码内受控注册表选择，默认 `order-extraction-v1`。`order-extraction-v2` 是严格抽取实验版，不允许通过环境变量注入任意提示词；切换前应使用同一数据集运行版本对比。

模型监控会记录 Provider、模型、Prompt 版本、Token、耗时、状态和规则降级原因。费用由下面两个环境变量按“每百万 Token 的费用单位”估算；默认值为 `0`，避免在未配置实际价格时展示虚假成本：

- `DEEPSEEK_INPUT_COST_PER_MILLION`
- `DEEPSEEK_OUTPUT_COST_PER_MILLION`

密钥只放本地 `.env`，该文件已加入 `.gitignore`。启用云模型抽取时，订单原话会发送到所配置的模型服务；启用云端 Harness 编排时，多轮消息以及 Agent 主动查询到的按工具白名单投影结果会回传给模型完成下一轮规划。任务幂等键、载荷摘要、内部错误、API Key、隐藏推理和非必要正文不会进入模型工具结果；事件写入还会做递归字段脱敏。

## 独立 Agent Harness

Harness 位于原 LangGraph 录单工作流之外。主 Agent 可以多轮理解请求并动态选择工具，但商品、价格、客户、草稿状态和建单结果始终由本地 Python 能力决定。模型不可见 `draft_confirm`；正式建单只能由独立的 `resume` API 创建与当前会话、草稿、版本和规范化草稿指纹匹配的审批凭证。旧草稿编辑、旧确认入口和 Harness 批准共享单进程审核锁；审批后内容发生变化时会撤销本次审批并要求重新检查。

页面顶部可以在“标准录单”和“Agent 对话”之间切换。Agent 工作区支持创建或刷新恢复持久会话、按真实事件顺序展示每轮用户消息与 Agent 回复、查看工具/模型/子 Agent 的可解释事件摘要，以及对通过目录校验的草稿明确批准或拒绝。会话 ID 保存在浏览器本地存储，实际会话、状态和事件保存在 SQLite；“大土豆”一类含修饰词的名称可召回“土豆”作为人工核对候选，但名称/已审核别名不完全一致时仍不会自动匹配，普通聊天消息也不能代替 `/resume` 审批。

主要入口：

- `POST /api/agent/sessions`：创建绑定 `site_id` 和可选客户的持久会话。
- `POST /api/agent/sessions/{id}/messages`：发送多轮消息，可带 `Idempotency-Key`。
- `GET /api/agent/sessions/{id}?after_sequence=0&limit=100`：读取状态与分页事件。
- `POST /api/agent/sessions/{id}/resume`：明确批准或拒绝当前待确认草稿，可带 `Idempotency-Key`。

本地规则与 DeepSeek Tool Calling 使用同一组带 JSON Schema 的受控工具：客户查询、客户偏好、当前站点 SKU 快照查询、草稿识别、当前草稿读取和识别任务读取。工具参数会在本地再次校验，站点、客户和草稿 ID 由会话注入，模型不能覆盖。每条消息最多 6 轮规划、8 次工具尝试；模型拒绝/失败、客户选择后的自动草稿续接和 SKU 子 Agent 都共用该预算。环境变量只能下调，不能突破代码绝对上限。

未匹配 SKU 会委派给唯一的 `sku-resolution` 子 Agent。它只拥有目录查询工具，同一草稿指纹只委派一次、累计最多执行三次目录检索；单次失败不会触发整组重跑，也不会自动修改草稿。人工仍在原草稿入口修正，随后向会话发送“重新检查”；草稿指纹变化且本地校验通过后，会话才进入 `waiting_approval`。

离线 HTTP 演示示例：

```powershell
$session = Invoke-RestMethod -Method Post -Uri http://localhost:8000/api/agent/sessions -ContentType application/json -Body '{"site_id":1}'
Invoke-RestMethod -Method Post -Uri "http://localhost:8000/api/agent/sessions/$($session.id)/messages" -ContentType application/json -Headers @{"Idempotency-Key"="demo-message-1"} -Body '{"content":"帮我录番茄5斤"}'
Invoke-RestMethod -Method Post -Uri "http://localhost:8000/api/agent/sessions/$($session.id)/messages" -ContentType application/json -Headers @{"Idempotency-Key"="demo-message-2"} -Body '{"content":"客户是惠民餐厅"}'
Invoke-RestMethod -Method Post -Uri "http://localhost:8000/api/agent/sessions/$($session.id)/resume" -ContentType application/json -Headers @{"Idempotency-Key"="demo-confirm-1"} -Body '{"approved":true}'
```

Harness 会话、事件和幂等响应保存在自己的 SQLite 表中；消息与恢复请求会在任何工具副作用前写入 `in_progress` 占位，键和载荷使用带域分隔的 SHA-256 摘要，旧明文键表会在启动时以原子迁移兼容升级。崩溃后同键同载荷可继续收敛，同键异载荷始终冲突。若草稿通过旧接口先完成确认，GET、消息或恢复入口会根据本地订单事实把 Harness 会话收敛到 `completed`。业务工具通过带类型的业务网关复用现有工作流、任务执行器和数据查询能力。当前并发保证面向单 FastAPI 进程；为保证草稿创建、绑定、编辑和确认一致，单进程共享审核锁会覆盖完整识别和 SKU 候选整理，慢模型期间可能短暂阻塞其他草稿编辑/确认。当前不宣称支持多进程或分布式 Agent 调度，未来可改为按草稿分片锁或事务化绑定。

## 本地开发

### 在 VS Code 中运行（推荐学习方式）

1. 安装 VS Code 的 Microsoft Python 扩展。
2. 打开左侧“运行和调试”，选择 `FastAPI 后端（本地调试）`，按 `F5`。该配置明确使用项目 `.venv\Scripts\python.exe`，不会落到系统 Python。
3. 运行“终端 → 运行任务 → 前端：启动开发服务”。
4. 打开 <http://localhost:3000>。此时前端请求会代理到 VS Code 中的 Python 后端，断点和 Agent 日志都可直接查看。

本地开发和 Docker 演示可以同时存在：`8080` 是 Docker 完整版，`3000` 是连接 VS Code 后端的开发版。
新建 VS Code 终端时，Python 扩展会自动激活项目 `.venv`；修改配置前已经打开的旧终端需要关闭后重新创建。

后端（Python 3.12）：

```powershell
cd backend
..\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8000
```

前端：

```powershell
cd frontend
npm.cmd install --cache .npm-cache
npm.cmd run dev
```

运行前端自动测试和生产构建：

```powershell
cd frontend
npm.cmd run test
npm.cmd run build
```

运行后端测试：

```powershell
cd backend
..\.venv\Scripts\python.exe -m pytest -q
```

## 可解释日志

FastAPI 容器的标准输出会记录 Agent 节点，例如模型抽取、文本解析、目录匹配、校验、保存草稿和人工确认，以及各步骤耗时与结果摘要。普通 HTTP `200` 访问不输出；`201/202` 等状态变化、`4xx/5xx`、未处理异常和任务状态仍会记录：

```powershell
docker compose logs -f backend
```

日志不输出 API Key，也不伪装成模型的隐藏思维过程。

## 模型监控与成本

首页“模型运行监控”展示调用次数、成功率、降级率、P95 时延、累计 Token 和估算费用。P95 使用全部历史模型调用；少于 20 次时页面会明确提示此时 nearest-rank P95 等于最慢样本，较大毫秒值会换算为秒显示，但不会删除或美化历史异常。纯规则模式不会制造模型调用记录，因此没有密钥也能正常启动和演示。

DeepSeek 网络阶段默认 15 秒无响应超时（`DEEPSEEK_TIMEOUT_SECONDS` 可配置），触发后传统识别会回退本地规则，Harness 会切换确定性规划；抽取请求显式关闭思考模式，减少结构化 JSON 场景的无效长输出。它用于压缩卡死/停顿型尾延迟，不是整个请求的绝对墙钟截止时间，也不保证外部模型正常响应一定更快。

只读指标接口：

- `GET /api/metrics/overview`
- `GET /api/metrics/models`
- `GET /api/metrics/failures?limit=50`

调用记录保存在同一个 SQLite 命名卷中。监控记录写入失败不会阻断识别主流程；模型失败时仍按原有安全边界回退本地规则。

## 商品目录与纠错治理（非 RAG）

首页提供站点商品目录管理、业务指标和纠错别名审核区。这部分不建设通用文档知识库，也不让模型直接改正式商品数据。

- 商品支持新增、编辑、启用/停用；每次保存都会发布新的不可变 `sku_version`，旧草稿和旧订单继续引用原版本。
- 支持 CSV/XLSX 批量导入，列名为 `sku/name/aliases/unit/unit_price/active`；`aliases` 可用逗号、分号或顿号分隔，文件最大 2MB。
- 上传采用分块累计限额，成功、超限或读取异常都会关闭上传流；明显超限的请求会在进入业务解析前拒绝。目录最多 5000 行，订单文件最多 200 行、归一化文本最多 5000 字符，XLSX 还限制解压后体积，损坏 ZIP/XML 会返回安全的中文 422。
- 人工把未匹配文本改选为正式商品时，只生成待审核别名候选；审核通过后才写入商品别名并发布新版本，拒绝不会改目录。
- 目录变更记录来源、动作、变更前后内容和时间；导入使用批量 upsert，索引由已发布版本重建。
- 目录创建、修改、导入和手动发版必须显式提供 `site_id`。SQLite 发布成功后即使 Elasticsearch 同步失败也不会伪装成写入失败；响应通过 `index_synced/index_error` 提示，并可调用 `POST /api/catalog/index/sync?site_id=1` 补偿同步。
- 业务指标包括任务成功率、自动匹配率、未匹配率、人工修改率、待审别名、启用商品、当前版本和任务 P95。

### 人工补录与价格覆盖

- 模型返回 0 项或漏项时，审核区可以点击“新增商品行”，不需要重新调用模型。
- 人工行必须选择当前草稿 SKU 版本中的启用商品；不能用任意文本绕过本地目录校验。
- 选择商品时会带出目录单位和目录价；单位可从当前商品目录的去重下拉列表选择，也可通过“＋ 新建单位”填写目录外单位。自定义单位只随当前草稿行保存，不会自动建立全局单位或换算规则；数量和单价仍可手工修改，目录价只是默认值。
- 前端先检查商品、数量、单位和非负单价；后端再限制空明细、重复行号、非有限数值与异常大数，确认时重新校验 SKU 快照。
- 合法修改会在停止输入 800ms 后自动保存；未完成的新行会显示“填写完整后自动保存”，不会反复请求无效接口。保存失败可手动重试，未保存时切换草稿或关闭页面会受到提示。

主要接口：

- `POST/PUT /api/catalog/products[/{product_id}]`
- `POST /api/catalog/import`
- `GET /api/catalog/alias-candidates`
- `POST /api/catalog/alias-candidates/{candidate_id}/review`
- `GET /api/metrics/business`

## 异步识别任务

文本与文件入口使用 SQLite 持久化队列和单后台工作线程，不需要额外安装 Redis 或 Celery。单工作线程与当前 SQLite LangGraph Checkpointer 的本地定位一致，也让 VS Code 和 Docker 保持原有启动方式。旧同步文件草稿接口仍保留用于兼容。

- `POST /api/recognition-tasks`：创建任务；JSON 必须显式提供正整数 `site_id`，可传 `Idempotency-Key`，同键同载荷返回原任务，同键不同载荷返回 409。同步 JSON 识别接口同样要求显式站点。
- `POST /api/recognition-tasks/file`：上传 TXT/CSV/XLSX/JPG/PNG/PDF 并创建异步识别任务。
- `GET /api/recognition-tasks`：列出最近任务，可用 `status` 和 `limit` 筛选。
- `GET /api/recognition-tasks/{task_id}`：读取任务状态、尝试次数和失败原因。
- `GET /api/recognition-tasks/{task_id}/events`：订阅 `event: task` SSE；首帧来自 SQLite，状态变化主动推送，15 秒无变化时发送 heartbeat，终态后关闭。
- `POST /api/recognition-tasks/{task_id}/retry`：手动重跑失败任务。
- `POST /api/recognition-tasks/{task_id}/cancel`：取消排队任务；运行中任务在当前步骤结束后取消。

临时执行异常按指数退避自动重试，业务输入和 SQLite 约束类确定性错误直接失败。应用重启会恢复 `running`、`pending` 和 `retrying` 任务；若中断只留下 Checkpoint 而未保存草稿，会先在同一把锁中清理孤儿 thread 再重跑。任务成功只生成 `needs_review` 草稿，正式订单仍必须经过人工确认。

SSE Broker 是单 FastAPI 进程内的线程安全有界通知层，每个订阅只保留最新任务通知；SQLite 仍是唯一事实来源。Nginx 仅对任务事件路径关闭响应缓冲和缓存。当前方案不支持多 worker/多实例广播；若未来横向扩容，需要替换为 Redis Pub/Sub 等跨进程事件层。

## 草稿恢复

- GET /api/drafts：按更新时间倒序列出草稿。
- GET /api/drafts?status=needs_review&limit=20：只查询待审核草稿。
- GET /api/drafts/{draft_id}：读取单个草稿。
- GET /api/drafts/{draft_id}/workflow：读取持久化 Checkpoint 状态。

草稿列表和 LangGraph Checkpoint 都保存在本地 SQLite 中。前端刷新只会重建页面组件，不会再失去进入持久化草稿的入口；人工编辑由防抖保存写回同一草稿。

## 前端韧性与订单详情

首屏将客户、商品、订单、草稿和识别任务作为核心录单数据加载，模型监控与失败记录作为可选数据独立加载；非核心指标失败只在对应面板显示降级提示，不会阻断录单。商品治理面板同样会分别保留已成功返回的业务指标或别名候选。

“最近订单”可打开详情抽屉，展示客户、商品行、数量、单位、单价、小计、总额、站点、SKU 版本和输入来源，并通过原草稿补充本地目录召回证据摘要。前端使用 Vitest 与 React Testing Library 覆盖防抖保存失败重试、空结果人工补录、未保存草稿切换拦截、目录保存后 ES 同步失败提示、非核心指标降级和订单详情。

## 已落地的面试证据链

- LangGraph `StateGraph` 编排解析、召回、偏好加载、校验、草稿保存、人工审核和确认节点。
- SQLite Checkpointer 按草稿 `thread_id` 持久化；`interrupt()` 暂停审核，确认时用 `Command(resume=...)` 恢复。
- Elasticsearch 对当前站点/SKU 版本做关键词与 64 维本地确定性向量混合候选召回；名称/别名精确命中才允许自动匹配。
- SKU 使用不可变版本快照；草稿和订单保存识别时的 `site_id`、`sku_version` 与召回证据。
- 确认接口使用 `Idempotency-Key`、规范化 payload MD5、SQLite 唯一约束三层防重；同键不同载荷返回 409。
- 草稿人工编辑记录字段级纠错；已确认的明确商品备注聚合为客户偏好证据，但不会静默改写新订单。
- 商品目录变更以新 SKU 版本发布；人工改选产生别名候选，只有审核通过后才进入正式目录。
- 业务看板统计自动匹配、未匹配、人工修改、目录状态和识别任务时延，不用离线评测指标冒充单次置信度。
- 模型调用按 Prompt 版本记录 Token、费用、耗时、状态和降级原因，并提供聚合接口与前端看板。
- SQLite 持久化识别任务支持幂等创建、指数退避、失败分类、手动重跑、取消和应用重启恢复。
- 识别任务状态通过有界进程内 Broker 和 SSE 主动推送；SQLite 首帧、断线 GET 对账与有限低频回退保证页面最终收敛。
- 异步任务使用任务 ID 作为固定草稿 ID，保存草稿后即使 worker 中断，恢复时也只会完成原草稿；无草稿的半截 Checkpoint 会在锁内二次检查后清理并重跑。Checkpoint 调用继续通过进程内锁串行访问共享 SQLite 连接。
- 文本 PDF 直接抽取，图片/扫描 PDF 使用本地中英文 OCR；置信度与输入警告随任务、草稿持久化并展示。
- 独立 Agent Harness 支持持久多轮会话、DeepSeek 动态 Tool Calling、规则降级、6/8 硬预算、按指纹去重的只读 SKU 子 Agent、副作用前幂等占位、事件递归脱敏、旧确认状态对账和显式审批凭证；原录单接口与 LangGraph 交易流保持不变。
- 115 条冻结核心集与 200 条确定性生成扩展集，共 315 条完全合成样本；评测器输出字段准确率、Recall@3、MRR、人工处理率、时延、Token 和成本，并支持 Prompt 版本回归门禁。

这里的本地向量用于证明完整检索链路，不等同于生产级语义 embedding；Checkpoint 采用单机 SQLite，适合个人 Demo，不宣称支持分布式执行。

## 离线评测

```powershell
cd backend
..\.venv\Scripts\python.exe -m evaluation.generate_extended_fixture --check
..\.venv\Scripts\python.exe -m evaluation.run_evaluation --provider rules --fixture tests\fixtures\order_recognition_extended_315.jsonl --label rules-extended-315
```

Docker 已包含评测脚本和合成 fixture，可复用容器中的模型配置运行同集对比：

```powershell
docker compose exec -T backend python -m evaluation.run_evaluation --provider deepseek --prompt-version order-extraction-v1 --fixture tests/fixtures/order_recognition_extended_315.jsonl --per-type 2 --output-dir /app/data/evaluation_results --label deepseek-v1-stratified-34
docker compose exec -T backend python -m evaluation.run_evaluation --provider deepseek --prompt-version order-extraction-v2 --fixture tests/fixtures/order_recognition_extended_315.jsonl --per-type 2 --output-dir /app/data/evaluation_results --label deepseek-v2-stratified-34
```

版本报告必须来自相同的 `dataset.sha256`，随后执行：

```powershell
docker compose exec -T backend python -m evaluation.compare_evaluations /app/data/evaluation_results/deepseek-v1-stratified-34.json /app/data/evaluation_results/deepseek-v2-stratified-34.json --output-dir /app/data/evaluation_results --label deepseek-v1-v2-stratified-34
```

容器评测结果保存在现有 `order_data` volume 的 `/app/data/evaluation_results`，不会写入订单表。仓库已经保存本次可复核的 v1、v2 和版本对比 JSON/Markdown 报告。

当前扩展规则基线：315 条精确通过率 96.83%，Recall@3/MRR 97.17%，人工处理率 25.40%，平均 20.96ms、P95 24.44ms。DeepSeek 同集分层 34 条中，v1/v2 均为 100% 且无降级；v2 平均 Token 由 379.35 增至 513.53，增长约 35%，超过默认 20% 门禁，因此生产默认仍为 v1。费用单价未配置，所以报告费用为 0；这不代表真实调用免费。所有样本均为合成数据，单次 P95 波动不能视为稳定性能提升。

## 架构

- `frontend/`：React + TypeScript + Vite，生产环境由 Nginx 提供页面并反向代理 `/api`。
- `backend/`：FastAPI、独立 Agent Harness、LangGraph、SQLite 任务队列、确定性解析器、DeepSeek Provider、模型调用监控、ES SKU 召回和 SQLite 数据层。应用级数据库、检索器、工作流、任务执行器、Harness 和配置由 lifespan 管理，并通过带明确类型的 FastAPI 依赖注入路由；共享审核锁也使用真实的标准库可重入锁类型注解，便于 IDE 补全和测试替换。
- `compose.yaml`：前端、后端和 Elasticsearch 三个服务；SQLite 与 ES 分别使用命名卷持久化。

## 当前演示数据与边界

项目只内置虚构的客户和五种商品，用于证明流程，不代表真实公司的业务规则。当前仍是单站点商品主表加站点版本快照，并非完整多租户商品隔离；写接口及 JSON 识别入口要求显式站点只是防止误写默认站点，两个只读展示接口仍保留站点 1 的演示默认值。当前规则解析支持逗号/换行分隔的“商品 + 数量 + 单位 + 可选备注”；复杂口语依赖模型增强。价格以本地演示目录为准，建单前必须人工确认。请求字段错误会映射为简短中文，不回传 Pydantic 输入值或底层异常原文。OCR 置信度是页面级平均参考值，不是字段级保证；图片和 PDF 页面还受边长及总像素限制。当前不支持语音、手写专项模型和真实公司目录同步。

## VS Code 本地 OCR 依赖

Docker 镜像已经内置 Tesseract 和简体中文语言包，无需额外配置。直接用 VS Code F5 跑后端时，文本 PDF 不需要 Tesseract；JPG/PNG 和扫描 PDF 需要 Windows Tesseract OCR 及 `chi_sim`、`eng` 语言数据。后端会优先发现项目内 `.venv/tools/tesseract/tesseract.exe`，也支持通过 `TESSERACT_CMD` 指定路径，最后才读取系统 `PATH`。可用以下命令检查系统安装：

```powershell
tesseract --list-langs
```

输出应同时包含 `chi_sim` 和 `eng`。缺失时接口会返回 422 和明确安装提示，不会静默跳过 OCR。
