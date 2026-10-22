"""Stable errors of agent runs (docs/api/agents.md#errors)."""

from nexa_common.errors import ErrorCode, NexaError


class AgentUnavailable(NexaError):
    """No such agent for the caller, or one that is disabled or deleted."""

    status = 404
    code = ErrorCode.AGENT_UNAVAILABLE


class RunNotActive(NexaError):
    """The run is not in a state that allows this (pause a finished run, resume a running one)."""

    status = 409
    code = ErrorCode.RUN_NOT_ACTIVE


class RunLimitReached(NexaError):
    """Too many runs at once for this user or organization."""

    status = 429
    code = ErrorCode.RUN_LIMIT_REACHED


class AgentsDisabled(NexaError):
    status = 503
    code = ErrorCode.SERVICE_UNAVAILABLE
