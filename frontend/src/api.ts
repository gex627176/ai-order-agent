import type {
  AliasCandidate, AliasReviewResult, BusinessMetrics, CatalogImportResult, CatalogPublishResult,
  Customer, Draft, LlmFailureRecord, MetricsOverview, ModelMetrics,
  Order, Product, RecognitionTask, WorkflowCheckpoint,
} from "./types";

export class ApiRequestError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
    this.name = "ApiRequestError";
  }
}

async function request<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    ...options,
    headers: { "Content-Type": "application/json", ...options?.headers },
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new ApiRequestError(
      payload.detail || `请求失败（${response.status}）`, response.status,
    );
  }
  return response.json();
}

export type RecognitionTaskSubscription = {
  close: () => void;
};

export const api = {
  customers: () => request<Customer[]>("/api/catalog/customers"),
  products: () => request<Product[]>("/api/catalog/products"),
  createProduct: (product: Omit<Product, "id">) => request<CatalogPublishResult>(
    "/api/catalog/products?site_id=1", {
      method: "POST",
      body: JSON.stringify({ ...product, source_type: "admin" }),
    },
  ),
  updateProduct: (product: Product) => request<CatalogPublishResult>(
    `/api/catalog/products/${product.id}?site_id=1`,
    { method: "PUT", body: JSON.stringify(product) },
  ),
  importCatalog: async (file: File) => {
    const form = new FormData();
    form.append("file", file);
    form.append("site_id", "1");
    const response = await fetch("/api/catalog/import", { method: "POST", body: form });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      throw new Error(payload.detail || `请求失败（${response.status}）`);
    }
    return response.json() as Promise<CatalogImportResult>;
  },
  aliasCandidates: () => request<AliasCandidate[]>("/api/catalog/alias-candidates"),
  reviewAliasCandidate: (id: number, decision: "approved" | "rejected") =>
    request<AliasReviewResult>(`/api/catalog/alias-candidates/${id}/review`, {
      method: "POST", body: JSON.stringify({ decision }),
    }),
  businessMetrics: () => request<BusinessMetrics>("/api/metrics/business"),
  orders: () => request<Order[]>("/api/orders"),
  order: (orderId: number) => request<Order>(`/api/orders/${orderId}`),
  drafts: (status?: "needs_review" | "confirmed") => request<Draft[]>(
    "/api/drafts" + (status ? "?status=" + status : ""),
  ),
  draft: (draftId: string) => request<Draft>("/api/drafts/" + draftId),
  metricsOverview: () => request<MetricsOverview>("/api/metrics/overview"),
  modelMetrics: () => request<ModelMetrics[]>("/api/metrics/models"),
  metricFailures: () => request<LlmFailureRecord[]>("/api/metrics/failures?limit=5"),
  recognitionTasks: () => request<RecognitionTask[]>("/api/recognition-tasks"),
  recognitionTask: (taskId: string) => request<RecognitionTask>(
    "/api/recognition-tasks/" + taskId,
  ),
  subscribeRecognitionTask: (
    taskId: string,
    onTask: (task: RecognitionTask) => void,
    onError: () => void,
  ): RecognitionTaskSubscription => {
    const source = new EventSource(
      `/api/recognition-tasks/${encodeURIComponent(taskId)}/events`,
    );
    source.addEventListener("task", (event) => {
      try {
        onTask(JSON.parse((event as MessageEvent<string>).data) as RecognitionTask);
      } catch {
        onError();
      }
    });
    source.onerror = onError;
    return { close: () => source.close() };
  },
  createRecognitionTask: (
    text: string, customerId: number | null, idempotencyKey: string,
  ) => request<RecognitionTask>("/api/recognition-tasks", {
    method: "POST",
    headers: { "Idempotency-Key": idempotencyKey },
    body: JSON.stringify({ text, customer_id: customerId, site_id: 1 }),
  }),
  createFileRecognitionTask: async (
    file: File, customerId: number | null, idempotencyKey: string,
  ) => {
    const form = new FormData();
    form.append("file", file);
    form.append("customer_id", String(customerId ?? 1));
    form.append("site_id", "1");
    const response = await fetch("/api/recognition-tasks/file", {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      body: form,
    });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      throw new Error(payload.detail || `请求失败（${response.status}）`);
    }
    return response.json() as Promise<RecognitionTask>;
  },
  retryRecognitionTask: (taskId: string) => request<RecognitionTask>(
    "/api/recognition-tasks/" + taskId + "/retry", { method: "POST" },
  ),
  cancelRecognitionTask: (taskId: string) => request<RecognitionTask>(
    "/api/recognition-tasks/" + taskId + "/cancel", { method: "POST" },
  ),
  recognize: (text: string, customerId: number | null) => request<Draft>(
    "/api/drafts/recognize",
    {
      method: "POST",
      body: JSON.stringify({ text, customer_id: customerId, site_id: 1 }),
    },
  ),
  workflow: (draftId: string) => request<WorkflowCheckpoint>(`/api/drafts/${draftId}/workflow`),
  updateDraft: (draft: Draft) => request<Draft>(`/api/drafts/${draft.id}`, {
    method: "PUT",
    body: JSON.stringify({ customer_id: draft.customer_id, items: draft.items }),
  }),
  confirm: (draftId: string) => request<Order>(`/api/drafts/${draftId}/confirm`, {
    method: "POST",
    headers: { "Idempotency-Key": `draft:${draftId}` },
  }),
};
