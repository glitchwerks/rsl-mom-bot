"""Opt-in portable maintenance admission shared by Mom's work producers.

Azure and development deployments leave enforcement disabled. A portable
deployment mounts a root-owned nonsecret control directory read-only. Missing
or malformed state fails closed, including after container recreation.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from threading import Lock

import discord
from discord import app_commands
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


class MaintenanceGate:
    """Count admitted work and reject new work while the host is paused."""

    def __init__(self, *, enabled: bool, state_file: str, owner_uid: int = 0) -> None:
        self.enabled = enabled
        self.state_file = Path(state_file) if state_file else None
        self.owner_uid = owner_uid
        self._lock = Lock()
        self._inflight = 0

    def state(self) -> str:
        if not self.enabled:
            return "active"
        path = self.state_file
        if path is None or not path.is_absolute():
            return "paused"
        try:
            if any(part.is_symlink() for part in (path, *path.parents)):
                return "paused"
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as source:
                info = os.fstat(source.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != self.owner_uid
                    or info.st_mode & 0o022
                    or info.st_size > 16
                ):
                    return "paused"
                return "active" if source.read(17) == b"active\n" else "paused"
        except OSError:
            return "paused"

    def _pause_history(self) -> tuple[datetime | None, datetime | None] | None:
        """Read trusted UTC boundaries; None means missing or malformed history."""
        path = self.state_file
        if path is None or not path.is_absolute():
            return None
        history_path = path.with_name("history.json")
        try:
            if any(part.is_symlink() for part in (history_path, *history_path.parents)):
                return None
            fd = os.open(history_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as source:
                info = os.fstat(source.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != self.owner_uid
                    or info.st_mode & 0o022
                    or info.st_size > 256
                ):
                    return None
                history = json.load(source)
            if (
                not isinstance(history, dict)
                or set(history) != {"schema", "pause_started_at", "last_resumed_at"}
                or history["schema"] != 1
            ):
                return None
            values: list[datetime | None] = []
            for field in ("pause_started_at", "last_resumed_at"):
                raw = history[field]
                if raw is None:
                    values.append(None)
                    continue
                if not isinstance(raw, str):
                    return None
                parsed = datetime.fromisoformat(raw)
                if parsed.tzinfo != UTC:
                    return None
                values.append(parsed)
            started, resumed = values
            if started is None and resumed is not None:
                return None
            return started, resumed
        except (OSError, ValueError, TypeError):
            return None

    def reconciliation_window(self) -> tuple[datetime, datetime] | None:
        """Return the durable pause interval only while admission is open."""
        if not self.enabled or self.state() != "active" or self.state_file is None:
            return None
        history = self._pause_history()
        if history is None or history[0] is None or history[1] is None:
            return None
        try:
            active_since = datetime.fromtimestamp(self.state_file.stat().st_mtime, UTC)
        except OSError:
            return None
        return history[0], max(history[1], active_since)

    def uncertain_member_activity(self, joined_at: datetime) -> bool:
        """Hold auto-kicks whose first-message record may have a gateway gap."""
        if not self.enabled:
            return False
        if self.state() != "active":
            return True
        history = self._pause_history()
        if history == (None, None):
            return False
        window = self.reconciliation_window()
        if window is None:
            return True
        joined = (
            joined_at.replace(tzinfo=UTC) if joined_at.tzinfo is None else joined_at.astimezone(UTC)
        )
        return joined <= window[1]

    def begin(self) -> bool:
        if self.state() != "active":
            return False
        with self._lock:
            if self.state() != "active":
                return False
            self._inflight += 1
        return True

    def end(self) -> None:
        with self._lock:
            self._inflight -= 1

    @contextmanager
    def admitted(self) -> Iterator[bool]:
        allowed = self.begin()
        try:
            yield allowed
        finally:
            if allowed:
                self.end()

    def snapshot(self) -> dict[str, bool | int]:
        with self._lock:
            count = self._inflight
        return {"paused": self.state() != "active", "inflight": count}


_enforcement = os.environ.get("MOM_BOT_MAINTENANCE_ENFORCED", "false").lower()
if _enforcement not in {"true", "false"}:
    raise ValueError("Invalid maintenance enforcement setting")

gate = MaintenanceGate(
    enabled=_enforcement == "true",
    state_file=os.environ.get("MOM_BOT_MAINTENANCE_STATE_FILE", ""),
)


def _probe_allowed(scope: Scope) -> bool:
    if scope.get("method") != "GET":
        return scope.get("method") == "OPTIONS"
    path = scope.get("path")
    if path in {"/api/health", "/api/version", "/api/maintenance/status"}:
        return True
    if path != "/api/members":
        return False
    headers = dict(scope.get("headers", []))
    return bool(headers.get(b"authorization", b"").lower().startswith(b"bearer "))


class MaintenancePauseMiddleware:
    """Reject sidecar work while preserving narrow read-only auth probes."""

    def __init__(self, app: ASGIApp, gate: MaintenanceGate) -> None:
        self.app = app
        self.gate = gate

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or _probe_allowed(scope):
            await self.app(scope, receive, send)
            return
        if not self.gate.begin():
            await JSONResponse(
                {"detail": "Maintenance pause active"},
                status_code=503,
                headers={"Retry-After": "30"},
            )(scope, receive, send)
            return
        try:
            await self.app(scope, receive, send)
        finally:
            self.gate.end()


class MaintenanceCommandTree(app_commands.CommandTree):
    """Hold one admission count across each slash-command invocation."""

    async def _call(self, interaction: discord.Interaction) -> None:
        with gate.admitted() as allowed:
            if not allowed:
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        "Maintenance is in progress. Please try again shortly.",
                        ephemeral=True,
                    )
                return
            await super()._call(interaction)


def guarded_gateway[**P](
    handler: Callable[P, Awaitable[None]],
) -> Callable[P, Awaitable[None]]:
    """Count a gateway handler until its database and Discord work completes."""

    @wraps(handler)
    async def wrapped(*args: P.args, **kwargs: P.kwargs) -> None:
        with gate.admitted() as allowed:
            if allowed:
                await handler(*args, **kwargs)

    return wrapped
