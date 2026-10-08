# Local AI Order Agent

一个本地运行的 AI 录单应用：将自然语言或订单文件识别为订单草稿，匹配本地商品目录，经人工核对后创建订单。

## 快速启动

需要提前安装并启动 Docker Desktop。

```bash
cp .env.example .env
docker compose up --build -d
```

Windows PowerShell 可使用：

```powershell
Copy-Item .env.example .env
docker compose up --build -d
```

启动完成后访问：

```text
http://localhost:8080
```

查看服务状态：

```bash
docker compose ps
```

停止服务：

```bash
docker compose down
```

普通停止不会删除订单和商品数据。数据保存在 Docker 命名卷中。

## 模型配置

`.env.example` 默认使用本地规则模式，无需 API Key：

```dotenv
AI_PROVIDER=rules
AGENT_HARNESS_PROVIDER=rules
```

如需启用 DeepSeek，将 `.env` 中的 Provider 改为 `deepseek`，并在本地填写对应 API Key。不要提交 `.env` 文件。

## 使用方式

1. 打开网页并选择客户。
2. 输入订单描述，或上传支持的订单文件。
3. 核对商品、数量、单位、价格和备注。
4. 修正未匹配的商品。
5. 人工批准后创建订单。

系统也提供 Agent 对话入口。Agent 可以生成和修改草稿，但不能跳过人工确认直接创建订单。

## 主要功能

- 自然语言订单识别
- TXT、CSV、XLSX、图片和 PDF 导入
- 本地商品目录检索与价格补全
- 草稿人工编辑和自动保存
- Agent 多轮对话与草稿备注修改
- 人工确认后建单
- SQLite 持久化订单、草稿和会话
- Elasticsearch 商品候选召回
- 识别失败时本地规则回退

## 代码结构

```text
backend/        FastAPI 后端、Agent 工作流、目录检索和 SQLite 持久化
frontend/       React + TypeScript 前端
examples/       示例订单文件
compose.yaml    Docker Compose 配置
```

主要技术栈：FastAPI、React、TypeScript、LangGraph、SQLite、Elasticsearch、Docker。

## 本地测试

后端：

```powershell
cd backend
..\.venv\Scripts\python.exe -m pytest -q
```

前端：

```powershell
cd frontend
npm.cmd install
npm.cmd run test
npm.cmd run build
```

## 健康检查

```text
GET http://localhost:8080/api/health
```
