import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import AgentWorkspace from "./AgentWorkspace";
import { ApiRequestError, api } from "./api";
import type { AgentSession } from "./types";

vi.mock("./api", () => ({
  ApiRequestError: class ApiRequestError extends Error {
    constructor(message: string, readonly status: number) { super(message); }
  },
  api: {
    createAgentSession: vi.fn(), agentSession: vi.fn(),
    sendAgentMessage: vi.fn(), resumeAgentSession: vi.fn(),
  },
}));

const mockedApi = vi.mocked(api);

function session(overrides: Partial<AgentSession> = {}): AgentSession {
  return {
    id: "session-1", status: "active", site_id: 1, customer_id: 1,
    current_draft_id: null, current_order_id: null, pending_action: null,
    context_summary: "", summary_through_sequence: 0, last_message: "会话已创建",
    version: 1, last_event_sequence: 1, has_more_events: false,
    created_at: "2026-09-09T08:00:00Z", updated_at: "2026-09-09T08:00:00Z",
    events: [{
      id: 1, sequence: 1, type: "session", actor: "harness", name: "session_created",
      status: "completed", summary: "Agent 会话已创建", payload: {},
      created_at: "2026-09-09T08:00:00Z",
    }],
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
});

afterEach(cleanup);

describe("Agent Harness 交互工作区", () => {
  it("创建会话并发送多轮消息", async () => {
    mockedApi.createAgentSession.mockResolvedValue(session());
    mockedApi.sendAgentMessage.mockResolvedValue(session({
      status: "waiting_input",
      last_message: "请告诉我客户名称。",
      pending_action: { name: "provide_customer", arguments: {}, permission: "read" },
      events: [{
        id: 2, sequence: 2, type: "user", actor: "user", name: "message_received",
        status: "completed", summary: "帮我录番茄5斤", payload: { content: "帮我录番茄5斤" },
        created_at: "2026-09-09T08:01:00Z",
      }, {
        id: 3, sequence: 3, type: "assistant", actor: "main_agent", name: "message_sent",
        status: "completed", summary: "请告诉我客户名称。", payload: { content: "请告诉我客户名称。" },
        created_at: "2026-09-09T08:01:01Z",
      }],
    }));

    render(<AgentWorkspace customers={[{ id: 1, name: "惠民餐厅", contact: "" }]} />);
    fireEvent.click(screen.getByRole("button", { name: "创建 Agent 会话" }));
    await screen.findByText("会话已创建");

    fireEvent.change(screen.getByLabelText("发送给 Agent"), {
      target: { value: "帮我录番茄5斤" },
    });
    fireEvent.click(screen.getByRole("button", { name: "发送消息" }));

    await screen.findAllByText("请告诉我客户名称。");
    expect(mockedApi.sendAgentMessage).toHaveBeenCalledWith(
      "session-1", "帮我录番茄5斤", expect.stringContaining("message:"),
    );
    expect(screen.getAllByText("帮我录番茄5斤")).toHaveLength(2);
  });

  it("按事件顺序展示每轮用户消息和 Agent 回复", async () => {
    localStorage.setItem("agent-harness-session-id", "restored-session");
    mockedApi.agentSession.mockResolvedValue(session({
      id: "restored-session", status: "waiting_input", last_message: "请核对商品。",
      events: [
        { id: 2, sequence: 2, type: "user", actor: "user", name: "message_received", status: "completed", summary: "帮我录10斤大土豆", payload: {}, created_at: "2026-09-09T08:01:00Z" },
        { id: 3, sequence: 3, type: "assistant", actor: "main_agent", name: "message_sent", status: "completed", summary: "请先告诉我客户。", payload: {}, created_at: "2026-09-09T08:01:01Z" },
        { id: 4, sequence: 4, type: "user", actor: "user", name: "message_received", status: "completed", summary: "惠民餐厅", payload: {}, created_at: "2026-09-09T08:02:00Z" },
        { id: 5, sequence: 5, type: "assistant", actor: "main_agent", name: "message_sent", status: "completed", summary: "请核对商品。", payload: {}, created_at: "2026-09-09T08:02:01Z" },
      ],
    }));

    render(<AgentWorkspace customers={[]} />);

    await screen.findAllByText("帮我录10斤大土豆");
    const messages = document.querySelector(".agent-messages")!.querySelectorAll("article p");
    expect([...messages].map((item) => item.textContent)).toEqual([
      "帮我录10斤大土豆", "请先告诉我客户。", "惠民餐厅", "请核对商品。",
    ]);
  });

  it("长消息使用完整事件正文且不重复追加最新回复", async () => {
    const fullUser = `用户${"甲".repeat(520)}`;
    const fullReply = `回复${"乙".repeat(520)}`;
    localStorage.setItem("agent-harness-session-id", "long-session");
    mockedApi.agentSession.mockResolvedValue(session({
      id: "long-session", last_message: fullReply,
      events: [
        { id: 2, sequence: 2, type: "user", actor: "user", name: "message_received", status: "completed", summary: fullUser.slice(0, 500), payload: { content: fullUser }, created_at: "2026-09-09T08:01:00Z" },
        { id: 3, sequence: 3, type: "assistant", actor: "main_agent", name: "message_sent", status: "completed", summary: fullReply.slice(0, 500), payload: { content: fullReply }, created_at: "2026-09-09T08:01:01Z" },
      ],
    }));

    render(<AgentWorkspace customers={[]} />);

    expect(await screen.findByText(fullUser)).toBeTruthy();
    expect(screen.getAllByText(fullReply)).toHaveLength(1);
    const transcript = [...document.querySelectorAll(".agent-messages article p")]
      .map((item) => item.textContent);
    expect(transcript).toEqual([fullUser, fullReply]);
  });

  it("仅在确认动作等待审批时提供批准与拒绝", async () => {
    mockedApi.createAgentSession.mockResolvedValue(session({
      status: "waiting_approval", current_draft_id: "draft-1",
      last_message: "草稿已通过校验，等待人工批准。",
      pending_action: {
        name: "confirm_order", arguments: { draft_fingerprint: "hash" },
        permission: "transaction_write",
      },
    }));
    mockedApi.resumeAgentSession.mockResolvedValue(session({
      status: "completed", current_draft_id: "draft-1", current_order_id: 9,
      last_message: "订单 ORD-9 已创建。", pending_action: null,
    }));

    render(<AgentWorkspace customers={[]} />);
    fireEvent.click(screen.getByRole("button", { name: "创建 Agent 会话" }));
    await screen.findByRole("button", { name: "批准并建单" });
    expect(screen.getByRole("button", { name: "拒绝建单" })).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "批准并建单" }));
    await screen.findByText("订单 ORD-9 已创建。");
    expect(mockedApi.resumeAgentSession).toHaveBeenCalledWith(
      "session-1", true, expect.stringContaining("resume:"),
    );
    expect(screen.queryByRole("button", { name: "批准并建单" })).toBeNull();
  });

  it("刷新后恢复本地保存的会话", async () => {
    localStorage.setItem("agent-harness-session-id", "restored-session");
    mockedApi.agentSession.mockResolvedValue(session({
      id: "restored-session", status: "waiting_input", last_message: "请继续补充。",
    }));

    render(<AgentWorkspace customers={[]} />);

    await waitFor(() => expect(mockedApi.agentSession).toHaveBeenCalledWith("restored-session"));
    expect(await screen.findByText("请继续补充。")).toBeTruthy();
  });

  it("审批状态冲突后自动读取服务端最新状态", async () => {
    mockedApi.createAgentSession.mockResolvedValue(session({
      status: "waiting_approval", current_draft_id: "draft-1",
      pending_action: {
        name: "confirm_order", arguments: { draft_fingerprint: "old" },
        permission: "transaction_write",
      },
    }));
    mockedApi.resumeAgentSession.mockRejectedValue(
      new ApiRequestError("草稿内容已变化", 409),
    );
    mockedApi.agentSession.mockResolvedValue(session({
      status: "waiting_input", current_draft_id: "draft-1",
      last_message: "草稿发生变化，请重新检查。",
      pending_action: { name: "review_sku", arguments: {}, permission: "draft_write" },
      version: 2,
    }));

    render(<AgentWorkspace customers={[]} />);
    fireEvent.click(screen.getByRole("button", { name: "创建 Agent 会话" }));
    await screen.findByRole("button", { name: "批准并建单" });
    fireEvent.click(screen.getByRole("button", { name: "批准并建单" }));

    expect(await screen.findByText("请人工核对商品")).toBeTruthy();
    expect(screen.getByText(/会话状态已刷新/)).toBeTruthy();
    expect(screen.getByLabelText("发送给 Agent")).not.toBeDisabled();
    expect(mockedApi.agentSession).toHaveBeenCalledWith("session-1");
  });

  it("网络结果不明时相同逻辑消息复用幂等键", async () => {
    mockedApi.createAgentSession.mockResolvedValue(session());
    mockedApi.sendAgentMessage
      .mockRejectedValueOnce(new Error("网络中断"))
      .mockResolvedValueOnce(session({ last_message: "已收到。", version: 2 }));

    render(<AgentWorkspace customers={[]} />);
    fireEvent.click(screen.getByRole("button", { name: "创建 Agent 会话" }));
    await screen.findByText("会话已创建");
    fireEvent.change(screen.getByLabelText("发送给 Agent"), { target: { value: "番茄2斤" } });
    fireEvent.click(screen.getByRole("button", { name: "发送消息" }));
    await screen.findByText("网络中断");
    fireEvent.click(screen.getByRole("button", { name: "发送消息" }));
    await screen.findByText("已收到。");

    const firstKey = mockedApi.sendAgentMessage.mock.calls[0][2];
    const secondKey = mockedApi.sendAgentMessage.mock.calls[1][2];
    expect(firstKey).toBe(secondKey);
    expect(firstKey).toMatch(/^agent-ui:message:[0-9a-f]{64}$/);
  });

  it("拒绝审批调用独立 resume 且卸载后不触发完成回调", async () => {
    let resolveResume!: (value: AgentSession) => void;
    const pendingResume = new Promise<AgentSession>((resolve) => { resolveResume = resolve; });
    mockedApi.createAgentSession.mockResolvedValue(session({
      status: "waiting_approval",
      pending_action: {
        name: "confirm_order", arguments: {}, permission: "transaction_write",
      },
    }));
    mockedApi.resumeAgentSession.mockReturnValue(pendingResume);
    const onOrderCreated = vi.fn();
    const view = render(<AgentWorkspace customers={[]} onOrderCreated={onOrderCreated} />);
    fireEvent.click(screen.getByRole("button", { name: "创建 Agent 会话" }));
    await screen.findByRole("button", { name: "拒绝建单" });
    fireEvent.click(screen.getByRole("button", { name: "拒绝建单" }));
    await waitFor(() => expect(mockedApi.resumeAgentSession).toHaveBeenCalledWith(
      "session-1", false, expect.any(String),
    ));
    view.unmount();
    resolveResume(session({ status: "completed", current_order_id: 10 }));
    await pendingResume;

    expect(onOrderCreated).not.toHaveBeenCalled();
  });
});
