from __future__ import annotations

from .errors import ToolPermissionDenied
from .models import ApprovalGrant, PermissionLevel, ToolExecutionContext


class PermissionManager:
    def authorize(
        self,
        permission: PermissionLevel,
        context: ToolExecutionContext,
        grant: ApprovalGrant | None = None,
    ) -> None:
        if permission is not PermissionLevel.TRANSACTION_WRITE:
            return
        if (
            grant is None
            or grant.session_id != context.session_id
            or grant.draft_id != context.current_draft_id
            or grant.session_version != context.session_version
        ):
            raise ToolPermissionDenied("该操作需要与当前会话匹配的人工审批凭证")

