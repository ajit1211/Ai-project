"""Success responses in the NEXA envelope, typed so the OpenAPI schema shows the real shape."""

from typing import Any

from pydantic import BaseModel


class Envelope[T](BaseModel):
    success: bool = True
    data: T
    message: str | None = None
    meta: dict[str, Any] | None = None


def ok[T](data: T, *, message: str | None = None) -> Envelope[T]:
    return Envelope[T](data=data, message=message)
