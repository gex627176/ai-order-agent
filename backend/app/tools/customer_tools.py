from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from ..agent_runtime.gateway import HarnessBusinessGateway
from ..agent_runtime.models import PermissionLevel, ToolExecutionContext
from ..agent_runtime.tool_registry import ToolDefinition, ToolRegistry


class NoArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CustomerSearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=120)


class CustomerPreferencesInput(BaseModel):
    model_config = ConfigDict(extra="forbid")



def register_customer_tools(
    registry: ToolRegistry, gateway: HarnessBusinessGateway
) -> None:
    registry.register(ToolDefinition(
        name="customer_list",
        description="列出本地客户；只在用户要求查看客户列表时使用。",
        permission=PermissionLevel.READ,
        input_model=NoArguments,
        handler=lambda _context, _args, _grant: gateway.list_customers(),
    ))

    def search(_context: ToolExecutionContext, args: BaseModel, _grant):
        query = str(args.query).strip().lower()
        return [
            customer for customer in gateway.list_customers()
            if query in customer["name"].lower()
            or query in customer.get("contact", "").lower()
        ][:20]

    registry.register(ToolDefinition(
        name="customer_search",
        description="按客户名称或联系人检索本地客户。",
        permission=PermissionLevel.READ,
        input_model=CustomerSearchInput,
        handler=search,
    ))
    registry.register(ToolDefinition(
        name="customer_preferences",
        description="读取当前会话客户的已确认偏好证据。",
        permission=PermissionLevel.READ,
        input_model=CustomerPreferencesInput,
        handler=lambda context, _args, _grant: (
            gateway.customer_preferences(context.customer_id)
            if context.customer_id is not None else []
        ),
    ))

