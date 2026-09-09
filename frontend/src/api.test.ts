import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "./api";

class MockEventSource {
  static instances: MockEventSource[] = [];
  readonly url: string;
  onerror: (() => void) | null = null;
  close = vi.fn();
  private listeners = new Map<string, (event: MessageEvent<string>) => void>();

  constructor(url: string | URL) {
    this.url = String(url);
    MockEventSource.instances.push(this);
  }

  addEventListener(name: string, listener: EventListenerOrEventListenerObject) {
    this.listeners.set(name, listener as (event: MessageEvent<string>) => void);
  }

  emit(name: string, data: string) {
    this.listeners.get(name)?.({ data } as MessageEvent<string>);
  }
}

afterEach(() => {
  MockEventSource.instances = [];
  vi.unstubAllGlobals();
});

describe("识别任务 SSE API", () => {
  it("订阅命名 task 事件并支持关闭", () => {
    vi.stubGlobal("EventSource", MockEventSource);
    const onTask = vi.fn();
    const onError = vi.fn();
    const subscription = api.subscribeRecognitionTask("task/a", onTask, onError);
    const source = MockEventSource.instances[0];

    expect(source.url).toBe("/api/recognition-tasks/task%2Fa/events");
    source.emit("task", JSON.stringify({ id: "task/a", status: "running" }));
    expect(onTask).toHaveBeenCalledWith({ id: "task/a", status: "running" });
    expect(onError).not.toHaveBeenCalled();

    subscription.close();
    expect(source.close).toHaveBeenCalledTimes(1);
  });

  it("畸形事件交给断线恢复处理", () => {
    vi.stubGlobal("EventSource", MockEventSource);
    const onError = vi.fn();
    api.subscribeRecognitionTask("task-1", vi.fn(), onError);

    MockEventSource.instances[0].emit("task", "not-json");

    expect(onError).toHaveBeenCalledTimes(1);
  });
});

describe("Agent Harness API", () => {
  it("按独立消息和审批入口发送幂等请求", async () => {
    const payload = { id: "session/a", status: "active", events: [] };
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => payload,
    });
    vi.stubGlobal("fetch", fetchMock);

    await api.createAgentSession(null);
    await api.agentSession("session/a");
    await api.sendAgentMessage("session/a", "番茄2斤", "message-key");
    await api.resumeAgentSession("session/a", true, "resume-key");

    expect(fetchMock).toHaveBeenNthCalledWith(1, "/api/agent/sessions", expect.objectContaining({
      method: "POST", body: JSON.stringify({ site_id: 1, customer_id: null }),
    }));
    expect(fetchMock).toHaveBeenNthCalledWith(2, "/api/agent/sessions/session%2Fa", expect.any(Object));
    expect(fetchMock).toHaveBeenNthCalledWith(3, "/api/agent/sessions/session%2Fa/messages", expect.objectContaining({
      method: "POST", headers: expect.objectContaining({ "Idempotency-Key": "message-key" }),
      body: JSON.stringify({ content: "番茄2斤" }),
    }));
    expect(fetchMock).toHaveBeenNthCalledWith(4, "/api/agent/sessions/session%2Fa/resume", expect.objectContaining({
      method: "POST", headers: expect.objectContaining({ "Idempotency-Key": "resume-key" }),
      body: JSON.stringify({ approved: true }),
    }));
  });
});
