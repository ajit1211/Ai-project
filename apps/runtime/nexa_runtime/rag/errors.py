"""Stable errors of document-grounded answers (docs/api/conversations.md#grounding)."""

from nexa_common.errors import ErrorCode, NexaError


class DocumentUnavailable(NexaError):
    """A selected document (or team) does not exist for the caller: deleted, in another
    organization, or not shared with them. The same answer for all three: no existence leak."""

    status = 404
    code = ErrorCode.DOCUMENT_UNAVAILABLE


class DocumentNotIndexed(NexaError):
    """The caller may see the document, but it is not ready to be searched yet."""

    status = 409
    code = ErrorCode.DOCUMENT_NOT_INDEXED


class InvalidScope(NexaError):
    status = 422
    code = ErrorCode.RAG_INVALID_SCOPE


class RetrievalFailed(NexaError):
    status = 503
    code = ErrorCode.RAG_RETRIEVAL_FAILED
    retryable = True


class GroundingDisabled(NexaError):
    status = 503
    code = ErrorCode.SERVICE_UNAVAILABLE
