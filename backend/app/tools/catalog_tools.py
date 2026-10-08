from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from ..agent_runtime.gateway import HarnessBusinessGateway
from ..agent_runtime.models import PermissionLevel, ToolExecutionContext
from ..agent_runtime.tool_registry import ToolDefinition, ToolRegistry


class CatalogSearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(default="", max_length=120)
    active_only: bool = True


def _matches_query(query: str, candidate: str) -> bool:
    normalized_candidate = candidate.strip().lower()
    return bool(
        normalized_candidate
        and (query in normalized_candidate or normalized_candidate in query)
    )



def register_catalog_tools(
    registry: ToolRegistry, gateway: HarnessBusinessGateway
) -> None:
    def search(context: ToolExecutionContext, args: BaseModel, _grant):
        normalized = str(args.query).strip().lower()
        return [
            product for product in gateway.catalog_snapshot(context.site_id)
            if (not args.active_only or product["active"])
            and (
                not normalized
                or _matches_query(normalized, product["name"])
                or _matches_query(normalized, product["sku"])
                or any(_matches_query(normalized, alias) for alias in product["aliases"])
            )
        ][:20]

    registry.register(ToolDefinition(
        name="catalog_search",
        description="检索当前会话站点的生效 SKU 快照，返回正式名称、单位和目录价。",
        permission=PermissionLevel.READ,
        input_model=CatalogSearchInput,
        handler=search,
    ))

