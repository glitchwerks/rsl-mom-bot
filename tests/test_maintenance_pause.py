"""Portable Mom pause admission, persistence, and sidecar probe boundaries."""

import asyncio
import datetime
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from mom_bot.health import progress
from mom_bot.maintenance import MaintenanceGate, MaintenancePauseMiddleware
from mom_bot.member_activity.scheduler import AutoKickScheduler
from mom_bot.reminders.scheduler import ReminderScheduler


@pytest.fixture
def gate(tmp_path):
    state = tmp_path / "state"
    state.write_bytes(b"active\n")
    state.chmod(0o644)
    return MaintenanceGate(enabled=True, state_file=str(state), owner_uid=os.geteuid())


@pytest.mark.asyncio
async def test_pause_blocks_side_effect_routes_and_keeps_auth_probe(gate):
    app = FastAPI()
    app.add_middleware(MaintenancePauseMiddleware, gate=gate)

    @app.get("/api/members")
    async def members():
        return []

    @app.post("/api/notify")
    async def notify():
        return {"sent": True}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post("/api/notify")).status_code == 200
        gate.state_file.write_bytes(b"paused\n")
        blocked = await client.post("/api/notify")
        assert blocked.status_code == 503
        assert blocked.headers["Retry-After"] == "30"
        assert (
            await client.get("/api/members", headers={"Authorization": "Bearer synthetic"})
        ).status_code == 200
        assert (await client.get("/api/members")).status_code == 503
        assert gate.snapshot() == {"paused": True, "inflight": 0}


@pytest.mark.asyncio
async def test_inflight_request_drains_after_pause(gate):
    app = FastAPI()
    app.add_middleware(MaintenancePauseMiddleware, gate=gate)
    entered, release = asyncio.Event(), asyncio.Event()

    @app.post("/api/post-message")
    async def post_message():
        entered.set()
        await release.wait()
        return {"sent": True}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        active = asyncio.create_task(client.post("/api/post-message"))
        await asyncio.wait_for(entered.wait(), timeout=2)
        gate.state_file.write_bytes(b"paused\n")
        assert gate.snapshot() == {"paused": True, "inflight": 1}
        release.set()
        assert (await active).status_code == 200
        assert gate.snapshot() == {"paused": True, "inflight": 0}


def test_missing_invalid_or_restarted_state_fails_closed(gate, tmp_path):
    gate.state_file.write_bytes(b"paused\n")
    restarted = MaintenanceGate(
        enabled=True, state_file=str(gate.state_file), owner_uid=os.geteuid()
    )
    assert restarted.snapshot() == {"paused": True, "inflight": 0}
    gate.state_file.write_bytes(b"invalid\n")
    assert gate.state() == "paused"
    gate.state_file.unlink()
    assert gate.state() == "paused"
    target = tmp_path / "target"
    target.write_bytes(b"active\n")
    gate.state_file.symlink_to(target)
    assert gate.state() == "paused"


@pytest.mark.asyncio
async def test_paused_scheduler_work_does_not_enter_mutating_body():
    reminder = MagicMock()
    reminder._process_tick_unchecked = AsyncMock()
    kick = MagicMock()
    kick._process_member_unchecked = AsyncMock()
    with patch("mom_bot.maintenance.gate.begin", return_value=False):
        assert (
            await ReminderScheduler._process_tick(
                reminder, 0, datetime.time(), datetime.date.today()
            )
            is None
        )
        await AutoKickScheduler._process_member(kick, MagicMock())
    reminder._process_tick_unchecked.assert_not_awaited()
    kick._process_member_unchecked.assert_not_awaited()


def test_progress_reports_intentional_pause_without_claiming_success():
    progress.started("reminders")
    progress.started("auto_kick")
    try:
        with patch("mom_bot.health.progress.maintenance_gate.state", return_value="paused"):
            snapshot = progress.snapshot()
        assert snapshot["healthy"] is True
        assert snapshot["schedulers"]["reminders"]["status"] == "paused"
        assert snapshot["schedulers"]["reminders"]["last_success_age_seconds"] is None
    finally:
        progress.stopped("reminders")
        progress.stopped("auto_kick")
