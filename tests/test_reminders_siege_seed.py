"""Tests for Siege reminder seeding (#327 — Slice B).

TDD: written before ``seed_siege_reminders`` and the ``0007_siege_seed_rows``
migration exist. Own file, not appended to ``tests/test_reminders_seed.py``
or ``tests/test_alembic.py``, mirroring the precedent in
``tests/test_alembic_member_activity.py`` for keeping a from-scratch feature's
tests self-contained.

Covers:

- ``seed_siege_reminders`` (the data-migration entry point for
  already-seeded environments, mirroring ``seed_tank_week_reminders``):
  inserts the two Siege rows copying ``channel_id``/``role_mention_id``
  from the existing ``Hydra`` row, is idempotent, and no-ops when no
  ``Hydra`` row exists.
- ``seed_siege_reminders`` is never referenced from ``main.py`` — production
  activation is migration-only (plan §8 Slice B step 3).
- The ``0007_siege_seed_rows`` Alembic migration: correct row content,
  idempotent re-application, and the exact revision chain
  (``0007_siege_seed_rows`` revises ``0006_siege_month_conditions``).
"""

from __future__ import annotations

import datetime
import importlib.util
import logging.config
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import sqlalchemy as sa
from alembic.config import Config
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mom_bot.db import Base
from mom_bot.reminders.models import Reminder, ReminderSent  # noqa: F401

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ALEMBIC_INI = str(_REPO_ROOT / "alembic.ini")
_MIGRATION_FILE = _REPO_ROOT / "migrations" / "versions" / "0007_siege_seed_rows.py"

_CHANNEL_ID = 987654321098765432
_ROLE_ID = 111222333444555666

_SIEGE_48H_NAME = "Siege 48h Heads-up"
_SIEGE_24H_NAME = "Siege 24h Heads-up"


# ---------------------------------------------------------------------------
# Module-scoped autouse fixture: prevent alembic from disabling loggers
# (mirrors the identical fixture in tests/test_alembic.py and
# tests/test_alembic_member_activity.py — kept as a local copy so this
# module independently neutralizes the side effect).
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True, scope="module")
def _no_disable_existing_loggers() -> Any:
    """Force ``disable_existing_loggers=False`` on every ``fileConfig`` call."""
    _real_fileConfig = logging.config.fileConfig

    def _patched_fileConfig(fname: Any, *args: Any, **kwargs: Any) -> None:
        """Delegate to real fileConfig with disable_existing_loggers=False."""
        kwargs["disable_existing_loggers"] = False
        _real_fileConfig(fname, *args, **kwargs)

    with patch("logging.config.fileConfig", side_effect=_patched_fileConfig):
        yield


# ---------------------------------------------------------------------------
# ORM fixtures (mirroring tests/test_reminders_tank_week_seed.py)
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine() -> object:
    """In-memory SQLite engine with reminder tables created from ORM metadata."""
    eng = sa.create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture()
def session(engine: object) -> Session:
    """Open session on the in-memory engine."""
    with Session(engine) as s:
        yield s


def _seed_hydra(session: Session) -> Reminder:
    """Insert a minimal Hydra row so seed_siege_reminders can copy from it."""
    hydra = Reminder(
        name="Hydra",
        channel_id=_CHANNEL_ID,
        weekday=1,
        fire_time_utc=datetime.time(7, 0, 0),
        message_template="Hydra msg",
        role_mention_id=_ROLE_ID,
    )
    session.add(hydra)
    session.commit()
    return hydra


# ---------------------------------------------------------------------------
# seed_siege_reminders — data-migration entry point for already-seeded DBs
# ---------------------------------------------------------------------------


def test_seed_siege_reminders_inserts_two_rows_with_correct_attributes(
    session: Session,
) -> None:
    """seed_siege_reminders inserts exactly 2 rows with the approved shape.

    Both rows copy channel_id/role_mention_id from the existing Hydra row,
    carry delivery_target='channel' explicitly, and use the Slice-A-added
    month_condition values.
    """
    _seed_hydra(session)

    from mom_bot.reminders.seed import (  # noqa: PLC0415
        SIEGE_24H_HEADSUP_TEMPLATE,
        SIEGE_48H_HEADSUP_TEMPLATE,
        seed_siege_reminders,
    )

    before_count = session.scalar(select(func.count(Reminder.id)))
    seed_siege_reminders(session)
    after_count = session.scalar(select(func.count(Reminder.id)))
    assert after_count - before_count == 2, (
        f"Expected seed_siege_reminders to insert exactly 2 new rows; "
        f"went from {before_count} to {after_count}."
    )

    siege_48h = session.execute(
        select(Reminder).where(Reminder.name == _SIEGE_48H_NAME)
    ).scalar_one_or_none()
    siege_24h = session.execute(
        select(Reminder).where(Reminder.name == _SIEGE_24H_NAME)
    ).scalar_one_or_none()

    assert siege_48h is not None, f"Missing {_SIEGE_48H_NAME!r} row."
    assert siege_48h.weekday == 6
    assert siege_48h.fire_time_utc == datetime.time(10, 0, 0)
    assert siege_48h.month_condition == "siege_48h_headsup"
    assert siege_48h.delivery_target == "channel"
    assert siege_48h.message_template == SIEGE_48H_HEADSUP_TEMPLATE
    assert siege_48h.channel_id == _CHANNEL_ID
    assert siege_48h.role_mention_id == _ROLE_ID

    assert siege_24h is not None, f"Missing {_SIEGE_24H_NAME!r} row."
    assert siege_24h.weekday == 0
    assert siege_24h.fire_time_utc == datetime.time(10, 0, 0)
    assert siege_24h.month_condition == "siege_24h_headsup"
    assert siege_24h.delivery_target == "channel"
    assert siege_24h.message_template == SIEGE_24H_HEADSUP_TEMPLATE
    assert siege_24h.channel_id == _CHANNEL_ID
    assert siege_24h.role_mention_id == _ROLE_ID


def test_seed_siege_reminders_is_idempotent(session: Session) -> None:
    """Calling seed_siege_reminders twice inserts nothing on the second call."""
    _seed_hydra(session)

    from mom_bot.reminders.seed import seed_siege_reminders  # noqa: PLC0415

    seed_siege_reminders(session)
    first_count = session.scalar(select(func.count(Reminder.id)))

    seed_siege_reminders(session)  # must not raise a duplicate-key error
    second_count = session.scalar(select(func.count(Reminder.id)))

    assert second_count == first_count, (
        f"Second call inserted rows: {first_count} -> {second_count}. "
        "seed_siege_reminders must be idempotent (WHERE NOT EXISTS by name)."
    )

    siege_48h_count = session.scalar(
        select(func.count(Reminder.id)).where(Reminder.name == _SIEGE_48H_NAME)
    )
    siege_24h_count = session.scalar(
        select(func.count(Reminder.id)).where(Reminder.name == _SIEGE_24H_NAME)
    )
    assert siege_48h_count == 1, f"Expected 1 {_SIEGE_48H_NAME!r} row, got {siege_48h_count}"
    assert siege_24h_count == 1, f"Expected 1 {_SIEGE_24H_NAME!r} row, got {siege_24h_count}"


def test_seed_siege_reminders_noop_when_no_hydra_row(session: Session) -> None:
    """seed_siege_reminders is a safe no-op when no Hydra row exists."""
    from mom_bot.reminders.seed import seed_siege_reminders  # noqa: PLC0415

    seed_siege_reminders(session)  # must not raise

    count = session.scalar(select(func.count(Reminder.id)))
    assert count == 0, f"Expected 0 rows on no-Hydra no-op, got {count}"


def test_seed_siege_reminders_not_referenced_in_main_py() -> None:
    """seed_siege_reminders is never called from main.py.

    Production activation happens exclusively via the 0007 migration —
    same precedent as seed_tank_week_reminders (plan §8 Slice B step 3,
    project-reviewer's folded-in CONCERN). Regression guard: this asserts
    the source text of main.py never names the function, so a future
    edit wiring it in cannot land silently.
    """
    main_py = _REPO_ROOT / "src" / "mom_bot" / "main.py"
    source = main_py.read_text(encoding="utf-8")
    assert "seed_siege_reminders" not in source, (
        "seed_siege_reminders must not be referenced in main.py — "
        "production activation is migration-only (0007_siege_seed_rows)."
    )


# ---------------------------------------------------------------------------
# 0007_siege_seed_rows — data migration for already-seeded environments
# ---------------------------------------------------------------------------


def _make_alembic_config(db_path: str) -> Config:
    """Create an Alembic Config pointed at a temp SQLite database."""
    cfg = Config(_ALEMBIC_INI)
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _load_migration_module() -> Any:
    """Load migrations/versions/0007_siege_seed_rows.py directly by path.

    Loading (rather than going through ``alembic upgrade``) lets a test
    call the migration's own ``upgrade()`` function more than once against
    the same connection to exercise its ``WHERE NOT EXISTS`` idempotency
    guard directly — ``alembic upgrade`` to an already-applied revision is
    a version-table no-op and never re-invokes the migration body.

    Raises:
        FileNotFoundError: If the migration file does not exist yet.
    """
    spec = importlib.util.spec_from_file_location("_siege_seed_rows_migration", _MIGRATION_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _seed_hydra_row_sql(engine: sa.Engine) -> None:
    """Insert a minimal Hydra row via raw SQL (mirrors 0005's precedent)."""
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO reminders "
                "(name, channel_id, weekday, fire_time_utc, message_template, "
                "role_mention_id, delivery_target, created_at, updated_at) "
                "VALUES ('Hydra', :channel_id, 1, '07:00:00', 'Hydra msg', "
                ":role_id, 'channel', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"channel_id": _CHANNEL_ID, "role_id": _ROLE_ID},
        )


class TestSiegeSeedRowsMigration:
    """Chain, content, and idempotency tests for the 0007 data migration.

    Mirrors ``TestSiegeMonthConditionsMigration`` in ``tests/test_alembic.py``
    — pins the exact revision id so these tests fail before 0007 exists
    (current head is ``0006_siege_month_conditions``) rather than passing
    vacuously against the pre-#327 chain.
    """

    _EXPECTED_REVISION = "0007_siege_seed_rows"
    _EXPECTED_PARENT = "0006_siege_month_conditions"

    def test_alembic_heads_is_0007_siege_seed_rows(self) -> None:
        """The sole alembic head is 0007_siege_seed_rows once it lands."""
        cfg = Config(_ALEMBIC_INI)
        script = ScriptDirectory.from_config(cfg)
        heads = script.get_heads()
        assert heads == [self._EXPECTED_REVISION], (
            f"Expected exactly one alembic head, {self._EXPECTED_REVISION!r}, " f"got: {heads}"
        )

    def test_down_revision_chains_off_0006(self) -> None:
        """0007's down_revision is 0006_siege_month_conditions (plan §4.2)."""
        cfg = Config(_ALEMBIC_INI)
        script = ScriptDirectory.from_config(cfg)
        revision = script.get_revision(self._EXPECTED_REVISION)
        assert revision.down_revision == self._EXPECTED_PARENT, (
            f"Expected 0007's down_revision == {self._EXPECTED_PARENT!r}, "
            f"got: {revision.down_revision!r}"
        )

    def test_upgrade_inserts_two_siege_rows_copying_hydra(self, tmp_path: Path) -> None:
        """0007.upgrade() inserts both Siege rows, copying channel/role from Hydra."""
        from mom_bot.reminders.seed import (  # noqa: PLC0415
            SIEGE_24H_HEADSUP_TEMPLATE,
            SIEGE_48H_HEADSUP_TEMPLATE,
        )

        db_file = str(tmp_path / "test.db")
        cfg = _make_alembic_config(db_file)
        # Build schema up to 0006 (the migration under test's parent) via
        # the normal alembic command path — only 0007's own upgrade() is
        # invoked directly, below.
        from alembic import command  # noqa: PLC0415

        command.upgrade(cfg, self._EXPECTED_PARENT)

        engine = sa.create_engine(f"sqlite:///{db_file}")
        _seed_hydra_row_sql(engine)

        module = _load_migration_module()
        with engine.begin() as conn:
            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                module.upgrade()

        with engine.connect() as conn:
            row_48h = conn.execute(
                sa.text(
                    "SELECT weekday, fire_time_utc, message_template, "
                    "month_condition, delivery_target, channel_id, role_mention_id "
                    "FROM reminders WHERE name = :name"
                ),
                {"name": _SIEGE_48H_NAME},
            ).fetchone()
            row_24h = conn.execute(
                sa.text(
                    "SELECT weekday, fire_time_utc, message_template, "
                    "month_condition, delivery_target, channel_id, role_mention_id "
                    "FROM reminders WHERE name = :name"
                ),
                {"name": _SIEGE_24H_NAME},
            ).fetchone()
        engine.dispose()

        assert row_48h is not None, f"Migration did not insert {_SIEGE_48H_NAME!r} row."
        assert row_48h[0] == 6
        assert row_48h[1] == "10:00:00"
        assert row_48h[2] == SIEGE_48H_HEADSUP_TEMPLATE
        assert row_48h[3] == "siege_48h_headsup"
        assert row_48h[4] == "channel"
        assert row_48h[5] == _CHANNEL_ID
        assert row_48h[6] == _ROLE_ID

        assert row_24h is not None, f"Migration did not insert {_SIEGE_24H_NAME!r} row."
        assert row_24h[0] == 0
        assert row_24h[1] == "10:00:00"
        assert row_24h[2] == SIEGE_24H_HEADSUP_TEMPLATE
        assert row_24h[3] == "siege_24h_headsup"
        assert row_24h[4] == "channel"
        assert row_24h[5] == _CHANNEL_ID
        assert row_24h[6] == _ROLE_ID

    def test_upgrade_called_twice_is_idempotent(self, tmp_path: Path) -> None:
        """Calling 0007's upgrade() twice does not insert duplicate rows.

        Alembic's own ``upgrade`` command would no-op the second call at
        the version-table level without re-running the migration body, so
        this calls the loaded module's ``upgrade()`` directly on the same
        connection to exercise the migration's own ``WHERE NOT EXISTS``
        guard (mirroring 0005's idempotency, plan §8 Slice B step 5).
        """
        db_file = str(tmp_path / "test.db")
        cfg = _make_alembic_config(db_file)
        from alembic import command  # noqa: PLC0415

        command.upgrade(cfg, self._EXPECTED_PARENT)

        engine = sa.create_engine(f"sqlite:///{db_file}")
        _seed_hydra_row_sql(engine)

        module = _load_migration_module()
        with engine.begin() as conn:
            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                module.upgrade()
                module.upgrade()  # second call — must not raise, must not duplicate

        with engine.connect() as conn:
            siege_48h_count = conn.execute(
                sa.text("SELECT count(*) FROM reminders WHERE name = :name"),
                {"name": _SIEGE_48H_NAME},
            ).scalar_one()
            siege_24h_count = conn.execute(
                sa.text("SELECT count(*) FROM reminders WHERE name = :name"),
                {"name": _SIEGE_24H_NAME},
            ).scalar_one()
        engine.dispose()

        assert siege_48h_count == 1, f"Expected 1 {_SIEGE_48H_NAME!r} row, got {siege_48h_count}"
        assert siege_24h_count == 1, f"Expected 1 {_SIEGE_24H_NAME!r} row, got {siege_24h_count}"
