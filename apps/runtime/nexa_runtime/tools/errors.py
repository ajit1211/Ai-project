"""Stable errors of tool calling (docs/api/tools.md#errors)."""

from nexa_common.errors import ErrorCode, NexaError


class ToolNotFound(NexaError):
    """No such tool, or one the caller may not see: the same answer for both."""

    status = 404
    code = ErrorCode.TOOL_NOT_FOUND


class ApprovalPending(NexaError):
    """A reply in the conversation is paused on an action that still awaits a decision."""

    status = 409
    code = ErrorCode.TOOL_APPROVAL_PENDING


class ApprovalExpired(NexaError):
    status = 409
    code = ErrorCode.TOOL_APPROVAL_EXPIRED


class ApprovalStale(NexaError):
    """The decision names a different action (its arguments hash) than the one pending."""

    status = 409
    code = ErrorCode.TOOL_APPROVAL_STALE


class ApprovalClosed(NexaError):
    """The approval was already decided, or its reply is no longer paused."""

    status = 409
    code = ErrorCode.TOOL_APPROVAL_CLOSED
