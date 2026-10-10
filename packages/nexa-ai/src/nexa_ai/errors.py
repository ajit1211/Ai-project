"""The AI error hierarchy. Adapters raise these; nothing else escapes an adapter.

Every class fixes the NEXA error code and HTTP status, whether the same model is worth
retrying, and whether the next candidate model may be tried instead (the fallback matrix in
docs/architecture/ai-gateway.md). The provider's own message, status and request ID are kept
for logs and never shown to a client.
"""

from typing import ClassVar

from nexa_common.errors import ErrorCode, NexaError

_PROVIDER_MESSAGE_LIMIT = 300


class AIError(NexaError):
    status: ClassVar[int] = 500
    code: ClassVar[ErrorCode] = ErrorCode.AI_INTERNAL_ERROR
    retryable: ClassVar[bool] = False
    fallback_eligible: ClassVar[bool] = False
    # The fault is the provider account (key, billing), so every model at that provider will
    # fail the same way: the gateway skips them and tries other providers only.
    provider_wide: ClassVar[bool] = False

    def __init__(
        self,
        message: str | None = None,
        *,
        provider: str | None = None,
        provider_model: str | None = None,
        provider_status: int | None = None,
        provider_message: str | None = None,
        provider_request_id: str | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        headers = {}
        if retry_after_seconds is not None:
            headers["Retry-After"] = str(max(1, int(retry_after_seconds + 0.999)))
        super().__init__(message, headers=headers)
        self.provider = provider
        self.provider_model = provider_model
        self.provider_status = provider_status
        self.provider_message = (provider_message or "")[:_PROVIDER_MESSAGE_LIMIT]
        self.provider_request_id = provider_request_id
        self.retry_after_seconds = retry_after_seconds

    def log_fields(self) -> dict[str, object]:
        """What the log line may carry: codes, statuses and a truncated provider message."""
        return {
            "error_code": self.error_code.value,
            "error_class": type(self).__name__,
            "provider": self.provider,
            "provider_model": self.provider_model,
            "provider_status": self.provider_status,
            "provider_message": self.provider_message or None,
            "provider_request_id": self.provider_request_id,
            "retryable": self.retryable,
            "fallback_eligible": self.fallback_eligible,
        }


class AIInvalidRequest(AIError):
    """The request is malformed or asks for something this model or phase cannot do."""

    status = 422
    code = ErrorCode.AI_INVALID_REQUEST


class AIUnsupportedByProvider(AIInvalidRequest):
    """The provider rejected a parameter the gateway produced (an unsupported schema keyword,
    an option this model does not take). Not the caller's fault: another model may accept
    it, so fallback is allowed, and the adapter mapping deserves a look."""

    fallback_eligible = True


class AIModelUnavailable(AIError):
    """No usable model: unknown alias, inactive, not allowed for the organization, or the
    provider does not know the model. From a provider this is fallback-eligible."""

    status = 422
    code = ErrorCode.AI_MODEL_UNAVAILABLE
    fallback_eligible = True


class AIContextTooLarge(AIError):
    """Not retried. When a provider reports it, the gateway still tries candidates with a
    strictly larger context window."""

    status = 413
    code = ErrorCode.AI_CONTEXT_TOO_LARGE


class AIContentRejected(AIError):
    status = 422
    code = ErrorCode.AI_CONTENT_REJECTED


class AIQuotaExceeded(AIError):
    """A NEXA limit for the organization or user, not the provider's."""

    status = 429
    code = ErrorCode.AI_QUOTA_EXCEEDED


class AIRateLimited(AIError):
    """The provider is rate limiting NEXA. Worth a short wait, then another candidate."""

    status = 429
    code = ErrorCode.AI_RATE_LIMIT
    retryable = True
    fallback_eligible = True


class AITimeout(AIError):
    """Retrying the same model would double the wait; the next candidate is tried instead."""

    status = 504
    code = ErrorCode.AI_TIMEOUT
    fallback_eligible = True


class AIAuthenticationError(AIError):
    """The provider rejected NEXA's credentials or account (bad key, no credit, spend limit):
    a configuration fault, never the user's."""

    status = 502
    code = ErrorCode.AI_AUTHENTICATION_ERROR
    fallback_eligible = True
    provider_wide = True


class AIProviderError(AIError):
    """A provider-side failure (5xx, overloaded, connection dropped)."""

    status = 503
    code = ErrorCode.AI_PROVIDER_UNAVAILABLE
    retryable = True
    fallback_eligible = True


class AIOutputInvalid(AIError):
    """Structured output did not match the schema, even after one repair attempt."""

    status = 502
    code = ErrorCode.AI_OUTPUT_INVALID


class AIInternalError(AIError):
    """A bug in the gateway or an adapter, reported without details."""

    status = 500
    code = ErrorCode.AI_INTERNAL_ERROR
