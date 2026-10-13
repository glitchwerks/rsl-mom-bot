"""Progress health observes completed work without Discord or external alerts."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mom_bot.health import progress
from mom_bot.member_activity.scheduler import AutoKickScheduler
from mom_bot.reminders.scheduler import ReminderScheduler


@pytest.fixture(autouse=True)
def isolated_progress(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Reset task generations and confine the legacy sentinel to test storage."""
    for name in ("reminders", "auto_kick"):
        monkeypatch.setitem(progress._states, name, progress.Progress())
    monkeypatch.setattr("mom_bot.reminders.scheduler._HEARTBEAT_PATH", tmp_path / "heartbeat")


def test_empty_completed_cycles_and_stall_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    """A loop start is insufficient; both successful cycles must be recent."""
    now = 1000.0
    monkeypatch.setattr(progress.time, "monotonic", lambda: now)
    assert progress.snapshot()["healthy"] is False
    progress.started("reminders")
    progress.started("auto_kick")
    assert progress.snapshot()["healthy"] is False
    progress.succeeded("reminders")
    assert progress.snapshot()["healthy"] is False
    progress.succeeded("auto_kick")
    assert progress.snapshot()["healthy"] is True
    now += progress.PROGRESS_THRESHOLD_SECONDS
    result = progress.snapshot()
    assert result["healthy"] is False
    assert result["schedulers"]["reminders"]["status"] == "stale"


def test_failure_recovery_stopped_and_new_generation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Failures invalidate immediately; restarting cannot inherit old success."""
    monkeypatch.setattr(progress.time, "monotonic", lambda: 100.0)
    for name in ("reminders", "auto_kick"):
        progress.started(name)
        progress.succeeded(name)
    progress.failed("reminders")
    assert progress.snapshot()["schedulers"]["reminders"]["status"] == "failed"
    progress.succeeded("reminders")
    assert progress.snapshot()["healthy"] is True
    progress.stopped("reminders")
    assert progress.snapshot()["healthy"] is False
    progress.started("reminders")
    assert progress.snapshot()["schedulers"]["reminders"]["last_success_age_seconds"] is None


@pytest.mark.asyncio
async def test_endpoint_status_and_sanitized_shape() -> None:
    """The private endpoint contains only fixed names/status and relative ages."""
    response = await progress.progress_handler(MagicMock())
    assert response.status == 503
    for name in ("reminders", "auto_kick"):
        progress.started(name)
        progress.succeeded(name)
    response = await progress.progress_handler(MagicMock())
    assert response.status == 200
    data = json.loads(response.text)
    assert set(data) == {"schema", "healthy", "schedulers"}
    assert set(data["schedulers"]) == {"reminders", "auto_kick"}
    assert all(
        set(value) == {"healthy", "status", "last_success_age_seconds"}
        for value in data["schedulers"].values()
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcomes",
    [
        [True],
        [RuntimeError("private database error")],
        [False],
        [RuntimeError("private database error"), True],
    ],
)
async def test_reminder_tick_completion_failure_and_recovery(outcomes) -> None:
    """Actual loop wiring records completion, failures and a later recovery."""
    bot = MagicMock()
    bot.is_ready.return_value = True
    scheduler = ReminderScheduler(bot, MagicMock())
    scheduler._process_tick = AsyncMock(side_effect=outcomes)
    observed = []

    async def sleep(_seconds):
        observed.append(progress.snapshot()["schedulers"]["reminders"])
        if len(observed) == len(outcomes):
            raise asyncio.CancelledError

    with patch("mom_bot.reminders.scheduler.asyncio.sleep", side_effect=sleep):
        with pytest.raises(asyncio.CancelledError):
            await scheduler.run()
    assert observed[-1]["healthy"] is (outcomes[-1] is True)
    assert "private" not in json.dumps(observed)
    assert progress.snapshot()["schedulers"]["reminders"]["status"] == "stopped"


@pytest.mark.asyncio
async def test_disconnected_gateway_does_not_qualify_as_success() -> None:
    """A live loop with a disconnected gateway remains progress-unhealthy."""
    bot = MagicMock()
    bot.is_ready.return_value = False
    scheduler = ReminderScheduler(bot, MagicMock())
    scheduler._process_tick = AsyncMock()
    with patch("mom_bot.reminders.scheduler.asyncio.sleep", side_effect=asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await scheduler.run()
    scheduler._process_tick.assert_not_awaited()
    assert progress.snapshot()["schedulers"]["reminders"]["last_success_age_seconds"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_member_sweep_query_progress_even_without_stale_members(failure: bool) -> None:
    """A successful empty query qualifies; database failure never does."""
    bot = MagicMock()
    bot.is_ready.return_value = True
    scheduler = AutoKickScheduler(bot, MagicMock(), MagicMock())
    scheduler._service = MagicMock()
    scheduler._service.list_stale.return_value = []
    if failure:
        scheduler._service.list_stale.side_effect = RuntimeError("private workload error")
    observed = []

    async def sleep(_seconds):
        observed.append(progress.snapshot()["schedulers"]["auto_kick"])
        raise asyncio.CancelledError

    with patch("mom_bot.member_activity.scheduler.asyncio.sleep", side_effect=sleep):
        with pytest.raises(asyncio.CancelledError):
            await scheduler.run()
    assert observed[0]["healthy"] is not failure
    assert "private" not in json.dumps(observed)
    assert progress.snapshot()["schedulers"]["auto_kick"]["status"] == "stopped"


@pytest.mark.asyncio
async def test_caught_dm_retry_failure_prevents_successful_cycle() -> None:
    """Continuing other DMs must not turn a failed retry into healthy progress."""
    import datetime

    bot = MagicMock()
    session_factory = MagicMock()
    session = session_factory.return_value.__enter__.return_value
    session.execute.return_value.scalars.return_value.all.return_value = []
    scheduler = ReminderScheduler(bot, session_factory, guild=MagicMock())
    scheduler._handle_member_notification = AsyncMock(side_effect=RuntimeError("private DM error"))
    with patch("mom_bot.reminders.scheduler.MemberNotificationService") as service:
        service.return_value.list_due.return_value = [MagicMock()]
        completed = await scheduler._process_tick(1, datetime.time(12), datetime.date(2026, 10, 13))
    assert completed is False
    scheduler._handle_member_notification.assert_awaited_once()
