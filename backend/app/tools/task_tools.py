from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from ..agent_runtime.gateway import HarnessBusinessGateway
from ..agent_runtime.models import PermissionLevel
from ..agent_runtime.tool_registry import ToolDefinition, ToolRegistry


class RecognitionTaskGetInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str = Field(min_length=1, max_length=100)



def register_task_tools(
    registry: ToolRegistry, gateway: HarnessBusinessGateway
) -> None:
    registry.register(ToolDefinition(
        name="recognition_task_get",
        description="读取当前站点已有持久识别任务的状态。",
        permission=PermissionLevel.READ,
        input_model=RecognitionTaskGetInput,
        handler=lambda context, args, _grant: gateway.get_task(context, args.task_id),
    ))

