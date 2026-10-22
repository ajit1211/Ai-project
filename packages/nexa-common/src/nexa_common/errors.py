"""Domain errors and the API response envelope shared by every NEXA service (doc 18.1, 27.7).

Every `/api/v1` response uses one envelope:

    success: {"success": true,  "data": ..., "message": "...", "meta": {...}}
    error:   {"success": false, "error": {"code": "...", "message": "...", "details": [...]},
              "request_id": "req_..."}

HTTP status codes stay meaningful; `code` is the stable, machine-readable value clients branch on.
Services raise `NexaError` subclasses, and each framework translates them exactly once, at its
boundary. Messages are always safe to show to an end user; internals go to logs only.
"""

from enum import StrEnum
from typing import Any, ClassVar, TypedDict


class ErrorCode(StrEnum):
    BAD_REQUEST = "BAD_REQUEST"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    AUTH_INVALID_CREDENTIALS = "AUTH_INVALID_CREDENTIALS"
    AUTH_TOKEN_INVALID = "AUTH_TOKEN_INVALID"  # noqa: S105 - an error code, not a secret
    AUTH_TOKEN_EXPIRED = "AUTH_TOKEN_EXPIRED"  # noqa: S105 - an error code, not a secret
    AUTH_SESSION_REVOKED = "AUTH_SESSION_REVOKED"
    EMAIL_NOT_VERIFIED = "EMAIL_NOT_VERIFIED"
    TOKEN_INVALID = "TOKEN_INVALID"  # noqa: S105 - an error code, not a secret
    TOKEN_EXPIRED = "TOKEN_EXPIRED"  # noqa: S105 - an error code, not a secret
    PERMISSION_DENIED = "PERMISSION_DENIED"
    RESOURCE_NOT_FOUND = "RESOURCE_NOT_FOUND"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
    CONFLICT = "CONFLICT"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    UNSUPPORTED_MEDIA_TYPE = "UNSUPPORTED_MEDIA_TYPE"
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
    GENERATION_IN_PROGRESS = "GENERATION_IN_PROGRESS"
    # Stored on a reply whose generation died with its process (never an HTTP error).
    GENERATION_INTERRUPTED = "GENERATION_INTERRUPTED"
    RATE_LIMITED = "RATE_LIMITED"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    # AI Gateway (docs/architecture/ai-gateway.md). Provider failures are mapped to these.
    AI_INVALID_REQUEST = "AI_INVALID_REQUEST"
    AI_MODEL_UNAVAILABLE = "AI_MODEL_UNAVAILABLE"
    AI_CONTEXT_TOO_LARGE = "AI_CONTEXT_TOO_LARGE"
    AI_CONTENT_REJECTED = "AI_CONTENT_REJECTED"
    AI_QUOTA_EXCEEDED = "AI_QUOTA_EXCEEDED"
    AI_RATE_LIMIT = "AI_RATE_LIMIT"
    AI_TIMEOUT = "AI_TIMEOUT"
    AI_AUTHENTICATION_ERROR = "AI_AUTHENTICATION_ERROR"
    AI_PROVIDER_UNAVAILABLE = "AI_PROVIDER_UNAVAILABLE"
    AI_OUTPUT_INVALID = "AI_OUTPUT_INVALID"
    AI_EMPTY_RESPONSE = "AI_EMPTY_RESPONSE"
    AI_INTERNAL_ERROR = "AI_INTERNAL_ERROR"
    # Document-grounded answers (Phase 08, docs/architecture/rag.md).
    DOCUMENT_UNAVAILABLE = "DOCUMENT_UNAVAILABLE"
    DOCUMENT_NOT_INDEXED = "DOCUMENT_NOT_INDEXED"
    RAG_INVALID_SCOPE = "RAG_INVALID_SCOPE"
    RAG_RETRIEVAL_FAILED = "RAG_RETRIEVAL_FAILED"
    # AI tool calling (Phase 10, docs/architecture/tools.md).
    TOOL_NOT_FOUND = "TOOL_NOT_FOUND"
    TOOL_APPROVAL_PENDING = "TOOL_APPROVAL_PENDING"
    TOOL_APPROVAL_EXPIRED = "TOOL_APPROVAL_EXPIRED"
    TOOL_APPROVAL_STALE = "TOOL_APPROVAL_STALE"
    TOOL_APPROVAL_CLOSED = "TOOL_APPROVAL_CLOSED"
    # AI agents (Phase 11, docs/architecture/agents.md).
    AGENT_UNAVAILABLE = "AGENT_UNAVAILABLE"
    RUN_NOT_ACTIVE = "RUN_NOT_ACTIVE"
    RUN_LIMIT_REACHED = "RUN_LIMIT_REACHED"
    TOOL_NOT_ALLOWED = "TOOL_NOT_ALLOWED"


DEFAULT_MESSAGES: dict[ErrorCode, str] = {
    ErrorCode.BAD_REQUEST: "The request could not be processed.",
    ErrorCode.VALIDATION_FAILED: "Some fields are invalid.",
    ErrorCode.AUTH_REQUIRED: "Authentication is required.",
    ErrorCode.AUTH_INVALID_CREDENTIALS: "Invalid email or password.",
    ErrorCode.AUTH_TOKEN_INVALID: "The access token is invalid.",
    ErrorCode.AUTH_TOKEN_EXPIRED: "The access token has expired.",
    ErrorCode.AUTH_SESSION_REVOKED: "This session has ended. Please sign in again.",
    ErrorCode.EMAIL_NOT_VERIFIED: "Verify your email address before signing in.",
    ErrorCode.TOKEN_INVALID: "This link is invalid or has already been used.",
    ErrorCode.TOKEN_EXPIRED: "This link has expired.",
    ErrorCode.PERMISSION_DENIED: "You do not have permission to perform this action.",
    ErrorCode.RESOURCE_NOT_FOUND: "The requested resource was not found.",
    ErrorCode.METHOD_NOT_ALLOWED: "This method is not allowed for this endpoint.",
    ErrorCode.CONFLICT: "The request conflicts with the current state of the resource.",
    ErrorCode.PAYLOAD_TOO_LARGE: "The request is too large.",
    ErrorCode.UNSUPPORTED_MEDIA_TYPE: "This file type is not supported.",
    ErrorCode.QUOTA_EXCEEDED: "A storage or usage quota has been reached.",
    ErrorCode.GENERATION_IN_PROGRESS: "A reply is already being generated in this conversation.",
    ErrorCode.GENERATION_INTERRUPTED: "The reply was interrupted before it finished. Please try again.",
    ErrorCode.RATE_LIMITED: "Too many requests. Please try again later.",
    ErrorCode.INTERNAL_ERROR: "Something went wrong on our side.",
    ErrorCode.SERVICE_UNAVAILABLE: "The service is temporarily unavailable.",
    ErrorCode.AI_INVALID_REQUEST: "The AI request is invalid.",
    ErrorCode.AI_MODEL_UNAVAILABLE: "That model is not available.",
    ErrorCode.AI_CONTEXT_TOO_LARGE: "The input is too long for this model.",
    ErrorCode.AI_CONTENT_REJECTED: "The AI provider declined to answer this request.",
    ErrorCode.AI_QUOTA_EXCEEDED: "Your AI usage limit has been reached. Please try again later.",
    ErrorCode.AI_RATE_LIMIT: "The AI provider is busy. Please try again in a moment.",
    ErrorCode.AI_TIMEOUT: "The AI provider took too long to answer. Please try again.",
    ErrorCode.AI_AUTHENTICATION_ERROR: "The AI provider could not be reached. Please try again later.",
    ErrorCode.AI_PROVIDER_UNAVAILABLE: "The AI provider is temporarily unavailable.",
    ErrorCode.AI_OUTPUT_INVALID: "The AI answer did not match the requested format.",
    ErrorCode.AI_EMPTY_RESPONSE: "The AI model returned an empty answer. Please try again.",
    ErrorCode.AI_INTERNAL_ERROR: "Something went wrong while generating the answer.",
    ErrorCode.DOCUMENT_UNAVAILABLE: "One or more of the selected documents are not available.",
    ErrorCode.DOCUMENT_NOT_INDEXED: (
        "One or more of the selected documents are still being prepared for search."
    ),
    ErrorCode.RAG_INVALID_SCOPE: "The document selection is not valid.",
    ErrorCode.RAG_RETRIEVAL_FAILED: "Searching the documents failed. Please try again.",
    ErrorCode.TOOL_NOT_FOUND: "That tool is not available.",
    ErrorCode.TOOL_APPROVAL_PENDING: (
        "An action in this conversation is waiting for your approval. Approve or reject it first."
    ),
    ErrorCode.TOOL_APPROVAL_EXPIRED: "This action was not approved in time and has expired.",
    ErrorCode.TOOL_APPROVAL_STALE: (
        "The action changed since it was shown to you. Review it again before deciding."
    ),
    ErrorCode.TOOL_APPROVAL_CLOSED: "This action is no longer waiting for a decision.",
    ErrorCode.AGENT_UNAVAILABLE: "That agent is not available.",
    ErrorCode.RUN_NOT_ACTIVE: "The task is not in a state that allows this.",
    ErrorCode.RUN_LIMIT_REACHED: "Too many tasks are in progress. Please wait for one to finish.",
    ErrorCode.TOOL_NOT_ALLOWED: "That tool cannot be given to this agent.",
}


class FieldError(TypedDict):
    field: str
    code: str
    message: str


class NexaError(Exception):
    """An error that becomes an API error response.

    Subclasses fix the HTTP status and code; any instance may override the code, status and
    message, e.g. `NexaError(code=ErrorCode.EMAIL_NOT_VERIFIED, status=403)`.
    """

    status: ClassVar[int] = 500
    code: ClassVar[ErrorCode] = ErrorCode.INTERNAL_ERROR
    retryable: ClassVar[bool] = False

    def __init__(
        self,
        message: str | None = None,
        *,
        code: ErrorCode | None = None,
        status: int | None = None,
        details: list[FieldError] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.error_code = code or self.code
        self.http_status = status or self.status
        self.message = message or DEFAULT_MESSAGES[self.error_code]
        self.details = details or []
        self.headers = headers or {}
        super().__init__(self.message)


class ValidationFailed(NexaError):
    status = 422
    code = ErrorCode.VALIDATION_FAILED


class AuthRequired(NexaError):
    status = 401
    code = ErrorCode.AUTH_REQUIRED


class PermissionDenied(NexaError):
    status = 403
    code = ErrorCode.PERMISSION_DENIED


class NotFound(NexaError):
    status = 404
    code = ErrorCode.RESOURCE_NOT_FOUND


class Conflict(NexaError):
    status = 409
    code = ErrorCode.CONFLICT


class PayloadTooLarge(NexaError):
    status = 413
    code = ErrorCode.PAYLOAD_TOO_LARGE


class UnsupportedMediaType(NexaError):
    status = 415
    code = ErrorCode.UNSUPPORTED_MEDIA_TYPE


class QuotaExceeded(NexaError):
    status = 402
    code = ErrorCode.QUOTA_EXCEEDED


class RateLimited(NexaError):
    status = 429
    code = ErrorCode.RATE_LIMITED
    retryable = True

    def __init__(self, retry_after_seconds: int, message: str | None = None) -> None:
        super().__init__(message, headers={"Retry-After": str(max(1, retry_after_seconds))})
        self.retry_after_seconds = retry_after_seconds


class ServiceUnavailable(NexaError):
    status = 503
    code = ErrorCode.SERVICE_UNAVAILABLE
    retryable = True


_STATUS_CODES: dict[int, ErrorCode] = {
    400: ErrorCode.BAD_REQUEST,
    401: ErrorCode.AUTH_REQUIRED,
    403: ErrorCode.PERMISSION_DENIED,
    404: ErrorCode.RESOURCE_NOT_FOUND,
    405: ErrorCode.METHOD_NOT_ALLOWED,
    409: ErrorCode.CONFLICT,
    413: ErrorCode.PAYLOAD_TOO_LARGE,
    415: ErrorCode.UNSUPPORTED_MEDIA_TYPE,
    422: ErrorCode.VALIDATION_FAILED,
    429: ErrorCode.RATE_LIMITED,
    503: ErrorCode.SERVICE_UNAVAILABLE,
}


def code_for_status(status: int) -> ErrorCode:
    """The error code used when a framework raises a bare HTTP status."""
    if status in _STATUS_CODES:
        return _STATUS_CODES[status]
    return ErrorCode.INTERNAL_ERROR if status >= 500 else ErrorCode.BAD_REQUEST


def error_body(
    *,
    code: ErrorCode,
    message: str | None = None,
    request_id: str | None,
    details: list[FieldError] | None = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code.value, "message": message or DEFAULT_MESSAGES[code]}
    if details:
        error["details"] = details
    return {"success": False, "error": error, "request_id": request_id}


def error_body_from(error: NexaError, *, request_id: str | None) -> dict[str, Any]:
    return error_body(
        code=error.error_code,
        message=error.message,
        request_id=request_id,
        details=error.details,
    )


def success_body(
    data: Any = None, *, message: str | None = None, meta: dict[str, Any] | None = None
) -> dict[str, Any]:
    body: dict[str, Any] = {"success": True, "data": data}
    if message:
        body["message"] = message
    if meta:
        body["meta"] = meta
    return body
