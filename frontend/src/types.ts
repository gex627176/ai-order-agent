export type Customer = { id: number; name: string; contact: string };
export type AgentSessionStatus =
  | "active" | "waiting_input" | "waiting_approval" | "completed" | "failed";
export type AgentPendingAction = {
  name: "provide_customer" | "review_sku" | "confirm_order";
  arguments: Record<string, unknown>;
  permission: "read" | "draft_write" | "transaction_write";
};
export type AgentEvent = {
  id: number; sequence: number; type: string; actor: string; name: string;
  status: string; summary: string; payload: Record<string, unknown>; created_at: string;
};
export type AgentSession = {
  id: string; status: AgentSessionStatus; site_id: number; customer_id: number | null;
  current_draft_id: string | null; current_order_id: number | null;
  pending_action: AgentPendingAction | null; context_summary: string;
  summary_through_sequence: number; last_message: string; version: number;
  last_event_sequence: number; has_more_events: boolean;
  created_at: string; updated_at: string; events: AgentEvent[];
};
export type Product = {
  id: number; sku: string; name: string; aliases: string[];
  unit: string; unit_price: number; active: boolean;
};
export type SkuVersion = {
  site_id: number; version: number; status: string; content_md5: string; created_at: string;
};
export type CatalogPublishResult = {
  product: Product | null; version: SkuVersion; indexed_count: number; backend: string;
  index_synced: boolean; index_error: string | null;
};
export type CatalogImportResult = {
  imported_count: number; version: SkuVersion; indexed_count: number; backend: string;
  index_synced: boolean; index_error: string | null;
};
export type AliasCandidate = {
  id: number; site_id: number; product_id: number; product_name: string;
  alias: string; evidence_count: number; status: "pending" | "approved" | "rejected";
  source_type: string; created_at: string; updated_at: string; reviewed_at: string | null;
};
export type AliasReviewResult = {
  candidate: AliasCandidate; version: SkuVersion | null; indexed_count: number;
  backend: string; index_synced: boolean; index_error: string | null;
};
export type BusinessMetrics = {
  total_tasks: number; completed_tasks: number; task_success_rate: number;
  confirmed_orders: number; total_recognized_items: number; auto_matched_items: number;
  auto_match_rate: number; unmatched_items: number; unmatched_rate: number;
  corrected_drafts: number; correction_rate: number; pending_alias_candidates: number;
  approved_alias_candidates: number; catalog_products: number; active_products: number;
  current_sku_version: number; average_task_duration_ms: number; p95_task_duration_ms: number;
};
export type DraftItem = {
  line_no: number; raw_product_name: string; product_id: number | null;
  product_name: string; quantity: number; unit: string; unit_price: number;
  note: string; matched: boolean;
};
export type TraceStep = {
  step: number; name: string; status: string; summary: string; duration_ms: number;
};
export type Draft = {
  id: string; customer_id: number | null; source_text: string; status: string;
  items: DraftItem[]; warnings: string[]; trace: TraceStep[]; provider: string;
  site_id: number; sku_version: number; retrieval: Record<string, unknown>[];
  preferences: Record<string, unknown>[]; input_type: string; source_filename: string;
  input_confidence: number | null; input_warnings: string[];
  created_at: string; updated_at: string;
};
export type RecognitionTask = {
  id: string; idempotency_key: string; payload_md5: string;
  status: "pending" | "running" | "retrying" | "succeeded" | "failed" | "cancelled";
  text: string; customer_id: number | null; site_id: number; input_type: string;
  source_filename: string; draft_id: string | null; attempt_count: number;
  input_confidence: number | null; input_warnings: string[];
  max_attempts: number; cancel_requested: boolean; error_type: string;
  error_message: string; next_run_at: string; created_at: string;
  updated_at: string; started_at: string | null; finished_at: string | null;
};
export type WorkflowCheckpoint = {
  thread_id: string; checkpoint_exists: boolean; waiting_for_review: boolean; next_nodes: string[];
};
export type MetricsOverview = {
  total_calls: number; success_count: number; fallback_count: number; failure_count: number;
  success_rate: number; fallback_rate: number; average_duration_ms: number;
  p95_duration_ms: number; prompt_tokens: number; completion_tokens: number;
  total_tokens: number; estimated_cost: number;
};
export type ModelMetrics = MetricsOverview & {
  provider: string; model: string; prompt_version: string;
};
export type LlmFailureRecord = {
  id: number; task_id: string; draft_id: string | null; provider: string;
  model: string; prompt_version: string; prompt_tokens: number;
  completion_tokens: number; total_tokens: number; estimated_cost: number;
  duration_ms: number; status: string; error_type: string;
  fallback_reason: string; created_at: string;
};
export type Order = {
  id: number; order_no: string; draft_id: string; customer_id: number;
  customer_name: string; source_text: string; total_amount: number;
  status: string; site_id: number; sku_version: number;
  idempotency_key: string; payload_md5: string;
  input_type: string; source_filename: string; created_at: string;
  items: OrderItem[];
};
export type OrderItem = {
  line_no: number; product_id: number; product_name: string;
  quantity: number; unit: string; unit_price: number; note: string; subtotal: number;
};
