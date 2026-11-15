"""Portable Mom pause admission, persistence, and sidecar probe boundaries."""

import asyncio
import datetime
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from mom_bot.health import progress
from mom_bot.main import MomBot
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


def test_auto_kick_holds_uncertain_rows_after_resume(gate):
    history = gate.state_file.with_name("history.json")
    history.write_text(
        json.dumps(
            {
                "schema": 1,
                "pause_started_at": None,
                "last_resumed_at": None,
            }
        )
    )
    history.chmod(0o644)
    before = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=2)
    assert gate.uncertain_member_activity(before) is False

    resumed = datetime.datetime.now(datetime.UTC)
    history.write_text(
        json.dumps(
            {
                "schema": 1,
                "pause_started_at": (resumed - datetime.timedelta(minutes=2)).isoformat(),
                "last_resumed_at": resumed.isoformat(),
            }
        )
    )
    window = gate.reconciliation_window()
    assert window is not None and window[0] == resumed - datetime.timedelta(minutes=2)
    assert gate.uncertain_member_activity(before) is True
    after = resumed + datetime.timedelta(days=1)
    assert gate.uncertain_member_activity(after) is False
    history.write_text("invalid")
    assert gate.uncertain_member_activity(after) is True


@pytest.mark.asyncio
async def test_uncertain_auto_kick_does_not_dm_or_remove_tracking():
    activity = MagicMock()
    activity.joined_at = datetime.datetime.now(datetime.UTC)
    activity.member_id = 42
    activity.guild_id = 17
    scheduler = MagicMock()
    with patch(
        "mom_bot.member_activity.scheduler.maintenance_gate.uncertain_member_activity",
        return_value=True,
    ):
        await AutoKickScheduler._process_member_unchecked(scheduler, activity)
    scheduler._guild.get_member.assert_not_called()
    scheduler._service.remove_tracking.assert_not_called()


@pytest.mark.asyncio
async def test_deferred_startup_retries_only_after_resume():
    bot = MagicMock()
    bot._deferred_startup_tasks = set()
    operation = AsyncMock(side_effect=[RuntimeError("transient"), None])
    real_sleep = asyncio.sleep

    async def fast_sleep(_seconds):
        await real_sleep(0)

    with (
        patch("mom_bot.main.maintenance_gate.begin", side_effect=[False, False, True, True]),
        patch("mom_bot.main.maintenance_gate.end"),
        patch("mom_bot.main.asyncio.sleep", side_effect=fast_sleep),
    ):
        MomBot._defer_startup(bot, "probe", operation)
        task = next(iter(bot._deferred_startup_tasks))
        await asyncio.wait_for(task, timeout=2)

    assert operation.await_count == 2
    assert len(bot._deferred_startup_tasks) == 0


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
