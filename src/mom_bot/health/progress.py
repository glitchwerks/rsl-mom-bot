"""Successful scheduler-cycle progress, separate from Azure loop liveness.

State contains only fixed scheduler names and monotonic timestamps. No errors,
member identifiers, workload counts or credentials are exposed by the endpoint.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Literal

from aiohttp import web

SchedulerName = Literal["reminders", "auto_kick"]
PROGRESS_THRESHOLD_SECONDS = 180.0


@dataclass
class Progress:
    """In-process state for one scheduler task."""

    running: bool = False
    last_success: float | None = None
    failed: bool = False


_states: dict[SchedulerName, Progress] = {"reminders": Progress(), "auto_kick": Progress()}


def started(name: SchedulerName) -> None:
    """Start a new task generation; previous success cannot qualify it."""
    _states[name] = Progress(running=True)


def succeeded(name: SchedulerName) -> None:
    """Record a completed cycle, including one with no work due."""
    state = _states[name]
    state.last_success = time.monotonic()
    state.failed = False


def failed(name: SchedulerName) -> None:
    """Invalidate progress immediately without retaining exception details."""
    _states[name].failed = True


def stopped(name: SchedulerName) -> None:
    """A cancelled or exited task is unhealthy even after recent success."""
    _states[name].running = False


def snapshot() -> dict[str, object]:
    """Return sanitized progress, using monotonic age rather than wall time."""
    now = time.monotonic()
    schedulers: dict[str, dict[str, object]] = {}
    for name, state in _states.items():
        age = None if state.last_success is None else max(0.0, now - state.last_success)
        if not state.running:
            status = "stopped"
        elif state.failed:
            status = "failed"
        elif age is None:
            status = "starting"
        elif age >= PROGRESS_THRESHOLD_SECONDS:
            status = "stale"
        else:
            status = "healthy"
        schedulers[name] = {
            "healthy": status == "healthy",
            "status": status,
            "last_success_age_seconds": None if age is None else round(age, 1),
        }
    healthy = all(item["healthy"] is True for item in schedulers.values())
    return {"schema": 1, "healthy": healthy, "schedulers": schedulers}


async def progress_handler(request: web.Request) -> web.Response:
    """Serve private GET /healthz/progress; both schedulers must progress."""
    result = snapshot()
    return web.json_response(result, status=200 if result["healthy"] is True else 503)
