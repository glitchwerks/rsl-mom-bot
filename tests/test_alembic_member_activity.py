"""Alembic schema tests for the ``member_activity`` migration (#300).

TDD: written before the migration file exists. Mirrors the
``TestRemindersSchema`` pattern in ``tests/test_alembic.py`` (own file,
not appended there, so this test-authoring pass does not edit a test file
it did not write — see that module for the shared ``_make_alembic_config`` /
``_get_table_names`` / ``_get_column_names`` helpers this file duplicates
locally, on purpose, to stay self-contained).

Unlike the ORM-level tests in ``tests/member_activity/test_models.py``
(which use ``Base.metadata.create_all`` and never touch the migration),
these tests run ``alembic upgrade head`` against a real SQLite file so a
missing or broken ``migrations/versions/*member_activity*.py`` file is
caught — issue #300 explicitly calls for a migration, and CI applies
migrations (not ``create_all``) before deploy.
"""

from __future__ import annotations

import logging.config
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ALEMBIC_INI = str(_REPO_ROOT / "alembic.ini")


# ---------------------------------------------------------------------------
# Module-scoped autouse fixture: prevent alembic from disabling loggers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True, scope="module")
def _no_disable_existing_loggers() -> Any:
    """Force ``disable_existing_loggers=False`` on every ``fileConfig`` call.

    ``alembic.command.upgrade``/``downgrade`` invoke ``migrations/env.py``,
    which calls ``logging.config.fileConfig(alembic_ini)``.  The stdlib
    default for ``disable_existing_loggers`` is ``True``, which sets
    ``.disabled = True`` on every logger that already existed at call time —
    process-wide, for the rest of the pytest session. This silently breaks
    unrelated tests elsewhere in the suite that rely on ``caplog`` capturing
    records from loggers such as ``mom_bot.roles``/``mom_bot.sidecar``.

    This fixture wraps ``fileConfig`` so the ``disable_existing_loggers``
    argument is always ``False``, without touching ``env.py`` or the
    production logging configuration. Mirrors the identical fixture in
    ``tests/test_alembic.py``, kept as a local copy here so this module
    independently neutralizes the side effect regardless of test-collection
    order or which other alembic test modules are present.
    """
    _real_fileConfig = logging.config.fileConfig

    def _patched_fileConfig(fname: Any, *args: Any, **kwargs: Any) -> None:
        """Delegate to real fileConfig with disable_existing_loggers=False."""
        kwargs["disable_existing_loggers"] = False
        _real_fileConfig(fname, *args, **kwargs)

    with patch("logging.config.fileConfig", side_effect=_patched_fileConfig):
        yield


def _make_alembic_config(db_path: str) -> Config:
    """Create an Alembic Config pointed at a temp SQLite database.

    Args:
        db_path: Filesystem path to the SQLite database file.

    Returns:
        An Alembic ``Config`` instance with the URL override applied.
    """
    cfg = Config(_ALEMBIC_INI)
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _get_table_names(engine: sa.Engine) -> list[str]:
    """Return all table names in the database."""
    inspector = sa.inspect(engine)
    return sorted(inspector.get_table_names())


def _get_column_names(engine: sa.Engine, table: str) -> list[str]:
    """Return column names for ``table``."""
    inspector = sa.inspect(engine)
    return [col["name"] for col in inspector.get_columns(table)]


def test_member_activity_table_exists_after_upgrade(tmp_path: Path) -> None:
    """``alembic upgrade head`` creates the ``member_activity`` table."""
    db_file = str(tmp_path / "test.db")
    cfg = _make_alembic_config(db_file)
    command.upgrade(cfg, "head")

    engine = sa.create_engine(f"sqlite:///{db_file}")
    tables = _get_table_names(engine)
    engine.dispose()

    assert "member_activity" in tables, f"Expected 'member_activity' table; found: {tables}"


def test_member_activity_columns(tmp_path: Path) -> None:
    """``member_activity`` has the four spec'd columns (#300 technical notes)."""
    db_file = str(tmp_path / "test.db")
    cfg = _make_alembic_config(db_file)
    command.upgrade(cfg, "head")

    engine = sa.create_engine(f"sqlite:///{db_file}")
    columns = _get_column_names(engine, "member_activity")
    engine.dispose()

    expected = {"id", "guild_id", "member_id", "joined_at", "first_message_at"}
    assert expected.issubset(
        set(columns)
    ), f"Missing columns in 'member_activity': {expected - set(columns)}"


def test_member_activity_primary_key(tmp_path: Path) -> None:
    """``member_activity.id`` is the primary key."""
    db_file = str(tmp_path / "test.db")
    cfg = _make_alembic_config(db_file)
    command.upgrade(cfg, "head")

    engine = sa.create_engine(f"sqlite:///{db_file}")
    inspector = sa.inspect(engine)
    pk = inspector.get_pk_constraint("member_activity")
    engine.dispose()

    assert "id" in pk["constrained_columns"], f"Expected 'id' as PK; got: {pk}"


def test_member_activity_downgrade_drops_table(tmp_path: Path) -> None:
    """``downgrade()`` removes ``member_activity`` cleanly (reversible migration).

    Asserts the table is present immediately after upgrade (a precondition
    that must itself pass — otherwise "not in tables" after downgrade would
    hold vacuously for a migration that was never applied in the first
    place, masking a missing migration file as a passing downgrade test).
    """
    db_file = str(tmp_path / "test.db")
    cfg = _make_alembic_config(db_file)
    command.upgrade(cfg, "head")

    engine = sa.create_engine(f"sqlite:///{db_file}")
    tables_after_upgrade = _get_table_names(engine)
    engine.dispose()
    assert "member_activity" in tables_after_upgrade, (
        f"Precondition failed: 'member_activity' must exist after upgrade "
        f"before downgrade is meaningful; found: {tables_after_upgrade}"
    )

    # Target the specific ancestor revision rather than "-1": a later
    # migration (b6_new_member_alert_subscription, #301) now chains on top
    # of this one, so a relative one-step downgrade from head would only
    # undo that later migration, not this one.
    command.downgrade(cfg, "b4_idx_mn_sent_occ_date")

    engine = sa.create_engine(f"sqlite:///{db_file}")
    tables_after_downgrade = _get_table_names(engine)
    engine.dispose()

    assert "member_activity" not in tables_after_downgrade, (
        f"Expected 'member_activity' to be dropped by downgrade; "
        f"found: {tables_after_downgrade}"
    )
