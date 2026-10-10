from nexa_common.context import resolve_request_id
from nexa_common.errors import (
    ErrorCode,
    NexaError,
    NotFound,
    RateLimited,
    code_for_status,
    error_body,
    error_body_from,
    success_body,
)
from nexa_common.ids import new_id, new_request_id


def test_new_id_is_uuid_v7() -> None:
    assert new_id().version == 7


def test_ids_sort_by_creation_time() -> None:
    first, second = new_id(), new_id()
    assert first < second


def test_success_envelope() -> None:
    assert success_body({"id": 1}, message="Created.") == {
        "success": True,
        "data": {"id": 1},
        "message": "Created.",
    }


def test_error_envelope_uses_default_user_safe_message() -> None:
    assert error_body(code=ErrorCode.RESOURCE_NOT_FOUND, request_id="req_1") == {
        "success": False,
        "error": {
            "code": "RESOURCE_NOT_FOUND",
            "message": "The requested resource was not found.",
        },
        "request_id": "req_1",
    }


def test_domain_errors_carry_status_code_and_message() -> None:
    body = error_body_from(NotFound("Agent 1 does not exist"), request_id=None)
    assert body["error"] == {"code": "RESOURCE_NOT_FOUND", "message": "Agent 1 does not exist"}

    custom = NexaError(code=ErrorCode.EMAIL_NOT_VERIFIED, status=403)
    assert (custom.http_status, custom.error_code) == (403, ErrorCode.EMAIL_NOT_VERIFIED)
    assert custom.message == "Verify your email address before signing in."


def test_rate_limited_sets_retry_after() -> None:
    assert RateLimited(retry_after_seconds=30).headers == {"Retry-After": "30"}


def test_framework_statuses_get_stable_codes() -> None:
    assert code_for_status(405) is ErrorCode.METHOD_NOT_ALLOWED
    assert code_for_status(502) is ErrorCode.INTERNAL_ERROR
    assert code_for_status(418) is ErrorCode.BAD_REQUEST


def test_safe_request_ids_are_kept_and_unsafe_ones_replaced() -> None:
    assert resolve_request_id("trace-123") == "trace-123"
    assert resolve_request_id("has spaces").startswith("req_")
    assert resolve_request_id("x" * 129).startswith("req_")
    assert resolve_request_id(None).startswith("req_")
    assert new_request_id() != new_request_id()
