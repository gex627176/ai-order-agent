from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SessionStatus(str, Enum):
    ACTIVE = "active"
    WAITING_INPUT = "waiting_input"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"


class PendingActionName(str, Enum):
    PROVIDE_CUSTOMER = "provide_customer"
    REVIEW_SKU = "review_sku"
    CONFIRM_ORDER = "confirm_order"


class PermissionLevel(str, Enum):
    READ = "read"
    DRAFT_WRITE = "draft_write"
    TRANSACTION_WRITE = "transaction_write"


class PendingAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: PendingActionName
    arguments: dict[str, Any] = Field(default_factory=dict)
    permission: PermissionLevel


@dataclass(frozen=True)
class ToolExecutionContext:
    session_id: str
    site_id: int
    customer_id: int | None
    current_draft_id: str | None
    session_version: int
    request_key: str | None = None


@dataclass(frozen=True)
class ApprovalGrant:
    session_id: str
    draft_id: str
    session_version: int
    draft_fingerprint: str


@dataclass(frozen=True)
class PlannerToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class PlannerTurn:
    content: str
    tool_calls: list[PlannerToolCall]
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    duration_ms: int = 0


class ToolResult(BaseModel):
    name: str
    status: str
    summary: str
    data: Any = None
