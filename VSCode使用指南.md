# 在 VS Code 中运行和继续开发“拾单”

## 一、打开项目

在资源管理器中双击 `agent_order_myself.code-workspace`，或者在终端执行：

```powershell
code E:\agent_order_myself\agent_order_myself.code-workspace
```

如果 VS Code 提示推荐扩展，安装 Microsoft Python 扩展即可。前端不要求额外扩展。

## 二、用 VS Code 跑 Python 后端

1. 点左侧“运行和调试”（三角形和小虫子图标）。
2. 顶部选择 `FastAPI 后端（本地调试）`。
3. 按 `F5`。
4. 看到 `Uvicorn running on http://0.0.0.0:8000` 就代表后端启动成功。

Python 日志会直接出现在 VS Code 集成终端。识别一条订单时，你会看到：

- `解析订单文本`
- `召回并匹配站点 SKU`
- `校验订单草稿`
- `保存待确认草稿`
- `人工确认并创建订单`

为避免前端轮询刷屏，普通 HTTP `200` 访问不会输出；模型抽取/规则回退、解析步骤、任务状态以及非 `200` 请求仍会保留。

## 三、启动本地前端

在菜单中选择“终端 → 运行任务 → 前端：启动开发服务”，然后打开：

<http://localhost:3000>

这个页面连接的是 VS Code 中运行的 Python 后端，适合学习代码、打断点和看日志。

## 四、直接使用 Docker 完整版

当前 Docker 版地址：

<http://localhost:8080>

如果以后容器停了，在 VS Code 终端执行：

```powershell
docker compose up --build -d
```

查看 Python Agent 日志：

```powershell
docker compose logs -f backend
```

停止容器：

```powershell
docker compose down
```

不要随便加 `-v`；`docker compose down -v` 会删除 SQLite 演示订单。

## 五、跑测试

选择“终端 → 运行任务 → 后端：运行测试”，或者执行：

```powershell
cd E:\agent_order_myself\backend
..\.venv\Scripts\python.exe -m pytest -q
```

当前基线是 `121 passed`。

也可以运行“终端 → 运行任务 → 评测：运行离线规则基线”，报告会写入 `backend/evaluation/results/`。

直接用 VS Code 运行时，TXT/CSV/XLSX 和文本 PDF 无需额外程序。图片与扫描 PDF 需要 Windows 已安装 Tesseract OCR，且 `tesseract --list-langs` 能看到 `chi_sim` 和 `eng`。Docker 版已内置这两个 OCR 语言环境。

## 六、让 AI 接着操作

在 VS Code 的 Codex 或 Claude Code 中打开本项目后，可以直接说：

> 请先读取 README.md、当前源码和测试，再继续修改。每一步先解释目标，再修改并验证；不要虚构业务规则，也不要在输出或源码中暴露 .env 密钥。

本地协作约束和执行记录不纳入公开仓库；新开聊天时可直接发送上面的通用提示。

## 七、建议从哪里读代码

按这个顺序最容易理解：

1. `backend/app/main.py`：API 入口。
2. `backend/app/workflow.py`：LangGraph、Checkpoint、中断恢复和日志。
3. `backend/app/parser.py`：离线规则解析。
4. `backend/app/llm_provider.py`：DeepSeek 结构化抽取。
5. `backend/app/retrieval.py`：ES 关键词+本地向量候选召回。
6. `backend/app/database.py`：SQLite、SKU 版本、幂等和纠错事务。
7. `frontend/src/App.tsx`：页面交互和文件上传。
8. `backend/evaluation/`：离线评测器与基线报告。
9. `backend/tests/`：最小流程的可执行说明。

## 八、两个网址的区别

| 地址 | 后端来源 | 用途 |
|---|---|---|
| <http://localhost:3000> | VS Code 中按 F5 启动的 Python | 学习、断点调试、实时改代码 |
| <http://localhost:8080> | Docker 容器中的 Python | 面试演示、验证完整部署 |
