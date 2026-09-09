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
