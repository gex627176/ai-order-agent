import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import CatalogPanel from "./CatalogPanel";
import { api } from "./api";
import type { AgentSession, Draft, Order, Product, RecognitionTask } from "./types";

vi.mock("./api", () => {
  class ApiRequestError extends Error {
    constructor(message: string, readonly status: number) {
      super(message);
    }
  }
  return {
    ApiRequestError,
    api: {
    customers: vi.fn(), products: vi.fn(), orders: vi.fn(), order: vi.fn(),
    drafts: vi.fn(), draft: vi.fn(), workflow: vi.fn(), recognitionTasks: vi.fn(),
    metricsOverview: vi.fn(), modelMetrics: vi.fn(), metricFailures: vi.fn(),
    aliasCandidates: vi.fn(), businessMetrics: vi.fn(), updateDraft: vi.fn(),
    updateProduct: vi.fn(), createProduct: vi.fn(), importCatalog: vi.fn(),
    reviewAliasCandidate: vi.fn(), recognitionTask: vi.fn(), createRecognitionTask: vi.fn(),
    createFileRecognitionTask: vi.fn(), retryRecognitionTask: vi.fn(),
    cancelRecognitionTask: vi.fn(), recognize: vi.fn(), confirm: vi.fn(),
    subscribeRecognitionTask: vi.fn(),
    createAgentSession: vi.fn(), agentSession: vi.fn(),
    sendAgentMessage: vi.fn(), resumeAgentSession: vi.fn(),
    },
  };
});

const product: Product = {
  id: 1, sku: "VEG-001", name: "番茄", aliases: ["西红柿"],
  unit: "斤", unit_price: 5.2, active: true,
};

function makeDraft(id: string, items: Draft["items"] = []): Draft {
  return {
    id, customer_id: 1, source_text: `${id} 的订单`, status: "needs_review",
    items, warnings: [], trace: [], provider: "rules", site_id: 1, sku_version: 3,
    retrieval: [], preferences: [], input_type: "text", source_filename: "",
    input_confidence: null, input_warnings: [],
    created_at: "2026-08-31T08:00:00", updated_at: "2026-08-31T08:00:00",
  };
}

const validItem: Draft["items"][number] = {
  line_no: 1, raw_product_name: "番茄", product_id: 1, product_name: "番茄",
  quantity: 2, unit: "斤", unit_price: 5.2, note: "", matched: true,
};

const order: Order = {
  id: 9, order_no: "ORD-20260831-0009", draft_id: "draft-order", customer_id: 1,
  customer_name: "张三", source_text: "番茄2斤", total_amount: 10.4,
  status: "confirmed", site_id: 1, sku_version: 3,
  idempotency_key: "draft:draft-order", payload_md5: "abc",
  input_type: "text", source_filename: "", created_at: "2026-08-31T08:00:00",
  items: [{ line_no: 1, product_id: 1, product_name: "番茄", quantity: 2, unit: "斤", unit_price: 5.2, note: "", subtotal: 10.4 }],
};

const mockedApi = vi.mocked(api);

function makeTask(
  status: "pending" | "running" | "retrying" | "succeeded" | "failed" | "cancelled",
  draftId: string | null = null,
): RecognitionTask {
  return {
    id: "task-live", idempotency_key: "task-key", payload_md5: "hash", status,
    text: "番茄2斤", customer_id: 1, site_id: 1, input_type: "text",
    source_filename: "", draft_id: draftId, attempt_count: 1,
    input_confidence: null, input_warnings: [], max_attempts: 3,
    cancel_requested: status === "cancelled", error_type: status === "failed" ? "RuntimeError" : "",
    error_message: status === "failed" ? "识别暂时失败" : "",
    next_run_at: "2026-09-06T08:00:00Z", created_at: "2026-09-06T08:00:00Z",
    updated_at: "2026-09-06T08:00:01Z", started_at: null, finished_at: null,
  };
}

function makeAgentSession(): AgentSession {
  return {
    id: "agent-session", status: "active", site_id: 1, customer_id: 1,
    current_draft_id: null, current_order_id: null, pending_action: null,
    context_summary: "", summary_through_sequence: 0, last_message: "会话已创建",
    version: 1, last_event_sequence: 1, has_more_events: false,
    created_at: "2026-09-09T08:00:00Z", updated_at: "2026-09-09T08:00:00Z",
    events: [],
  };
}

function setDefaultApiMocks() {
  mockedApi.customers.mockResolvedValue([{ id: 1, name: "张三", contact: "13800000000" }]);
  mockedApi.products.mockResolvedValue([product]);
  mockedApi.orders.mockResolvedValue([]);
  mockedApi.drafts.mockResolvedValue([]);
  mockedApi.recognitionTasks.mockResolvedValue([]);
  mockedApi.metricsOverview.mockResolvedValue({
    total_calls: 0, success_count: 0, fallback_count: 0, failure_count: 0,
    success_rate: 0, fallback_rate: 0, average_duration_ms: 0, p95_duration_ms: 0,
    prompt_tokens: 0, completion_tokens: 0, total_tokens: 0, estimated_cost: 0,
  });
  mockedApi.modelMetrics.mockResolvedValue([]);
  mockedApi.metricFailures.mockResolvedValue([]);
  mockedApi.aliasCandidates.mockResolvedValue([]);
  mockedApi.businessMetrics.mockResolvedValue({
    total_tasks: 0, completed_tasks: 0, task_success_rate: 0, confirmed_orders: 0,
    total_recognized_items: 0, auto_matched_items: 0, auto_match_rate: 0,
    unmatched_items: 0, unmatched_rate: 0, corrected_drafts: 0, correction_rate: 0,
    pending_alias_candidates: 0, approved_alias_candidates: 0, catalog_products: 1,
    active_products: 1, current_sku_version: 3, average_task_duration_ms: 0, p95_task_duration_ms: 0,
  });
  mockedApi.workflow.mockResolvedValue({
    thread_id: "draft", checkpoint_exists: true, waiting_for_review: true, next_nodes: [],
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  setDefaultApiMocks();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("前端韧性与人工审核", () => {
  it("模型 P95 使用易读单位并提示小样本语义", async () => {
    mockedApi.metricsOverview.mockResolvedValue({
      total_calls: 19, success_count: 18, fallback_count: 1, failure_count: 0,
      success_rate: 94.74, fallback_rate: 5.26, average_duration_ms: 5149.37,
      p95_duration_ms: 20875, prompt_tokens: 4202, completion_tokens: 4852,
      total_tokens: 9054, estimated_cost: 0,
    });

    render(<App />);

    const metric = (await screen.findByText("P95 时延")).closest("article");
    expect(metric?.textContent).toContain("20.9 s");
    expect(metric?.textContent).toContain("19 次样本 · 小样本 P95 等于最慢值");
  });

  it.each([
    [0, 0, "0 ms", "暂无历史样本"],
    [20, 9999, "9,999 ms", "20 次历史样本"],
    [20, 10000, "10.0 s", "20 次历史样本"],
  ])("模型 P95 正确处理 %i 次样本和 %i 毫秒边界", async (
    totalCalls, durationMs, displayed, context,
  ) => {
    mockedApi.metricsOverview.mockResolvedValue({
      total_calls: totalCalls, success_count: totalCalls, fallback_count: 0,
      failure_count: 0, success_rate: totalCalls ? 100 : 0, fallback_rate: 0,
      average_duration_ms: durationMs, p95_duration_ms: durationMs,
      prompt_tokens: 0, completion_tokens: 0, total_tokens: 0, estimated_cost: 0,
    });

    render(<App />);

    const metric = (await screen.findByText("P95 时延")).closest("article");
    expect(metric?.textContent).toContain(displayed);
    expect(metric?.textContent).toContain(context);
  });

  it("可以切换到独立 Agent 对话工作区", async () => {
    render(<App />);

    fireEvent.click(screen.getByRole("button", { name: /Agent 对话/ }));

    expect(await screen.findByRole("button", { name: "创建 Agent 会话" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "开始智能识别 →" })).toBeNull();
  });

  it("切换录单模式不会卸载正在使用的 Agent 会话", async () => {
    mockedApi.createAgentSession.mockResolvedValue(makeAgentSession());
    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: /Agent 对话/ }));
    fireEvent.click(await screen.findByRole("button", { name: "创建 Agent 会话" }));
    await screen.findByText("会话已创建");

    fireEvent.click(screen.getByRole("button", { name: /标准录单/ }));
    fireEvent.click(screen.getByRole("button", { name: /Agent 对话/ }));

    expect(screen.getByText("会话已创建")).toBeTruthy();
    expect(mockedApi.createAgentSession).toHaveBeenCalledTimes(1);
  });

  it("非核心指标失败时仍加载客户和录单入口", async () => {
    mockedApi.metricsOverview.mockRejectedValue(new Error("metrics offline"));
    mockedApi.modelMetrics.mockRejectedValue(new Error("metrics offline"));
    mockedApi.metricFailures.mockRejectedValue(new Error("metrics offline"));

    render(<App />);

    expect(await screen.findByRole("option", { name: /张三/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /开始智能识别/ })).toBeEnabled();
    expect(await screen.findByText(/模型监控暂时不可用，不影响客户、商品和草稿录单/)).toBeInTheDocument();
  });

  it("空结果可人工补录，并阻止带未保存行的草稿切换", async () => {
    const first = makeDraft("draft-one");
    const second = makeDraft("draft-two");
    mockedApi.drafts.mockResolvedValue([first, second]);
    mockedApi.draft.mockImplementation(async (id) => id === first.id ? first : second);

    render(<App />);
    await screen.findByText(/未识别出商品，请点击“新增商品行”人工补录/);
    fireEvent.click(screen.getByRole("button", { name: /新增商品行/ }));

    expect(screen.getByRole("combobox", { name: "第 1 行商品" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /draft-two 的订单/ }));
    expect(await screen.findByText("请先补全或删除未完成的商品行，再切换草稿")).toBeInTheDocument();
    expect(mockedApi.draft).toHaveBeenCalledTimes(1);
  });

  it("单位可以从目录选择，也可以新建自定义单位", async () => {
    const draft = makeDraft("draft-unit", [validItem]);
    mockedApi.products.mockResolvedValue([
      product,
      { ...product, id: 2, sku: "FRUIT-002", name: "苹果", unit: "箱" },
    ]);
    mockedApi.drafts.mockResolvedValue([draft]);
    mockedApi.draft.mockResolvedValue(draft);
    mockedApi.updateDraft.mockImplementation(async (value) => value);

    render(<App />);

    const unitSelect = await screen.findByRole("combobox", { name: "第 1 行单位" });
    expect(unitSelect).toHaveValue("斤");
    expect(screen.getByRole("option", { name: "箱" })).toBeInTheDocument();
    fireEvent.change(unitSelect, { target: { value: "箱" } });
    expect(unitSelect).toHaveValue("箱");

    vi.useFakeTimers();
    fireEvent.change(unitSelect, { target: { value: "" } });
    fireEvent.change(
      screen.getByRole("textbox", { name: "第 1 行自定义单位" }),
      { target: { value: "斤" } },
    );
    const customUnit = screen.getByRole("textbox", { name: "第 1 行自定义单位" });
    fireEvent.change(customUnit, { target: { value: "斤装" } });
    await act(async () => { vi.advanceTimersByTime(800); await Promise.resolve(); });

    expect(customUnit).toHaveValue("斤装");
    expect(mockedApi.updateDraft).toHaveBeenCalledWith(expect.objectContaining({
      items: [expect.objectContaining({ unit: "斤装" })],
    }));
    expect(screen.queryByText("第 1 行请填写单位")).not.toBeInTheDocument();
  });

  it("删除上一行后不会把自定义单位状态带给下一行", async () => {
    const products = [
      product,
      { ...product, id: 2, sku: "FRUIT-002", name: "苹果", unit: "箱" },
    ];
    const secondItem = {
      ...validItem, line_no: 2, product_id: 2, product_name: "苹果",
      raw_product_name: "苹果", unit: "箱",
    };
    const draft = makeDraft("draft-unit-remove", [validItem, secondItem]);
    mockedApi.products.mockResolvedValue(products);
    mockedApi.drafts.mockResolvedValue([draft]);
    mockedApi.draft.mockResolvedValue(draft);

    render(<App />);

    fireEvent.change(
      await screen.findByRole("combobox", { name: "第 1 行单位" }),
      { target: { value: "" } },
    );
    fireEvent.change(
      screen.getByRole("textbox", { name: "第 1 行自定义单位" }),
      { target: { value: "斤" } },
    );
    fireEvent.click(screen.getAllByRole("button", { name: "删除" })[0]);

    expect(screen.getByRole("combobox", { name: "第 1 行单位" })).toHaveValue("箱");
    expect(screen.queryByRole("textbox", { name: "第 1 行自定义单位" })).not.toBeInTheDocument();
  });

  it("草稿防抖保存失败后可以手动重试", async () => {
    const draft = makeDraft("draft-save", [validItem]);
    mockedApi.drafts.mockResolvedValue([draft]);
    mockedApi.draft.mockResolvedValue(draft);
    mockedApi.updateDraft
      .mockRejectedValueOnce(new Error("网络暂时不可用"))
      .mockResolvedValueOnce({ ...draft, items: [{ ...validItem, quantity: 3 }] });

    render(<App />);
    const quantity = await screen.findByRole("spinbutton", { name: "第 1 行数量" });
    vi.useFakeTimers();
    fireEvent.change(quantity, { target: { value: "3" } });
    await act(async () => { vi.advanceTimersByTime(800); await Promise.resolve(); });
    expect(screen.getByText("网络暂时不可用")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "重试保存" }));
    await act(async () => { vi.advanceTimersByTime(800); await Promise.resolve(); });
    expect(screen.getByText("草稿已保存")).toBeInTheDocument();
    expect(mockedApi.updateDraft).toHaveBeenCalledTimes(2);
  });

  it("目录保存成功但 ES 同步失败时显示明确提示", async () => {
    const onMessage = vi.fn();
    mockedApi.updateProduct.mockResolvedValue({
      product, version: { site_id: 1, version: 4, status: "active", content_md5: "hash", created_at: "2026-08-31" },
      indexed_count: 0, backend: "elasticsearch_hybrid", index_synced: false,
      index_error: "目录已保存，但 Elasticsearch 同步失败",
    });

    render(<CatalogPanel products={[product]} onProductsChanged={vi.fn()} onMessage={onMessage} />);
    fireEvent.change(screen.getByDisplayValue("番茄"), { target: { value: "精品番茄" } });
    fireEvent.click(screen.getByRole("button", { name: "保存发布" }));

    await waitFor(() => expect(onMessage).toHaveBeenCalledWith(
      "目录已保存，但 Elasticsearch 同步失败（SKU v4）", true,
    ));
  });

  it("从最近订单打开完整详情和来源证据", async () => {
    mockedApi.orders.mockResolvedValue([order]);
    mockedApi.order.mockResolvedValue(order);
    mockedApi.draft.mockResolvedValue({ ...makeDraft(order.draft_id, [validItem]), retrieval: [{ product_id: 1 }] });

    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: /张三.*ORD-20260831-0009/ }));

    const dialog = await screen.findByRole("dialog", { name: "ORD-20260831-0009" });
    expect(dialog).toHaveTextContent("站点 / 目录");
    expect(dialog).toHaveTextContent("1 / v3");
    expect(dialog).toHaveTextContent("番茄2斤");
    expect(dialog).toHaveTextContent("1 条本地目录候选记录");
  });

  it("通过 SSE 接收成功终态并加载草稿，正常路径不轮询单任务 GET", async () => {
    const running = makeTask("running");
    const succeeded = makeTask("succeeded", "task-live");
    const completedDraft = makeDraft("task-live", [validItem]);
    let onTask: ((task: RecognitionTask) => void) | undefined;
    const close = vi.fn();
    mockedApi.recognitionTasks
      .mockResolvedValueOnce([running])
      .mockResolvedValueOnce([succeeded]);
    mockedApi.draft.mockResolvedValue(completedDraft);
    mockedApi.drafts
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce([completedDraft]);
    mockedApi.subscribeRecognitionTask.mockImplementation((_id, next) => {
      onTask = next;
      return { close };
    });

    render(<App />);
    await waitFor(() => expect(mockedApi.subscribeRecognitionTask).toHaveBeenCalledTimes(1));
    await act(async () => { onTask?.(succeeded); });

    expect(await screen.findByText("识别完成，请核对草稿后再创建订单")).toBeInTheDocument();
    expect(screen.getByDisplayValue("task-live 的订单")).toBeInTheDocument();
    expect(mockedApi.recognitionTask).not.toHaveBeenCalled();
    expect(close).toHaveBeenCalledTimes(1);
  });

  it("SSE 断线先 GET 对账、有限重连，并在卸载时关闭连接", async () => {
    const running = makeTask("running");
    let onError: (() => void) | undefined;
    const closes: ReturnType<typeof vi.fn>[] = [];
    mockedApi.recognitionTasks.mockResolvedValue([running]);
    mockedApi.recognitionTask.mockResolvedValue(running);
    mockedApi.subscribeRecognitionTask.mockImplementation((_id, _next, errorHandler) => {
      onError = errorHandler;
      const close = vi.fn();
      closes.push(close);
      return { close };
    });

    const view = render(<App />);
    await waitFor(() => expect(mockedApi.subscribeRecognitionTask).toHaveBeenCalledTimes(1));
    vi.useFakeTimers();
    await act(async () => { onError?.(); await Promise.resolve(); });
    expect(mockedApi.recognitionTask).toHaveBeenCalledWith("task-live");
    await act(async () => { vi.advanceTimersByTime(500); await Promise.resolve(); });
    expect(mockedApi.subscribeRecognitionTask).toHaveBeenCalledTimes(2);

    view.unmount();
    expect(closes[1]).toHaveBeenCalledTimes(1);
  });

  it("GET 对账短暂失败后仍按有限预算重连 SSE", async () => {
    const running = makeTask("running");
    let onError: (() => void) | undefined;
    mockedApi.recognitionTasks.mockResolvedValue([running]);
    mockedApi.recognitionTask.mockRejectedValue(new Error("offline"));
    mockedApi.subscribeRecognitionTask.mockImplementation((_id, _next, errorHandler) => {
      onError = errorHandler;
      return { close: vi.fn() };
    });

    render(<App />);
    await waitFor(() => expect(mockedApi.subscribeRecognitionTask).toHaveBeenCalledTimes(1));
    vi.useFakeTimers();
    await act(async () => { onError?.(); await Promise.resolve(); });
    expect(mockedApi.recognitionTask).toHaveBeenCalledTimes(1);
    await act(async () => { vi.advanceTimersByTime(500); await Promise.resolve(); });
    expect(mockedApi.subscribeRecognitionTask).toHaveBeenCalledTimes(2);
  });

  it("终态事件后紧接 EOF error 不会重复 GET 或加载草稿", async () => {
    const running = makeTask("running");
    const succeeded = makeTask("succeeded", "task-live");
    const completedDraft = makeDraft("task-live", [validItem]);
    let onTask: ((task: RecognitionTask) => void) | undefined;
    let onError: (() => void) | undefined;
    let resolveDraft!: (value: Draft) => void;
    mockedApi.recognitionTasks
      .mockResolvedValueOnce([running])
      .mockResolvedValueOnce([succeeded]);
    mockedApi.draft.mockReturnValue(new Promise((resolve) => { resolveDraft = resolve; }));
    mockedApi.drafts
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce([completedDraft]);
    mockedApi.subscribeRecognitionTask.mockImplementation((_id, next, errorHandler) => {
      onTask = next; onError = errorHandler;
      return { close: vi.fn() };
    });

    render(<App />);
    await waitFor(() => expect(mockedApi.subscribeRecognitionTask).toHaveBeenCalledTimes(1));
    act(() => { onTask?.(succeeded); onError?.(); });
    expect(mockedApi.recognitionTask).not.toHaveBeenCalled();
    expect(mockedApi.draft).toHaveBeenCalledTimes(1);

    await act(async () => { resolveDraft(completedDraft); await Promise.resolve(); });
    expect(await screen.findByText("识别完成，请核对草稿后再创建订单")).toBeInTheDocument();
    expect(mockedApi.draft).toHaveBeenCalledTimes(1);
  });

  it("终态 hydration 期间卸载后不再发起后续任务列表请求", async () => {
    const running = makeTask("running");
    const succeeded = makeTask("succeeded", "task-live");
    const completedDraft = makeDraft("task-live", [validItem]);
    let onTask: ((task: RecognitionTask) => void) | undefined;
    let resolveDraft!: (value: Draft) => void;
    mockedApi.recognitionTasks.mockResolvedValue([running]);
    mockedApi.draft.mockReturnValue(new Promise((resolve) => { resolveDraft = resolve; }));
    mockedApi.drafts
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce([completedDraft]);
    mockedApi.subscribeRecognitionTask.mockImplementation((_id, next) => {
      onTask = next;
      return { close: vi.fn() };
    });

    const view = render(<App />);
    await waitFor(() => expect(mockedApi.subscribeRecognitionTask).toHaveBeenCalledTimes(1));
    act(() => { onTask?.(succeeded); });
    view.unmount();
    await act(async () => { resolveDraft(completedDraft); await Promise.resolve(); });

    expect(mockedApi.recognitionTasks).toHaveBeenCalledTimes(1);
  });

  it("首屏任务列表在卸载后才返回时不会创建 SSE 订阅", async () => {
    const running = makeTask("running");
    let resolveTasks!: (tasks: RecognitionTask[]) => void;
    mockedApi.recognitionTasks.mockReturnValue(
      new Promise((resolve) => { resolveTasks = resolve; }),
    );

    const view = render(<App />);
    view.unmount();
    await act(async () => { resolveTasks([running]); await Promise.resolve(); });

    expect(mockedApi.subscribeRecognitionTask).not.toHaveBeenCalled();
  });

  it("创建任务请求在卸载后才返回时不会创建 SSE 订阅", async () => {
    const pending = makeTask("pending");
    let resolveTask!: (task: RecognitionTask) => void;
    mockedApi.createRecognitionTask.mockReturnValue(
      new Promise((resolve) => { resolveTask = resolve; }),
    );

    const view = render(<App />);
    const button = await screen.findByRole("button", { name: /开始智能识别/ });
    fireEvent.click(button);
    view.unmount();
    await act(async () => { resolveTask(pending); await Promise.resolve(); });

    expect(mockedApi.subscribeRecognitionTask).not.toHaveBeenCalled();
  });

  it.each([
    ["failed", "识别暂时失败"],
    ["cancelled", "识别任务已取消，没有创建订单"],
  ] as const)("SSE 收到 %s 终态后关闭订阅并展示结果", async (status, message) => {
    const running = makeTask("running");
    const terminal = makeTask(status);
    let onTask: ((task: RecognitionTask) => void) | undefined;
    const close = vi.fn();
    mockedApi.recognitionTasks
      .mockResolvedValueOnce([running])
      .mockResolvedValueOnce([terminal]);
    mockedApi.subscribeRecognitionTask.mockImplementation((_id, next) => {
      onTask = next;
      return { close };
    });

    render(<App />);
    await waitFor(() => expect(mockedApi.subscribeRecognitionTask).toHaveBeenCalledTimes(1));
    await act(async () => { onTask?.(terminal); });

    expect(await screen.findByText(message)).toBeInTheDocument();
    expect(close).toHaveBeenCalledTimes(1);
  });
});
