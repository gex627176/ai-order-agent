from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .agent_runtime.models import PendingAction, SessionStatus


class AgentSessionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    site_id: int = Field(ge=1)
    customer_id: int | None = Field(default=None, ge=1)


class AgentMessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=1, max_length=5000)


class AgentResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approved: bool


class AgentEvent(BaseModel):
    id: int
    sequence: int
    type: str
    actor: str
    name: str
    status: str
    summary: str
    payload: dict[str, Any]
    created_at: str


class AgentSession(BaseModel):
    id: str
    status: SessionStatus
    site_id: int
    customer_id: int | None
    current_draft_id: str | None
    current_order_id: int | None
    pending_action: PendingAction | None
    context_summary: str
    summary_through_sequence: int
    last_message: str
    version: int
    last_event_sequence: int
    has_more_events: bool
    created_at: str
    updated_at: str
    events: list[AgentEvent]


class Customer(BaseModel):
    id: int
    name: str
    contact: str = ""


class Product(BaseModel):
    id: int
    sku: str
    name: str
    aliases: list[str]
    unit: str
    unit_price: float = Field(ge=0)
    active: bool


class ProductCreate(BaseModel):
    sku: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=120)
    aliases: list[str] = Field(default_factory=list)
    unit: str = Field(min_length=1, max_length=20)
    unit_price: float = Field(ge=0, le=1_000_000, allow_inf_nan=False)
    active: bool = True
    source_type: str = Field(default="admin", pattern="^(admin|import)$")


class ProductUpdate(BaseModel):
    sku: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=120)
    aliases: list[str] = Field(default_factory=list)
    unit: str = Field(min_length=1, max_length=20)
    unit_price: float = Field(ge=0, le=1_000_000, allow_inf_nan=False)
    active: bool


class CatalogPublishResult(BaseModel):
    product: Product | None = None
    version: "SkuVersion"
    indexed_count: int
    backend: str
    index_synced: bool
    index_error: str | None = None


class CatalogImportResult(BaseModel):
    imported_count: int
    version: "SkuVersion"
    indexed_count: int
    backend: str
    index_synced: bool
    index_error: str | None = None


class AliasCandidate(BaseModel):
    id: int
    site_id: int
    product_id: int
    product_name: str
    alias: str
    evidence_count: int
    status: str
    source_type: str
    created_at: str
    updated_at: str
    reviewed_at: str | None = None


class AliasReviewRequest(BaseModel):
    decision: str = Field(pattern="^(approved|rejected)$")


class AliasReviewResult(BaseModel):
    candidate: AliasCandidate
    version: "SkuVersion | None" = None
    indexed_count: int = 0
    backend: str = "not_synced"
    index_synced: bool = True
    index_error: str | None = None


class BusinessMetrics(BaseModel):
    total_tasks: int
    completed_tasks: int
    task_success_rate: float
    confirmed_orders: int
    total_recognized_items: int
    auto_matched_items: int
    auto_match_rate: float
    unmatched_items: int
    unmatched_rate: float
    corrected_drafts: int
    correction_rate: float
    pending_alias_candidates: int
    approved_alias_candidates: int
    catalog_products: int
    active_products: int
    current_sku_version: int
    average_task_duration_ms: float
    p95_task_duration_ms: int


class HealthResponse(BaseModel):
    status: str
    ai_provider: str
    database: str


class RecognitionRequest(BaseModel):
    text: str = Field(min_length=1, max_length=5000)
    customer_id: int | None = None
    site_id: int = Field(ge=1)


class RecognitionTask(BaseModel):
    id: str
    idempotency_key: str
    payload_md5: str
    status: str
    text: str
    customer_id: int | None
    site_id: int
    input_type: str
    source_filename: str
    input_confidence: float | None = None
    input_warnings: list[str] = Field(default_factory=list)
    draft_id: str | None
    attempt_count: int
    max_attempts: int
    cancel_requested: bool
    error_type: str
    error_message: str
    next_run_at: str
    created_at: str
    updated_at: str
    started_at: str | None
    finished_at: str | None


class DraftItem(BaseModel):
    line_no: int = Field(ge=1)
    raw_product_name: str = Field(max_length=120)
    product_id: int | None = None
    product_name: str = Field(max_length=120)
    quantity: float = Field(gt=0, le=1_000_000, allow_inf_nan=False)
    unit: str = Field(max_length=30)
    unit_price: float = Field(ge=0, le=1_000_000, allow_inf_nan=False)
    note: str = Field(default="", max_length=500)
    matched: bool


class TraceStep(BaseModel):
    step: int
    name: str
    status: str
    summary: str
    duration_ms: int = Field(ge=0)


class Draft(BaseModel):
    id: str
    customer_id: int | None
    source_text: str
    status: str
    items: list[DraftItem]
    warnings: list[str]
    trace: list[TraceStep]
    provider: str
    site_id: int
    sku_version: int
    retrieval: list[dict] = Field(default_factory=list)
    preferences: list[dict] = Field(default_factory=list)
    input_type: str = "text"
    source_filename: str = ""
    input_confidence: float | None = None
    input_warnings: list[str] = Field(default_factory=list)
    created_at: str
    updated_at: str


class DraftUpdate(BaseModel):
    customer_id: int | None = None
    items: list[DraftItem] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def validate_line_numbers(self) -> DraftUpdate:
        line_numbers = [item.line_no for item in self.items]
        if len(line_numbers) != len(set(line_numbers)):
            raise ValueError("订单明细行号不能重复")
        return self


class WorkflowCheckpoint(BaseModel):
    thread_id: str
    checkpoint_exists: bool
    waiting_for_review: bool
    next_nodes: list[str]


class SkuVersion(BaseModel):
    site_id: int
    version: int
    status: str
    content_md5: str
    created_at: str


class SkuIndexStatus(BaseModel):
    site_id: int
    sku_version: int
    indexed_count: int
    backend: str
    index_synced: bool
    index_error: str | None = None


class SkuVersionPublishResult(BaseModel):
    version: SkuVersion
    indexed_count: int
    backend: str
    index_synced: bool
    index_error: str | None = None


class CorrectionEvent(BaseModel):
    id: int
    draft_id: str
    customer_id: int | None
    field_path: str
    before: object
    after: object
    created_at: str


class CustomerPreference(BaseModel):
    customer_id: int
    preference_key: str
    preference_value: object
    evidence_count: int
    updated_at: str


class MetricsOverview(BaseModel):
    total_calls: int
    success_count: int
    fallback_count: int
    failure_count: int
    success_rate: float
    fallback_rate: float
    average_duration_ms: float
    p95_duration_ms: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    estimated_cost: float


class ModelMetrics(MetricsOverview):
    provider: str
    model: str
    prompt_version: str


class LlmFailureRecord(BaseModel):
    id: int
    task_id: str
    draft_id: str | None
    provider: str
    model: str
    prompt_version: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    estimated_cost: float
    duration_ms: int
    status: str
    error_type: str
    fallback_reason: str
    created_at: str


class OrderItem(BaseModel):
    line_no: int
    product_id: int
    product_name: str
    quantity: float
    unit: str
    unit_price: float
    note: str
    subtotal: float


class Order(BaseModel):
    id: int
    order_no: str
    draft_id: str
    customer_id: int
    customer_name: str
    source_text: str
    total_amount: float
    status: str
    site_id: int
    sku_version: int
    idempotency_key: str
    payload_md5: str
    input_type: str
    source_filename: str
    created_at: str
    items: list[OrderItem]
