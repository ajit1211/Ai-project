"""A process that only executes agent runs (docs/architecture/agents.md#workers).

By default the worker loops run inside every Runtime API process (AGENTS_WORKER_ENABLED=true).
To scale runs separately from requests, set AGENTS_WORKER_ENABLED=false for the API processes
and start any number of these:

    uv run python -m nexa_runtime.agents.worker

It builds the same application as the API (database, AI Gateway, tools, the run engine), starts
the worker loops and serves no HTTP. All processes share one queue in PostgreSQL; leases keep a
run on one worker at a time. SIGINT or SIGTERM stops it gracefully: running steps get
AGENTS_SHUTDOWN_GRACE_SECONDS to reach a checkpoint, then their runs go back to the queue.
"""

import asyncio
import contextlib
import signal

from nexa_common.logs import get_logger
from nexa_runtime.config import RuntimeSettings
from nexa_runtime.main import create_app

log = get_logger("nexa.runtime.agents.worker")


async def run_worker(
    settings: RuntimeSettings | None = None, *, stop: asyncio.Event | None = None
) -> None:
    """Execute queued runs until `stop` is set (or a termination signal arrives)."""
    configured = (settings or RuntimeSettings()).model_copy(
        update={"agents_enabled": True, "agents_worker_enabled": True}
    )
    app = create_app(configured)
    stop = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        # Not available on Windows or outside the main thread: Ctrl+C then raises
        # KeyboardInterrupt, which cancels this task and still runs the shutdown below.
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
            loop.add_signal_handler(signum, stop.set)
    async with app.router.lifespan_context(app):
        log.info(
            "agents.worker.ready",
            worker=app.state.runner.worker_id,
            concurrency=configured.agents_worker_concurrency,
        )
        await stop.wait()
    log.info("agents.worker.stopped")


def main() -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run_worker())


if __name__ == "__main__":
    main()
