"""Identifiers. NEXA uses time-ordered UUIDv7 keys, generated in the application (doc 16.1)."""

import uuid

from uuid_utils.compat import uuid7


def new_id() -> uuid.UUID:
    """Return a new UUIDv7 as a standard-library `uuid.UUID`."""
    return uuid7()


def new_request_id() -> str:
    """Return a request ID such as `req_0192f3a1...`, sortable by creation time."""
    return f"req_{uuid7().hex}"
