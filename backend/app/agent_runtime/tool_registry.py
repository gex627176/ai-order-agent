from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from pydantic import BaseModel, ValidationError

from .errors import (
    ToolArgumentsInvalid,
    ToolExecutionFailed,
    ToolNotFound,
)
from .models import (
    ApprovalGrant,
    PermissionLevel,
    ToolExecutionContext,
    ToolResult,
)
from .permission import PermissionManager


ToolHandler = Callable[[ToolExecutionContext, BaseModel, ApprovalGrant | None], Any]


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    permission: PermissionLevel
    input_model: type[BaseModel]
    handler: ToolHandler
    model_visible: bool = True


class ToolRegistry:
    def __init__(self, permission_manager: PermissionManager):
        self._permission_manager = permission_manager
        self._tools: dict[str, ToolDefinition] = {}

    def register(self, definition: ToolDefinition) -> None:
        if definition.name in self._tools:
            raise ValueError(f"工具已注册: {definition.name}")
        self._tools[definition.name] = definition

    def definition(self, name: str) -> ToolDefinition:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ToolNotFound(f"工具不存在: {name}") from exc

    def model_tools(self, allowed_names: set[str] | None = None) -> list[dict[str, Any]]:
        definitions = [
            definition for definition in self._tools.values()
            if definition.model_visible
            and (allowed_names is None or definition.name in allowed_names)
        ]
        return [
            {
                "type": "function",
                "function": {
                    "name": definition.name,
                    "description": definition.description,
                    "parameters": definition.input_model.model_json_schema(),
                },
            }
            for definition in definitions
        ]

    def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
        grant: ApprovalGrant | None = None,
    ) -> ToolResult:
        definition = self.definition(name)
        self._permission_manager.authorize(definition.permission, context, grant)
        try:
            validated = definition.input_model.model_validate(arguments)
        except ValidationError as exc:
            raise ToolArgumentsInvalid(f"工具 {name} 参数无效") from exc
        try:
            data = definition.handler(context, validated, grant)
        except (LookupError, ValueError, PermissionError) as exc:
            raise ToolExecutionFailed(name, str(exc)) from exc
        except Exception as exc:
            raise ToolExecutionFailed(name, "工具暂时不可用，请稍后重试") from exc
        return ToolResult(
            name=name,
            status="completed",
            summary=_summarize(name, data),
            data=data,
        )


def _summarize(name: str, data: Any) -> str:
    if isinstance(data, list):
        return f"{name} 返回 {len(data)} 条结果"
    if isinstance(data, dict):
        identifier = data.get("id") or data.get("order_no") or ""
        return f"{name} 执行完成" + (f"，结果 {identifier}" if identifier else "")
    return f"{name} 执行完成"
