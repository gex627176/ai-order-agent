from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from ..agent_runtime.gateway import HarnessBusinessGateway
from ..agent_runtime.models import ApprovalGrant, PermissionLevel, ToolExecutionContext
from ..agent_runtime.tool_registry import ToolDefinition, ToolRegistry


class DraftRecognizeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=5000)


class DraftGetInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DraftUpdateNoteInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    line_no: int = Field(ge=1, le=200)
    note: str = Field(min_length=1, max_length=500)


class DraftConfirmInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str | None = Field(default=None, max_length=200)



def register_draft_tools(
    registry: ToolRegistry, gateway: HarnessBusinessGateway
) -> None:
    registry.register(ToolDefinition(
        name="draft_recognize",
        description="用现有确定性录单工作流把订单原文生成待审核草稿。",
        permission=PermissionLevel.DRAFT_WRITE,
        input_model=DraftRecognizeInput,
        handler=lambda context, args, _grant: gateway.recognize_draft(
            context, args.text
        ),
    ))
    registry.register(ToolDefinition(
        name="draft_get",
        description="重新读取当前会话草稿，用于人工修正后的状态检查。",
        permission=PermissionLevel.READ,
        input_model=DraftGetInput,
        handler=lambda context, _args, _grant: gateway.get_current_draft(context),
    ))
    registry.register(ToolDefinition(
        name="draft_update_note",
        description=(
            "更新当前会话草稿中指定商品行的备注。等待审批时用户要求修改备注，"
            "应先调用 draft_get 核对 line_no，再调用本工具；本工具不会创建订单。"
        ),
        permission=PermissionLevel.DRAFT_WRITE,
        input_model=DraftUpdateNoteInput,
        handler=lambda context, args, _grant: gateway.update_current_draft_note(
            context, args.line_no, args.note
        ),
        model_visible=True,
    ))

    def confirm(
        context: ToolExecutionContext,
        args: BaseModel,
        grant: ApprovalGrant | None,
    ):
        if grant is None:
            raise PermissionError("缺少人工审批凭证")
        return gateway.confirm_draft(context, grant, args.idempotency_key)

    registry.register(ToolDefinition(
        name="draft_confirm",
        description="使用人工审批凭证确认当前草稿。",
        permission=PermissionLevel.TRANSACTION_WRITE,
        input_model=DraftConfirmInput,
        handler=confirm,
        model_visible=False,
    ))

