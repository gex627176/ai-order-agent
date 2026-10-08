import { useEffect, useMemo, useRef, useState } from "react";
import { ApiRequestError, api } from "./api";
import AgentWorkspace from "./AgentWorkspace";
import CatalogPanel from "./CatalogPanel";
import OrderDetailsDrawer from "./OrderDetailsDrawer";
import type {
  Customer, Draft, DraftItem, LlmFailureRecord, MetricsOverview, ModelMetrics,
  Order, Product, RecognitionTask, WorkflowCheckpoint,
} from "./types";

const EXAMPLE = "番茄5斤，胡萝卜3公斤切丁，苹果2箱";
const AUTO_SAVE_DELAY_MS = 800;
const SSE_RECONNECT_LIMIT = 3;
const SSE_RECONNECT_DELAY_MS = 500;
const FALLBACK_POLL_INTERVAL_MS = 3000;
const FALLBACK_POLL_LIMIT = 40;
const CUSTOM_UNIT_VALUE = "";
type DraftSaveStatus = "idle" | "dirty" | "saving" | "saved" | "error";
type WorkspaceMode = "classic" | "agent";
const UNSAVED_DRAFT_STATES: DraftSaveStatus[] = ["dirty", "saving", "error"];

function displayDuration(durationMs: number): { value: string; unit: string } {
  if (durationMs >= 10_000) {
    return { value: (durationMs / 1000).toFixed(1), unit: "s" };
  }
  return { value: durationMs.toLocaleString(), unit: "ms" };
}

function p95SampleContext(totalCalls: number): string {
  if (totalCalls === 0) return "暂无历史样本";
  if (totalCalls < 20) return `${totalCalls} 次样本 · 小样本 P95 等于最慢值`;
  return `${totalCalls} 次历史样本`;
}

export default function App() {
  const recognizeLock = useRef(false);
  const editRevision = useRef(0);
  const mountedRef = useRef(true);
  const lifecycleGenerationRef = useRef(0);
  const taskMonitorRef = useRef<{ taskId: string; close: () => void } | null>(null);
  const [customers, setCustomers] = useState<Customer[]>([]);
  const [products, setProducts] = useState<Product[]>([]);
  const [orders, setOrders] = useState<Order[]>([]);
  const [drafts, setDrafts] = useState<Draft[]>([]);
  const [tasks, setTasks] = useState<RecognitionTask[]>([]);
  const [metrics, setMetrics] = useState<MetricsOverview | null>(null);
  const [modelMetrics, setModelMetrics] = useState<ModelMetrics[]>([]);
  const [metricFailures, setMetricFailures] = useState<LlmFailureRecord[]>([]);
  const [metricsError, setMetricsError] = useState("");
  const [customerId, setCustomerId] = useState<number | null>(1);
  const [text, setText] = useState(EXAMPLE);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [checkpoint, setCheckpoint] = useState<WorkflowCheckpoint | null>(null);
  const [busy, setBusy] = useState(false);
  const [recognizingTaskId, setRecognizingTaskId] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [draftSaveStatus, setDraftSaveStatus] = useState<DraftSaveStatus>("idle");
  const [draftSaveError, setDraftSaveError] = useState("");
  const [customUnitEditors, setCustomUnitEditors] = useState<Set<string>>(
    () => new Set(),
  );
  const [selectedOrder, setSelectedOrder] = useState<Order | null>(null);
  const [selectedOrderDraft, setSelectedOrderDraft] = useState<Draft | null>(null);
  const [orderDetailLoading, setOrderDetailLoading] = useState(false);
  const [orderDetailError, setOrderDetailError] = useState("");
  const [workspaceMode, setWorkspaceMode] = useState<WorkspaceMode>("classic");
  const p95Display = displayDuration(metrics?.p95_duration_ms ?? 0);

  const total = useMemo(
    () => draft?.items.reduce((sum, item) => sum + item.quantity * item.unit_price, 0) ?? 0,
    [draft],
  );
  const unitOptions = useMemo(
    () => [...new Set(products.map((product) => product.unit.trim()).filter(Boolean))]
      .sort((left, right) => left.localeCompare(right, "zh-CN")),
    [products],
  );
  const draftIssue = useMemo(() => {
    if (!draft || draft.status !== "needs_review") return "";
    if (!draft.items.length) return "请先新增一条商品明细";
    const invalidItem = draft.items.find((item) =>
      item.product_id === null
      || !Number.isFinite(item.quantity) || item.quantity <= 0
      || !item.unit.trim()
      || !Number.isFinite(item.unit_price) || item.unit_price < 0
    );
    if (!invalidItem) return "";
    if (invalidItem.product_id === null) return `第 ${invalidItem.line_no} 行请选择商品`;
    if (!Number.isFinite(invalidItem.quantity) || invalidItem.quantity <= 0) return `第 ${invalidItem.line_no} 行数量必须大于 0`;
    if (!invalidItem.unit.trim()) return `第 ${invalidItem.line_no} 行请填写单位`;
    return `第 ${invalidItem.line_no} 行单价必须是大于等于 0 的数字`;
  }, [draft]);
  const reviewWarnings = useMemo(() => {
    if (!draft) return [];
    const persistentWarnings = draft.warnings
      .filter((warning) => warning !== "未识别出商品，请按“商品+数量+单位”输入")
      .filter((warning) => !/^第 \d+ 行商品未匹配$/.test(warning));
    const unmatchedWarnings = draft.items
      .filter((item) => item.product_id === null)
      .map((item) => `第 ${item.line_no} 行请选择商品`);
    if (!draft.items.length) {
      return [...persistentWarnings, "未识别出商品，请点击“新增商品行”人工补录"];
    }
    return [...new Set([...persistentWarnings, ...unmatchedWarnings])];
  }, [draft]);

  function showCatalogMessage(message: string, isError = false) {
    if (isError) { setError(message); setNotice(""); }
    else { setNotice(message); setError(""); }
  }

  useEffect(() => {
    const generation = lifecycleGenerationRef.current + 1;
    lifecycleGenerationRef.current = generation;
    mountedRef.current = true;
    void loadCoreData(generation);
    void refreshMetrics(generation);
    return () => {
      if (lifecycleGenerationRef.current === generation) {
        mountedRef.current = false;
        lifecycleGenerationRef.current += 1;
      }
      taskMonitorRef.current?.close();
    };
  }, []);

  useEffect(() => {
    if (!selectedOrder) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") closeOrderDetails();
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [selectedOrder]);

  useEffect(() => {
    if (draftSaveStatus !== "dirty" || !draft || draft.status !== "needs_review" || draftIssue) return;
    const revision = editRevision.current;
    const pendingDraft = { ...draft, customer_id: customerId };
    const timer = window.setTimeout(() => {
      setDraftSaveStatus("saving");
      void api.updateDraft(pendingDraft)
        .then((savedDraft) => {
          if (editRevision.current !== revision) return;
          setDraft(savedDraft);
          setDrafts((current) => current.map((item) => item.id === savedDraft.id ? savedDraft : item));
          setDraftSaveStatus("saved");
          setDraftSaveError("");
        })
        .catch((reason) => {
          if (editRevision.current !== revision) return;
          setDraftSaveStatus("error");
          setDraftSaveError(reason instanceof Error ? reason.message : "草稿保存失败");
        });
    }, AUTO_SAVE_DELAY_MS);
    return () => window.clearTimeout(timer);
  }, [customerId, draft, draftIssue, draftSaveStatus]);

  useEffect(() => {
    if (!UNSAVED_DRAFT_STATES.includes(draftSaveStatus)) return;
    const warnBeforeClose = (event: BeforeUnloadEvent) => event.preventDefault();
    window.addEventListener("beforeunload", warnBeforeClose);
    return () => window.removeEventListener("beforeunload", warnBeforeClose);
  }, [draftSaveStatus]);

  function isCurrentGeneration(generation: number) {
    return mountedRef.current && lifecycleGenerationRef.current === generation;
  }

  async function loadCoreData(generation: number) {
    const [customerResult, productResult, orderResult, draftResult, taskResult] = await Promise.allSettled([
      api.customers(), api.products(), api.orders(), api.drafts(), api.recognitionTasks(),
    ]);
    if (!isCurrentGeneration(generation)) return;
    if (customerResult.status === "fulfilled") {
      setCustomers(customerResult.value);
      if (customerResult.value.length) setCustomerId(customerResult.value[0].id);
    }
    if (productResult.status === "fulfilled") setProducts(productResult.value);
    if (orderResult.status === "fulfilled") setOrders(orderResult.value);
    if (draftResult.status === "fulfilled") {
      setDrafts(draftResult.value);
      const pendingDraft = draftResult.value.find((item) => item.status === "needs_review");
      if (pendingDraft) await openDraft(pendingDraft, generation);
    }
    if (!isCurrentGeneration(generation)) return;
    if (taskResult.status === "fulfilled") {
      setTasks(taskResult.value);
      const activeTask = taskResult.value.find((item) =>
        ["pending", "running", "retrying"].includes(item.status),
      );
      if (activeTask) {
        setRecognizingTaskId(activeTask.id);
        void monitorRecognitionTask(activeTask.id).catch((reason) => {
          if (!isCurrentGeneration(generation)) return;
          setRecognizingTaskId(null);
          setError(reason instanceof Error ? reason.message : "恢复识别任务失败");
        });
      }
    }
    const coreFailures = [customerResult, productResult, orderResult, draftResult, taskResult]
      .filter((result) => result.status === "rejected").length;
    if (coreFailures) setError(`有 ${coreFailures} 项核心数据加载失败，请检查本地服务后刷新`);
  }

  async function refreshMetrics(
    generation = lifecycleGenerationRef.current,
  ) {
    const [overviewResult, modelResult, failureResult] = await Promise.allSettled([
      api.metricsOverview(), api.modelMetrics(), api.metricFailures(),
    ]);
    if (!isCurrentGeneration(generation)) return;
    if (overviewResult.status === "fulfilled") setMetrics(overviewResult.value);
    if (modelResult.status === "fulfilled") setModelMetrics(modelResult.value);
    if (failureResult.status === "fulfilled") setMetricFailures(failureResult.value);
    const failed = [overviewResult, modelResult, failureResult]
      .filter((result) => result.status === "rejected").length;
    setMetricsError(failed ? "模型监控暂时不可用，不影响客户、商品和草稿录单。" : "");
  }

  async function openOrderDetails(order: Order) {
    setSelectedOrder(order);
    setSelectedOrderDraft(null);
    setOrderDetailLoading(true);
    setOrderDetailError("");
    const [orderResult, draftResult] = await Promise.allSettled([
      api.order(order.id), api.draft(order.draft_id),
    ]);
    if (orderResult.status === "fulfilled") setSelectedOrder(orderResult.value);
    if (draftResult.status === "fulfilled") setSelectedOrderDraft(draftResult.value);
    if (orderResult.status === "rejected") {
      setOrderDetailError("完整订单读取失败，当前展示列表中的摘要数据。");
    } else if (draftResult.status === "rejected") {
      setOrderDetailError("订单已加载，但草稿召回证据暂时不可用。");
    }
    setOrderDetailLoading(false);
  }

  function closeOrderDetails() {
    setSelectedOrder(null);
    setSelectedOrderDraft(null);
    setOrderDetailError("");
  }

  async function openDraft(
    selectedDraft: Draft,
    generation = lifecycleGenerationRef.current,
  ) {
    if (draft?.id === selectedDraft.id) return;
    if (UNSAVED_DRAFT_STATES.includes(draftSaveStatus)) {
      const message = draftSaveStatus === "error"
        ? "草稿保存失败，请先重试保存"
        : draftIssue
          ? "请先补全或删除未完成的商品行，再切换草稿"
          : "草稿正在自动保存，请稍后再切换";
      setNotice(message);
      return;
    }
    setBusy(true); setError(""); setNotice("");
    try {
      const [freshDraft, freshCheckpoint] = await Promise.all([
        api.draft(selectedDraft.id), api.workflow(selectedDraft.id),
      ]);
      if (!isCurrentGeneration(generation)) return;
      editRevision.current = 0;
      setDraftSaveStatus("idle"); setDraftSaveError("");
      setDraft(freshDraft);
      setText(freshDraft.source_text);
      setCustomerId(freshDraft.customer_id);
      setCheckpoint(freshCheckpoint);
    } catch (reason) {
      if (isCurrentGeneration(generation)) {
        setError(reason instanceof Error ? reason.message : "加载草稿失败");
      }
    } finally {
      if (isCurrentGeneration(generation)) setBusy(false);
    }
  }

  async function recognize() {
    if (recognizingTaskId || recognizeLock.current) return;
    if (UNSAVED_DRAFT_STATES.includes(draftSaveStatus)) {
      setNotice("请先完成当前草稿保存，再开始新的识别任务");
      return;
    }
    recognizeLock.current = true;
    const generation = lifecycleGenerationRef.current;
    setBusy(true); setError(""); setNotice("");
    try {
      const task = await api.createRecognitionTask(
        text, customerId, crypto.randomUUID(),
      );
      if (!isCurrentGeneration(generation)) return;
      upsertTask(task);
      setRecognizingTaskId(task.id);
      setNotice("识别任务已进入本地队列，可以在下方查看进度");
      setBusy(false);
      await monitorRecognitionTask(task.id);
    }
    catch (reason) {
      if (isCurrentGeneration(generation)) {
        setError(reason instanceof Error ? reason.message : "识别失败");
      }
    }
    finally {
      recognizeLock.current = false;
      if (isCurrentGeneration(generation)) {
        setBusy(false); setRecognizingTaskId(null);
      }
    }
  }

  function upsertTask(task: RecognitionTask) {
    setTasks((current) => [task, ...current.filter((item) => item.id !== task.id)]);
  }

  async function applyRecognitionTaskSnapshot(
    task: RecognitionTask, isCurrent: () => boolean,
  ) {
    if (!isCurrent()) return true;
    upsertTask(task);
      if (task.status === "succeeded" && task.draft_id) {
        setRecognizingTaskId(null);
        const [nextDraft, nextCheckpoint, nextDrafts] = await Promise.all([
          api.draft(task.draft_id), api.workflow(task.draft_id), api.drafts(),
        ]);
        if (!isCurrent()) return true;
        setDraft(nextDraft); setCheckpoint(nextCheckpoint); setDrafts(nextDrafts);
        editRevision.current = 0; setDraftSaveStatus("idle"); setDraftSaveError("");
        setText(nextDraft.source_text); setCustomerId(nextDraft.customer_id);
        setNotice("识别完成，请核对草稿后再创建订单");
        const nextTasks = await api.recognitionTasks();
        if (!isCurrent()) return true;
        setTasks(nextTasks);
        await refreshMetrics();
        return true;
      }
      if (task.status === "failed") {
        setRecognizingTaskId(null);
        const nextTasks = await api.recognitionTasks();
        if (!isCurrent()) return true;
        setTasks(nextTasks);
        setError(task.error_message || "识别任务失败，可在任务列表中重跑");
        return true;
      }
      if (task.status === "cancelled") {
        setRecognizingTaskId(null);
        const nextTasks = await api.recognitionTasks();
        if (!isCurrent()) return true;
        setTasks(nextTasks);
        setNotice("识别任务已取消，没有创建订单");
        return true;
      }
    return false;
  }

  function monitorRecognitionTask(taskId: string): Promise<void> {
    const monitorGeneration = lifecycleGenerationRef.current;
    if (!isCurrentGeneration(monitorGeneration)) return Promise.resolve();
    taskMonitorRef.current?.close();
    return new Promise((resolve, reject) => {
      let active = true;
      let subscription: { close: () => void } | null = null;
      let timer: number | null = null;
      let reconnectAttempts = 0;
      let fallbackPolls = 0;
      let recoveryInFlight = false;
      let terminalProcessing = false;

      const cleanupMonitor = () => {
        if (!active) return;
        active = false;
        subscription?.close();
        if (timer !== null) window.clearTimeout(timer);
        if (taskMonitorRef.current?.taskId === taskId) taskMonitorRef.current = null;
      };
      const close = () => {
        cleanupMonitor();
        resolve();
      };
      const fail = (reason: unknown) => {
        cleanupMonitor();
        reject(reason);
      };
      taskMonitorRef.current = { taskId, close };

      const processTask = async (task: RecognitionTask) => {
        if (!active) return;
        const terminal = ["succeeded", "failed", "cancelled"].includes(task.status);
        if (terminal) {
          if (terminalProcessing) return;
          terminalProcessing = true;
          subscription?.close();
          subscription = null;
        }
        try {
          if (await applyRecognitionTaskSnapshot(
            task, () => active && isCurrentGeneration(monitorGeneration),
          )) close();
        } catch (reason) {
          fail(reason);
        }
      };

      const pollFallback = () => {
        if (!active) return;
        if (fallbackPolls >= FALLBACK_POLL_LIMIT) {
          if (isCurrentGeneration(monitorGeneration)) {
            setError("实时连接未恢复，任务仍可能在执行，请稍后刷新查看");
          }
          close();
          return;
        }
        fallbackPolls += 1;
        timer = window.setTimeout(() => {
          void api.recognitionTask(taskId)
            .then(async (task) => {
              await processTask(task);
              if (active) pollFallback();
            })
            .catch((reason) => {
              if (!active) return;
              if (reason instanceof ApiRequestError && reason.status === 404) {
                fail(reason);
              } else if (active) {
                pollFallback();
              }
            });
        }, FALLBACK_POLL_INTERVAL_MS);
      };

      const reconcileAndReconnect = async () => {
        if (!active) return;
        try {
          const task = await api.recognitionTask(taskId);
          await processTask(task);
          if (!active) return;
          if (reconnectAttempts < SSE_RECONNECT_LIMIT) {
            reconnectAttempts += 1;
            timer = window.setTimeout(connect, SSE_RECONNECT_DELAY_MS * reconnectAttempts);
          } else {
            if (isCurrentGeneration(monitorGeneration)) {
              setNotice("实时连接暂时不可用，已切换低频状态对账");
            }
            pollFallback();
          }
        } catch (reason) {
          if (!active) return;
          if (reason instanceof ApiRequestError && reason.status === 404) {
            fail(reason);
            return;
          }
          if (!active) return;
          if (reconnectAttempts < SSE_RECONNECT_LIMIT) {
            reconnectAttempts += 1;
            timer = window.setTimeout(connect, SSE_RECONNECT_DELAY_MS * reconnectAttempts);
          } else {
            if (isCurrentGeneration(monitorGeneration)) {
              setNotice("实时连接暂时不可用，已切换低频状态对账");
            }
            pollFallback();
          }
        } finally {
          recoveryInFlight = false;
        }
      };

      const connect = () => {
        if (!active) return;
        subscription = api.subscribeRecognitionTask(
          taskId,
          (task) => { void processTask(task); },
          () => {
            if (!active || terminalProcessing || recoveryInFlight) return;
            recoveryInFlight = true;
            subscription?.close();
            subscription = null;
            void reconcileAndReconnect();
          },
        );
      };

      connect();
    });
  }

  async function retryTask(taskId: string) {
    const generation = lifecycleGenerationRef.current;
    setError(""); setNotice("");
    try {
      const task = await api.retryRecognitionTask(taskId);
      if (!isCurrentGeneration(generation)) return;
      upsertTask(task); setRecognizingTaskId(task.id);
      setNotice("失败任务已重新排队");
      await monitorRecognitionTask(task.id);
    } catch (reason) {
      if (isCurrentGeneration(generation)) {
        setError(reason instanceof Error ? reason.message : "重试失败");
      }
    } finally {
      if (isCurrentGeneration(generation)) setRecognizingTaskId(null);
    }
  }

  async function cancelTask(taskId: string) {
    const generation = lifecycleGenerationRef.current;
    setError("");
    try {
      const task = await api.cancelRecognitionTask(taskId);
      if (!isCurrentGeneration(generation)) return;
      upsertTask(task);
      if (task.status === "cancelled" && taskMonitorRef.current?.taskId === taskId) {
        taskMonitorRef.current.close();
        setRecognizingTaskId(null);
      }
      setNotice(task.status === "cancelled" ? "任务已取消" : "已请求取消，当前步骤结束后生效");
    } catch (reason) {
      if (isCurrentGeneration(generation)) {
        setError(reason instanceof Error ? reason.message : "取消失败");
      }
    }
  }

  function taskStatusLabel(status: RecognitionTask["status"]) {
    return {
      pending: "排队中", running: "识别中", retrying: "等待重试",
      succeeded: "已完成", failed: "失败", cancelled: "已取消",
    }[status];
  }

  async function recognizeFile(file: File | undefined) {
    if (!file || recognizingTaskId || recognizeLock.current) return;
    if (UNSAVED_DRAFT_STATES.includes(draftSaveStatus)) {
      setNotice("请先完成当前草稿保存，再上传新的订单文件");
      return;
    }
    recognizeLock.current = true;
    const generation = lifecycleGenerationRef.current;
    setBusy(true); setError(""); setNotice("");
    try {
      const task = await api.createFileRecognitionTask(
        file, customerId, `file:${crypto.randomUUID()}`,
      );
      if (!isCurrentGeneration(generation)) return;
      upsertTask(task); setRecognizingTaskId(task.id);
      setNotice(`${file.name} 已进入本地识别队列`);
      setBusy(false);
      await monitorRecognitionTask(task.id);
    } catch (reason) {
      if (isCurrentGeneration(generation)) {
        setError(reason instanceof Error ? reason.message : "文件识别失败");
      }
    }
    finally {
      recognizeLock.current = false;
      if (isCurrentGeneration(generation)) {
        setBusy(false); setRecognizingTaskId(null);
      }
    }
  }

  function patchItem(index: number, patch: Record<string, unknown>) {
    if (!draft) return;
    const items = draft.items.map((item, i) => i === index ? { ...item, ...patch } : item);
    updateDraftLocally({ ...draft, items });
  }

  function customUnitEditorKey(lineNo: number) {
    return `${draft?.id ?? ""}:${lineNo}`;
  }

  function chooseUnit(index: number, lineNo: number, unit: string) {
    const editorKey = customUnitEditorKey(lineNo);
    setCustomUnitEditors((current) => {
      const next = new Set(current);
      if (unit === CUSTOM_UNIT_VALUE) next.add(editorKey);
      else next.delete(editorKey);
      return next;
    });
    patchItem(index, { unit });
  }

  function updateDraftLocally(nextDraft: Draft) {
    editRevision.current += 1;
    setDraft(nextDraft);
    setDraftSaveStatus("dirty");
    setDraftSaveError("");
  }

  function addManualItem() {
    if (!draft || draft.status !== "needs_review") return;
    const nextLineNo = Math.max(0, ...draft.items.map((item) => item.line_no)) + 1;
    const item: DraftItem = {
      line_no: nextLineNo, raw_product_name: "", product_id: null,
      product_name: "", quantity: 1, unit: "", unit_price: 0,
      note: "", matched: false,
    };
    updateDraftLocally({ ...draft, items: [...draft.items, item] });
  }

  function removeItem(index: number) {
    if (!draft || draft.status !== "needs_review") return;
    const draftKeyPrefix = `${draft.id}:`;
    setCustomUnitEditors((current) => {
      const next = new Set(
        [...current].filter((key) => !key.startsWith(draftKeyPrefix)),
      );
      let nextLineNo = 0;
      draft.items.forEach((item, itemIndex) => {
        if (itemIndex === index) return;
        nextLineNo += 1;
        if (current.has(customUnitEditorKey(item.line_no))) {
          next.add(`${draft.id}:${nextLineNo}`);
        }
      });
      return next;
    });
    const items = draft.items
      .filter((_, itemIndex) => itemIndex !== index)
      .map((item, itemIndex) => ({ ...item, line_no: itemIndex + 1 }));
    updateDraftLocally({ ...draft, items });
  }

  function selectProduct(index: number, productId: number) {
    const product = products.find((item) => item.id === productId);
    if (!product) return;
    const lineNo = draft?.items[index]?.line_no;
    if (lineNo !== undefined) {
      const editorKey = customUnitEditorKey(lineNo);
      setCustomUnitEditors((current) => {
        const next = new Set(current);
        next.delete(editorKey);
        return next;
      });
    }
    patchItem(index, {
      product_id: product.id, product_name: product.name, raw_product_name: product.name,
      unit: product.unit, unit_price: product.unit_price, matched: true,
    });
  }

  async function confirm() {
    if (!draft || draft.status !== "needs_review") return;
    if (draftIssue) { setError(draftIssue); setNotice(""); return; }
    editRevision.current += 1;
    setBusy(true); setDraftSaveStatus("saving"); setError(""); setNotice("");
    try {
      const updated = await api.updateDraft({ ...draft, customer_id: customerId });
      setDraft(updated);
      const order = await api.confirm(updated.id);
      setNotice(`订单 ${order.order_no} 已创建`);
      const [nextOrders, nextDrafts, confirmedDraft, nextCheckpoint] = await Promise.all([
        api.orders(), api.drafts(), api.draft(updated.id), api.workflow(updated.id),
      ]);
      setOrders(nextOrders); setDrafts(nextDrafts);
      setDraft(confirmedDraft); setCheckpoint(nextCheckpoint);
      setDraftSaveStatus("idle"); setDraftSaveError("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "确认失败");
      setDraftSaveStatus("error");
      setDraftSaveError("草稿或订单保存失败，请检查后重试");
    } finally { setBusy(false); }
  }

  function changeCustomer(nextCustomerId: number) {
    setCustomerId(nextCustomerId);
    if (draft?.status === "needs_review") {
      updateDraftLocally({ ...draft, customer_id: nextCustomerId });
    }
  }

  function draftSaveLabel() {
    if (draftSaveStatus === "dirty" && draftIssue) return "填写完整后自动保存";
    return {
      idle: "草稿未修改", dirty: "等待自动保存…", saving: "正在保存草稿…",
      saved: "草稿已保存", error: draftSaveError || "草稿保存失败",
    }[draftSaveStatus];
  }

  async function openAgentDraft(draftId: string) {
    setError("");
    setWorkspaceMode("classic");
    try {
      const currentDraft = await api.draft(draftId);
      if (!mountedRef.current) return;
      await openDraft(currentDraft);
    } catch (reason) {
      if (!mountedRef.current) return;
      setError(reason instanceof Error ? reason.message : "草稿加载失败");
    }
  }

  async function refreshAfterAgentOrder() {
    try {
      const [nextOrders, nextDrafts] = await Promise.all([api.orders(), api.drafts()]);
      if (!mountedRef.current) return;
      setOrders(nextOrders);
      setDrafts(nextDrafts);
    } catch (reason) {
      if (!mountedRef.current) return;
      setError(reason instanceof Error ? reason.message : "订单列表刷新失败");
      setWorkspaceMode("classic");
    }
  }

  return <div className="shell">
    <header className="topbar">
      <div className="brand"><span className="brand-mark">拾</span><div><h1>拾单</h1><p>Local AI Order Agent</p></div></div>
      <div className="status"><span /> 本地服务已连接</div>
    </header>

    <main>
      <section className="hero">
        <div><span className="eyebrow">AI 辅助 · 人工确认</span><h2>把一句话，变成一张可靠订单</h2><p>模型负责理解，本地目录负责事实，最终由你确认落单。</p></div>
        <div className="hero-number"><strong>{orders.length}</strong><span>已确认订单</span></div>
      </section>

      <nav className="mode-switch" aria-label="录单方式">
        <button
          type="button" className={workspaceMode === "classic" ? "active" : ""}
          aria-pressed={workspaceMode === "classic"}
          onClick={() => setWorkspaceMode("classic")}
        ><span>标准录单</span><small>识别后直接人工审核</small></button>
        <button
          type="button" className={workspaceMode === "agent" ? "active" : ""}
          aria-pressed={workspaceMode === "agent"}
          onClick={() => setWorkspaceMode("agent")}
        ><span>Agent 对话</span><small>多轮沟通与受控工具调用</small></button>
      </nav>

      <div className="mode-panel" hidden={workspaceMode !== "agent"}><AgentWorkspace
          customers={customers}
          onOpenDraft={(draftId) => { void openAgentDraft(draftId); }}
          onOrderCreated={() => { void refreshAfterAgentOrder(); }}
        /></div>
      <div className="mode-panel" hidden={workspaceMode !== "classic"}>

      {(error || notice) && <div className={error ? "alert error" : "alert success"}>{error || notice}</div>}

      <section className="card metrics-panel">
        <div className="section-title"><span>00</span><div><h3>模型运行监控</h3><p>{modelMetrics.length ? `${modelMetrics[0].model} · ${modelMetrics[0].prompt_version}` : "纯规则模式或暂无模型调用"}</p></div></div>
        {metricsError && <p className="inline-warning" role="status">{metricsError}</p>}
        <div className="metric-grid">
          <article><span>调用次数</span><strong>{metrics?.total_calls ?? 0}</strong></article>
          <article><span>成功率</span><strong>{(metrics?.success_rate ?? 0).toFixed(1)}%</strong></article>
          <article><span>降级率</span><strong>{(metrics?.fallback_rate ?? 0).toFixed(1)}%</strong></article>
          <article className="latency-metric">
            <span>P95 时延</span>
            <strong>{p95Display.value}<small> {p95Display.unit}</small></strong>
            <em>{p95SampleContext(metrics?.total_calls ?? 0)}</em>
          </article>
          <article><span>累计 Token</span><strong>{(metrics?.total_tokens ?? 0).toLocaleString()}</strong></article>
          <article><span>估算费用</span><strong>{(metrics?.estimated_cost ?? 0).toFixed(6)}</strong></article>
        </div>
        {metricFailures.length > 0 && <div className="failure-strip"><b>最近降级</b>{metricFailures.map((failure) => <span key={failure.id}>{failure.error_type} · {failure.fallback_reason || "无详细原因"}</span>)}</div>}
      </section>

      <div className="workspace">
        <section className="card composer">
          <div className="section-title"><span>01</span><div><h3>描述这张订单</h3><p>商品、数量、单位和加工备注都可以直接说</p></div></div>
          <label>下单客户</label>
          <select value={customerId ?? ""} onChange={(event) => changeCustomer(Number(event.target.value))}>
            {customers.map((customer) => <option key={customer.id} value={customer.id}>{customer.name} · {customer.contact}</option>)}
          </select>
          <label>订单原话</label>
          <textarea value={text} onChange={(event) => setText(event.target.value)} rows={6} />
          <div className="hint">试试：{EXAMPLE}</div>
          <button className="primary" onClick={recognize} disabled={busy || Boolean(recognizingTaskId) || !text.trim()}>{recognizingTaskId ? "识别任务执行中…" : "开始智能识别 →"}</button>
          <div className="file-divider"><span>或上传订单文件</span></div>
          <label className="file-button">选择 TXT / CSV / XLSX / 图片 / PDF
            <input type="file" accept=".txt,.csv,.xlsx,.jpg,.jpeg,.png,.pdf" disabled={busy || Boolean(recognizingTaskId)} onChange={(event) => { void recognizeFile(event.target.files?.[0]); event.target.value = ""; }} />
          </label>
          <div className="hint">图片/PDF 最大 8MB、PDF 最多 5 页；扫描件在本地 OCR，低置信度会提示人工复核。</div>
        </section>

        <section className="card result">
          <div className="section-title"><span>02</span><div><h3>核对识别结果</h3><p>目录价只作默认值，单价可人工修改</p></div>{draft && <em>{draft.provider}</em>}</div>
          {!draft ? <div className="empty"><div>⌁</div><h4>等待一条订单</h4><p>识别结果会在这里变成可编辑的订单草稿</p></div> : <>
            <div className="evidence-bar">
              <span>输入：{draft.source_filename || draft.input_type}</span>
              {draft.input_confidence !== null && <span className={draft.input_confidence < 70 ? "waiting" : ""}>OCR 置信度：{draft.input_confidence.toFixed(1)}%</span>}
              <span>站点 {draft.site_id} · SKU v{draft.sku_version}</span>
              <span className={checkpoint?.waiting_for_review ? "waiting" : ""}>{draft.status === "confirmed" ? "状态：已确认" : checkpoint?.waiting_for_review ? "Checkpoint：等待人工审核" : "Checkpoint：已恢复"}</span>
            </div>
            {draft.status === "needs_review" && <div className="manual-entry-bar"><span>识别不完整时可人工新增；选择商品后仍可覆盖单位和单价。</span><button type="button" onClick={addManualItem}>＋ 新增商品行</button></div>}
            <div className="table-wrap"><table><thead><tr><th>商品</th><th>数量</th><th>单位</th><th>单价（可改）</th><th>备注</th><th>小计</th><th>操作</th></tr></thead><tbody>
              {draft.items.map((item, index) => {
                const usesCustomUnit = customUnitEditors.has(
                  customUnitEditorKey(item.line_no),
                ) || !unitOptions.includes(item.unit);
                return <tr key={index} className={item.matched ? "" : "unmatched"}>
                <td><select aria-label={`第 ${item.line_no} 行商品`} disabled={draft.status === "confirmed"} value={item.product_id ?? ""} onChange={(event) => selectProduct(index, Number(event.target.value))}><option value="">未匹配</option>{products.map((product) => <option key={product.id} value={product.id}>{product.name}</option>)}</select></td>
                <td><input aria-label={`第 ${item.line_no} 行数量`} disabled={draft.status === "confirmed"} type="number" min="0.01" step="0.01" value={item.quantity} onChange={(event) => patchItem(index, { quantity: Number(event.target.value) })} /></td>
                <td><div className="unit-editor">
                  <select
                    aria-label={`第 ${item.line_no} 行单位`}
                    disabled={draft.status === "confirmed"}
                    value={usesCustomUnit ? CUSTOM_UNIT_VALUE : item.unit}
                    onChange={(event) => chooseUnit(
                      index, item.line_no, event.target.value,
                    )}
                  >
                    {unitOptions.map((unit) => <option key={unit} value={unit}>{unit}</option>)}
                    <option value={CUSTOM_UNIT_VALUE}>＋ 新建单位</option>
                  </select>
                  {usesCustomUnit && <input
                    aria-label={`第 ${item.line_no} 行自定义单位`}
                    disabled={draft.status === "confirmed"}
                    maxLength={30}
                    placeholder="输入新单位"
                    value={item.unit}
                    onChange={(event) => patchItem(index, { unit: event.target.value })}
                  />}
                </div></td>
                <td><input aria-label={`第 ${item.line_no} 行单价`} disabled={draft.status === "confirmed"} type="number" min="0" step="0.01" value={item.unit_price} onChange={(event) => patchItem(index, { unit_price: Number(event.target.value) })} /></td>
                <td><input aria-label={`第 ${item.line_no} 行备注`} disabled={draft.status === "confirmed"} value={item.note} placeholder="无" onChange={(event) => patchItem(index, { note: event.target.value })} /></td>
                <td>¥{(item.quantity * item.unit_price).toFixed(2)}</td>
                <td>{draft.status === "needs_review" && <button className="remove-line" type="button" onClick={() => removeItem(index)}>删除</button>}</td>
              </tr>;})}
            </tbody></table></div>
            {reviewWarnings.length > 0 && <div className="warnings">{reviewWarnings.map((warning) => <p key={warning}>⚠ {warning}</p>)}</div>}
            {draftIssue && <div className="draft-validation">{draftIssue}</div>}
            {draft.status === "needs_review" && <div className={`draft-save-state ${draftSaveStatus}`}><span>{draftSaveLabel()}</span>{draftSaveStatus === "error" && <button type="button" onClick={() => setDraftSaveStatus("dirty")}>重试保存</button>}</div>}
            <div className="total"><span>{draft.items.length} 项商品</span><div>预估合计 <strong>¥{total.toFixed(2)}</strong></div></div>
            <button className="confirm" onClick={confirm} disabled={busy || draftSaveStatus === "saving" || Boolean(draftIssue) || draft.status === "confirmed"}>{draft.status === "confirmed" ? "订单已创建" : "确认并创建订单"}</button>
          </>}
        </section>
      </div>

      <section className="card tasks-panel">
        <div className="section-title"><span>03</span><div><h3>识别任务</h3><p>本地持久化队列，支持失败重试、取消和刷新恢复</p></div></div>
        {tasks.length === 0 ? <p className="muted">还没有识别任务。</p> : <div className="task-list">
          {tasks.slice(0, 8).map((task) => <article key={task.id}>
            <div className="task-copy"><strong>{task.text}</strong><span>{new Date(task.created_at).toLocaleString("zh-CN")} · 尝试 {task.attempt_count}/{task.max_attempts}</span>{task.error_message && <small>{task.error_type}：{task.error_message}</small>}</div>
            <div className="task-actions"><em className={task.status}>{taskStatusLabel(task.status)}</em>
              {task.status === "failed" && <button onClick={() => void retryTask(task.id)} disabled={Boolean(recognizingTaskId)}>重跑</button>}
              {["pending", "running", "retrying"].includes(task.status) && <button onClick={() => void cancelTask(task.id)}>取消</button>}
              {task.draft_id && <button onClick={() => { const item = drafts.find((candidate) => candidate.id === task.draft_id); if (item) void openDraft(item); }}>查看草稿</button>}
            </div>
          </article>)}
        </div>}
      </section>

      <section className="card drafts-panel">
        <div className="section-title"><span>04</span><div><h3>最近草稿</h3><p>刷新页面后仍可继续审核，数据保存在本地 SQLite</p></div></div>
        {drafts.length === 0 ? <p className="muted">还没有识别草稿。</p> : <div className="draft-list">
          {drafts.slice(0, 8).map((item) => <button key={item.id} className={draft?.id === item.id ? "active" : ""} onClick={() => void openDraft(item)} disabled={busy}>
            <div><strong>{item.source_text}</strong><span>{item.items.length} 项 · {new Date(item.updated_at).toLocaleString("zh-CN")}</span></div>
            <em className={item.status}>{item.status === "needs_review" ? "待审核" : "已确认"}</em>
          </button>)}
        </div>}
      </section>

      <CatalogPanel
        products={products}
        onProductsChanged={setProducts}
        onMessage={showCatalogMessage}
      />

      <div className="lower-grid">
        <section className="card trace"><div className="section-title"><span>05</span><div><h3>Agent 执行轨迹</h3><p>展示可解释的工作步骤，不展示隐藏推理</p></div></div>
          {!draft ? <p className="muted">完成识别后，这里会显示每个节点的结果和耗时。</p> : <ol>{draft.trace.map((step) => <li key={step.step}><b>✓</b><div><strong>{step.name}</strong><p>{step.summary}</p></div><time>{step.duration_ms} ms</time></li>)}</ol>}
        </section>
        <section className="card history"><div className="section-title"><span>06</span><div><h3>最近订单</h3><p>数据保存在本地 SQLite 中</p></div></div>
          {orders.length === 0 ? <p className="muted">还没有已确认订单。</p> : <div className="order-list">{orders.slice(0, 5).map((order) => <button type="button" key={order.id} onClick={() => void openOrderDetails(order)}><div><strong>{order.customer_name}</strong><span>{order.order_no}</span></div><div><b>¥{order.total_amount.toFixed(2)}</b><span>{order.items.length} 项 · 查看详情</span></div></button>)}</div>}
        </section>
      </div>
      </div>
    </main>
    <OrderDetailsDrawer
      order={selectedOrder}
      draft={selectedOrderDraft}
      loading={orderDetailLoading}
      error={orderDetailError}
      onClose={closeOrderDetails}
    />
    <footer>本地演示项目 · 数据不会离开你的电脑（启用云模型时，订单原话会发送给所配置的模型服务）</footer>
  </div>;
}
