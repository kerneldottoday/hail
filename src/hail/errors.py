"""Errors and the exit codes they map to: the contract scripts and agents rely on (see `hail help agents`)."""

from __future__ import annotations

from types import MappingProxyType

EXIT = MappingProxyType(
    {
        "ok": 0,
        "error": 1,  # anything else; stderr says what
        "usage": 2,  # the command line could not be read
        "config": 3,  # no agents file, or it is invalid
        "agent_error": 4,  # the agent answered but exited with an error (its reply is still printed)
        "not_found": 5,  # no such agent, skill, task or thread
        "agent_unusable": 6,  # the agent could not run or returned nothing; retrying will not help
        "refused": 7,  # a guard rail said no (policy, hops, thread budget, stopped thread)
        "temporary": 75,  # timeout, SSH could not connect, rate limit, daily cap: may work later
    }
)


class HailError(RuntimeError):
    """The agent could not be reached or returned nothing usable."""

    exit_code = EXIT["error"]


class UsageError(HailError):
    exit_code = EXIT["usage"]


class NotFound(HailError):
    exit_code = EXIT["not_found"]


class AgentUnusable(HailError):
    exit_code = EXIT["agent_unusable"]


class Refused(HailError):
    """The call was blocked by a guard rail (hops, budget, policy, stop)."""

    exit_code = EXIT["refused"]


class Throttled(Refused):
    """A guard rail that lifts with time (rate limit, daily cap)."""

    exit_code = EXIT["temporary"]


class Temporary(HailError):
    exit_code = EXIT["temporary"]


class Reason(str):
    """Why a budget refused a call; ``temporary`` tells whether waiting will help."""

    temporary = False


class LaterReason(Reason):
    temporary = True
