"""Tests for auto-migration at bot startup (issue #94).

TDD: tests were written before the implementation.  Each test covers one
discrete behaviour of the startup migration wiring.

Design notes
------------
- We unit-test the *wiring* only — that ``run_migrations`` is called at the
  right point in ``setup_hook`` and that failures propagate loudly.  We do NOT
  run real migrations against a file-backed SQLite (that is the concern of
  ``test_alembic.py``).
- ``alembic.command.upgrade`` and ``alembic.config.Config`` are mocked so no
  disk I/O or actual migration logic runs in these tests.
- The autouse ``mock_health_server`` fixture (defined here) prevents port 8080
  collisions the same way ``test_main_wireup.py`` does.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from alembic.util.exc import CommandError

# ---------------------------------------------------------------------------
# Autouse fixture — prevent port 8080 collision across all setup_hook tests
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def mock_health_server() -> Any:
    """Patch start_health_server for every test in this module.

    Yields:
        The ``AsyncMock`` that replaced ``start_health_server``.
    """
    runner_mock = MagicMock()
    runner_mock.cleanup = AsyncMock()
    site_mock = MagicMock()
    health_mock = AsyncMock(return_value=(runner_mock, site_mock))
    with patch("mom_bot.main.start_health_server", health_mock):
        yield health_mock


# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_GUILD_ID = 999999999999999999


def _fake_load_secret(name: str) -> str:
    """Return canned KV values so no real Key Vault call is made.

    Args:
        name: Secret name requested by the bot.

    Returns:
        A canned string value for the given secret name.
    """
    values: dict[str, str] = {
        "guild-id": str(_GUILD_ID),
        "reminder-channel-name": "reminders",
        "reminder-mention-role-name": "Member",
    }
    return values[name]


# ---------------------------------------------------------------------------
# Test A — setup_hook calls run_migrations exactly once before any DB session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_setup_hook_calls_run_migrations_once() -> None:
    """setup_hook() must call run_migrations exactly once before health server
    and before the reminder background task is spawned.

    Verifies three ordering invariants that the design depends on:
    1. ``run_migrations`` is called exactly once.
    2. ``run_migrations`` is called *before* ``start_health_server``.
    3. ``run_migrations`` is called *before* ``asyncio.create_task`` spawns
       the reminder background task (which opens DB sessions).

    Call order is tracked via a shared ``call_order`` list populated by
    ``side_effect`` on each mock.  This approach works across sync mocks
    (``run_migrations``), async mocks (``start_health_server``), and the
    builtin ``asyncio.create_task``.
    """
    from mom_bot.main import MomBot, build_intents

    bot = MomBot(intents=build_intents())
    call_order: list[str] = []

    runner_mock = MagicMock()
    runner_mock.cleanup = AsyncMock()
    site_mock = MagicMock()

    async def _health_side_effect() -> tuple[MagicMock, MagicMock]:
        call_order.append("start_health_server")
        return runner_mock, site_mock

    def _migrations_side_effect() -> None:
        call_order.append("run_migrations")

    real_create_task = asyncio.create_task

    def _create_task_side_effect(coro: Any, **kwargs: Any) -> asyncio.Task[Any]:
        call_order.append("create_task")
        return real_create_task(coro, **kwargs)

    with (
        patch("mom_bot.main.load_secret", side_effect=_fake_load_secret),
        patch.object(bot.tree, "sync", new_callable=AsyncMock),
        patch(
            "mom_bot.main.run_migrations",
            side_effect=_migrations_side_effect,
        ) as mock_run_migrations,
        patch(
            "mom_bot.main.start_health_server",
            side_effect=_health_side_effect,
        ) as mock_health,
        patch("asyncio.create_task", side_effect=_create_task_side_effect),
    ):
        await bot.setup_hook()

    # --- call-count assertions ---
    mock_run_migrations.assert_called_once()
    mock_health.assert_called_once()

    # --- ordering assertions ---
    assert "run_migrations" in call_order, "run_migrations was never called"
    assert "start_health_server" in call_order, "start_health_server was never called"
    assert "create_task" in call_order, "asyncio.create_task was never called"

    mig_idx = call_order.index("run_migrations")
    health_idx = call_order.index("start_health_server")
    task_idx = call_order.index("create_task")

    assert mig_idx < health_idx, (
        f"run_migrations (pos {mig_idx}) must be called before "
        f"start_health_server (pos {health_idx}); order was {call_order}"
    )
    assert mig_idx < task_idx, (
        f"run_migrations (pos {mig_idx}) must be called before "
        f"asyncio.create_task (pos {task_idx}); order was {call_order}"
    )

    # Clean up background task.
    if bot._reminder_task is not None:
        bot._reminder_task.cancel()
        try:
            await bot._reminder_task
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# Test B — run_migrations invokes alembic.command.upgrade with correct args
# ---------------------------------------------------------------------------


def test_run_migrations_calls_alembic_upgrade_head() -> None:
    """run_migrations() must call alembic.command.upgrade(cfg, 'head').

    Verifies that the standalone ``run_migrations`` function constructs an
    ``alembic.config.Config`` from ``alembic.ini`` and passes it to
    ``alembic.command.upgrade`` with the ``'head'`` target.
    """
    from mom_bot.main import run_migrations

    with (
        patch("mom_bot.main.AlembicConfig") as mock_config_cls,
        patch("mom_bot.main.alembic_upgrade") as mock_upgrade,
    ):
        mock_cfg = MagicMock()
        mock_config_cls.return_value = mock_cfg

        run_migrations()

    mock_config_cls.assert_called_once_with("alembic.ini")
    mock_upgrade.assert_called_once_with(mock_cfg, "head")


# ---------------------------------------------------------------------------
# Test C — migration failure propagates; setup_hook must not swallow it
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_setup_hook_propagates_migration_failure() -> None:
    """If run_migrations raises, setup_hook must let the exception propagate.

    ACA will restart the container on crash; a bot with a stale schema must
    NOT silently start.  Verify that a ``RuntimeError`` from ``run_migrations``
    is NOT caught by ``setup_hook``.
    """
    from mom_bot.main import MomBot, build_intents

    bot = MomBot(intents=build_intents())

    with (
        patch("mom_bot.main.load_secret", side_effect=_fake_load_secret),
        patch.object(bot.tree, "sync", new_callable=AsyncMock),
        patch(
            "mom_bot.main.run_migrations",
            side_effect=RuntimeError("migration failed"),
        ),
        pytest.raises(RuntimeError, match="migration failed"),
    ):
        await bot.setup_hook()


# ---------------------------------------------------------------------------
# Test D — run_migrations propagates error for missing alembic.ini
# ---------------------------------------------------------------------------


def test_run_migrations_propagates_missing_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_migrations() must not silently swallow a missing alembic.ini.

    When ``MOM_BOT_ALEMBIC_CONFIG`` points at a nonexistent file, Alembic
    cannot locate a ``script_location`` and raises
    :exc:`alembic.util.exc.CommandError`.  ``run_migrations`` must let this
    propagate — a misconfigured or missing config file is a fatal startup
    error that the operator must fix, not a condition to swallow.

    This test verifies the error-propagation contract without creating any
    real file on disk; the nonexistent path is sufficient to trigger Alembic's
    ``CommandError``.

    Args:
        monkeypatch: pytest fixture used to set the env var for this test only.
    """
    monkeypatch.setenv("MOM_BOT_ALEMBIC_CONFIG", "/nonexistent/alembic.ini")

    # Force mom_bot.main to re-read MOM_BOT_ALEMBIC_CONFIG at module level
    # by reloading it with the patched env var in place.
    import importlib

    import mom_bot.main as main_mod

    importlib.reload(main_mod)

    with pytest.raises(CommandError, match="script_location"):
        main_mod.run_migrations()

    # Reload once more to restore the original _ALEMBIC_INI value so
    # subsequent tests in this session are not affected.
    monkeypatch.delenv("MOM_BOT_ALEMBIC_CONFIG", raising=False)
    importlib.reload(main_mod)
