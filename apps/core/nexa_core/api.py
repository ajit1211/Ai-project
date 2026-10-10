"""Success responses in the NEXA envelope. Errors are produced by `nexa_core.exceptions`."""

from typing import Any

from rest_framework.response import Response

from nexa_common.errors import success_body


def ok(
    data: Any = None,
    *,
    message: str | None = None,
    status: int = 200,
    meta: dict[str, Any] | None = None,
) -> Response:
    return Response(success_body(data, message=message, meta=meta), status=status)
