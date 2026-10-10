"""The SSE response closes the stream it reads from, even when it never read from it."""

from collections.abc import AsyncIterator

import pytest
from starlette.requests import ClientDisconnect
from starlette.types import Message, Scope

from nexa_runtime.ai.router import SSEResponse

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class Source:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


async def receive() -> Message:
    return {"type": "http.disconnect"}


async def gone(message: Message) -> None:
    raise OSError("the client has gone")  # how an ASGI 2.4 server reports it


async def test_the_source_is_closed_when_the_client_left_before_the_response() -> None:
    source = Source()
    read = False

    async def body() -> AsyncIterator[bytes]:
        nonlocal read
        read = True
        yield b"event: message_start\n\n"

    response = SSEResponse(body(), source=source, media_type="text/event-stream")
    scope: Scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"}}
    with pytest.raises(ClientDisconnect):
        await response(scope, receive, gone)
    assert read is False  # the encoder never started, so only closing the source reaches it
    assert source.closed is True
