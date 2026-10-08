from __future__ import annotations


class AgentHarnessError(Exception):
    code = "agent_harness_error"


class AgentSessionNotFound(AgentHarnessError):
    code = "session_not_found"


class AgentStateConflict(AgentHarnessError):
    code = "session_state_conflict"


class AgentIdempotencyConflict(AgentStateConflict):
    code = "idempotency_conflict"


class ToolNotFound(AgentHarnessError):
    code = "tool_not_found"


class ToolPermissionDenied(AgentHarnessError):
    code = "tool_permission_denied"


class ToolArgumentsInvalid(AgentHarnessError):
    code = "tool_arguments_invalid"


class ToolExecutionFailed(AgentHarnessError):
    code = "tool_execution_failed"

    def __init__(self, tool_name: str, message: str):
        super().__init__(message)
        self.tool_name = tool_name


class SkillLoadError(AgentHarnessError):
    code = "skill_load_error"


class PlannerError(AgentHarnessError):
    code = "planner_error"

