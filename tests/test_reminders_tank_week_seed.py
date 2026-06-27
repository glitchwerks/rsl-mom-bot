"""Tests for tank-week schema columns and seed behavior (#268).

Covers:
- month_condition column exists on Reminder and accepts NULL,
  'tank_week_headsup', and 'tank_week_end'.
- delivery_target column exists, is NOT NULL, and defaults to 'channel'
  (verified via DB round-trip — server defaults only appear after commit).
- _maybe_seed_reminders inserts four rows on an empty table (Hydra, Chimera,
  TankHeadsup, TankEnd), with the two new rows sharing channel_id and
  role_mention_id from the Hydra row.
- Data migration: a migration insert copies channel_id / role_mention_id
  from the existing Hydra row into the two new tank-week rows.
"""

from __future__ import annotations

import datetime
from unittest.mock import MagicMock, patch

import discord
import pytest
from sqlalchemy import create_engine, func, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from mom_bot.db import Base
from mom_bot.reminders.models import Reminder, ReminderSent  # noqa: F401
from mom_bot.reminders.seed import _maybe_seed_reminders

# ---------------------------------------------------------------------------
# Constants matching the existing seed test conventions
# ---------------------------------------------------------------------------

_CHANNEL_NAME = "reminders"
_ROLE_NAME = "Member"
_CHANNEL_ID = 987654321098765432
_ROLE_ID = 111222333444555666
_GUILD_ID = 1234567890
_GUILD_NAME = "test-guild"

# ---------------------------------------------------------------------------
# Fixtures (mirroring test_reminders_seed.py conventions)
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine() -> object:
    """In-memory SQLite engine with reminder tables created from ORM metadata."""
    eng = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture()
def session(engine: object) -> Session:
    """Open session on the in-memory engine."""
    with Session(engine) as s:
        yield s


@pytest.fixture()
def mock_bot() -> MagicMock:
    """discord.Client mock with a guild that resolves the expected channel/role."""
    mock_channel = MagicMock(spec=discord.TextChannel)
    mock_channel.name = _CHANNEL_NAME
    mock_channel.id = _CHANNEL_ID

    mock_role = MagicMock(spec=discord.Role)
    mock_role.name = _ROLE_NAME
    mock_role.id = _ROLE_ID

    mock_guild = MagicMock(spec=discord.Guild)
    mock_guild.text_channels = [mock_channel]
    mock_guild.roles = [mock_role]
    mock_guild.name = _GUILD_NAME
    mock_guild.id = _GUILD_ID

    bot = MagicMock(spec=discord.Client)
    bot.get_guild = MagicMock(return_value=mock_guild)
    return bot


def _secret_side_effect(name: str) -> str:
    """Resolve expected KV secret names to fake values."""
    secrets = {
        "guild-id": str(_GUILD_ID),
        "reminder-channel-name": _CHANNEL_NAME,
        "reminder-mention-role-name": _ROLE_NAME,
    }
    if name not in secrets:
        raise KeyError(f"Unexpected secret: {name!r}")
    return secrets[name]


# ---------------------------------------------------------------------------
# Schema: month_condition column
# ---------------------------------------------------------------------------


def test_month_condition_column_exists(engine: object) -> None:
    """month_condition column is present on the reminders table."""
    insp = inspect(engine)
    columns = {col["name"] for col in insp.get_columns("reminders")}
    assert (
        "month_condition" in columns
    ), "Expected 'month_condition' column on reminders table — column missing."


def test_month_condition_defaults_to_null(session: Session) -> None:
    """A Reminder inserted without month_condition stores NULL."""
    reminder = Reminder(
        name="NoCondition",
        channel_id=_CHANNEL_ID,
        weekday=1,
        fire_time_utc=datetime.time(7, 0, 0),
        message_template="test",
        role_mention_id=None,
    )
    session.add(reminder)
    session.commit()
    session.refresh(reminder)
    assert reminder.month_condition is None


def test_month_condition_accepts_tank_week_headsup(session: Session) -> None:
    """month_condition='tank_week_headsup' is accepted by the schema."""
    reminder = Reminder(
        name="Headsup",
        channel_id=_CHANNEL_ID,
        weekday=1,
        fire_time_utc=datetime.time(7, 0, 0),
        message_template="test",
        role_mention_id=None,
        month_condition="tank_week_headsup",
    )
    session.add(reminder)
    session.commit()
    session.refresh(reminder)
    assert reminder.month_condition == "tank_week_headsup"


def test_month_condition_accepts_tank_week_end(session: Session) -> None:
    """month_condition='tank_week_end' is accepted by the schema."""
    reminder = Reminder(
        name="TankEnd",
        channel_id=_CHANNEL_ID,
        weekday=1,
        fire_time_utc=datetime.time(7, 0, 0),
        message_template="test",
        role_mention_id=None,
        month_condition="tank_week_end",
    )
    session.add(reminder)
    session.commit()
    session.refresh(reminder)
    assert reminder.month_condition == "tank_week_end"


# ---------------------------------------------------------------------------
# Schema: delivery_target column (shared foundational change, spec §6)
# ---------------------------------------------------------------------------


def test_delivery_target_column_exists(engine: object) -> None:
    """delivery_target column is present on the reminders table."""
    insp = inspect(engine)
    columns = {col["name"] for col in insp.get_columns("reminders")}
    assert (
        "delivery_target" in columns
    ), "Expected 'delivery_target' column on reminders table — column missing."


def test_delivery_target_defaults_to_channel_via_round_trip(
    session: Session,
) -> None:
    """delivery_target defaults to 'channel' — verified via DB round-trip.

    Server defaults only apply at INSERT time; asserting on an uncommitted
    Python object would read None/unset. A commit + refresh is required
    to observe the server-applied default value (spec §6 NIT note).
    """
    reminder = Reminder(
        name="DefaultTarget",
        channel_id=_CHANNEL_ID,
        weekday=1,
        fire_time_utc=datetime.time(7, 0, 0),
        message_template="test",
        role_mention_id=None,
        # delivery_target intentionally omitted to exercise the server default.
    )
    session.add(reminder)
    session.commit()
    session.refresh(reminder)
    assert reminder.delivery_target == "channel", (
        f"Expected delivery_target='channel' after round-trip, " f"got {reminder.delivery_target!r}"
    )


# ---------------------------------------------------------------------------
# Seed: empty table inserts all four rows
# ---------------------------------------------------------------------------


def test_seed_empty_table_inserts_four_rows(session: Session, mock_bot: MagicMock) -> None:
    """Empty table + valid KV → four rows: Hydra, Chimera, TankHeadsup, TankEnd."""
    with patch(
        "mom_bot.reminders.seed.load_secret",
        side_effect=_secret_side_effect,
    ):
        _maybe_seed_reminders(session, mock_bot)

    count = session.scalar(select(func.count(Reminder.id)))
    assert count == 4, f"Expected 4 seeded rows, got {count}"


def test_seed_tank_headsup_row_has_correct_attributes(
    session: Session, mock_bot: MagicMock
) -> None:
    """Seeded TankHeadsup row: weekday=1, fire_time=07:00, month_condition='tank_week_headsup'."""
    with patch(
        "mom_bot.reminders.seed.load_secret",
        side_effect=_secret_side_effect,
    ):
        _maybe_seed_reminders(session, mock_bot)

    row = session.execute(
        select(Reminder).where(Reminder.name == "Hydra Tank Week Heads-up")
    ).scalar_one()
    assert row.weekday == 1
    assert row.fire_time_utc == datetime.time(7, 0, 0)
    assert row.month_condition == "tank_week_headsup"


def test_seed_tank_end_row_has_correct_attributes(session: Session, mock_bot: MagicMock) -> None:
    """Seeded TankEnd row has weekday=1, fire_time=07:00, month_condition='tank_week_end'."""
    with patch(
        "mom_bot.reminders.seed.load_secret",
        side_effect=_secret_side_effect,
    ):
        _maybe_seed_reminders(session, mock_bot)

    row = session.execute(
        select(Reminder).where(Reminder.name == "Hydra Tank Week End")
    ).scalar_one()
    assert row.weekday == 1
    assert row.fire_time_utc == datetime.time(7, 0, 0)
    assert row.month_condition == "tank_week_end"


def test_seed_tank_week_rows_inherit_channel_and_role_from_hydra(
    session: Session, mock_bot: MagicMock
) -> None:
    """New tank-week rows share channel_id and role_mention_id with the Hydra row."""
    with patch(
        "mom_bot.reminders.seed.load_secret",
        side_effect=_secret_side_effect,
    ):
        _maybe_seed_reminders(session, mock_bot)

    hydra = session.execute(select(Reminder).where(Reminder.name == "Hydra")).scalar_one()
    headsup = session.execute(
        select(Reminder).where(Reminder.name == "Hydra Tank Week Heads-up")
    ).scalar_one()
    tank_end = session.execute(
        select(Reminder).where(Reminder.name == "Hydra Tank Week End")
    ).scalar_one()

    assert (
        headsup.channel_id == hydra.channel_id
    ), f"TankHeadsup channel_id {headsup.channel_id} != Hydra channel_id {hydra.channel_id}"
    assert headsup.role_mention_id == hydra.role_mention_id, (
        f"TankHeadsup role_mention_id {headsup.role_mention_id} "
        f"!= Hydra role_mention_id {hydra.role_mention_id}"
    )
    assert (
        tank_end.channel_id == hydra.channel_id
    ), f"TankEnd channel_id {tank_end.channel_id} != Hydra channel_id {hydra.channel_id}"
    assert tank_end.role_mention_id == hydra.role_mention_id, (
        f"TankEnd role_mention_id {tank_end.role_mention_id} "
        f"!= Hydra role_mention_id {hydra.role_mention_id}"
    )


# ---------------------------------------------------------------------------
# Data migration: new rows copied from existing Hydra row
# ---------------------------------------------------------------------------


def test_data_migration_inserts_tank_week_rows_copying_hydra_channel_and_role(
    session: Session,
) -> None:
    """Data migration inserts TankHeadsup+TankEnd into an already-seeded DB.

    Simulates an already-seeded environment (Hydra + Chimera present).
    The migration must copy channel_id and role_mention_id from the Hydra row
    (no Discord access at migration time — spec §3 item 2).
    """
    # Pre-seed Hydra + Chimera (simulating an already-seeded prod/dev DB).
    hydra = Reminder(
        name="Hydra",
        channel_id=_CHANNEL_ID,
        weekday=1,
        fire_time_utc=datetime.time(7, 0, 0),
        message_template="Hydra msg",
        role_mention_id=_ROLE_ID,
    )
    chimera = Reminder(
        name="Chimera",
        channel_id=_CHANNEL_ID,
        weekday=2,
        fire_time_utc=datetime.time(12, 0, 0),
        message_template="Chimera msg",
        role_mention_id=_ROLE_ID,
    )
    session.add_all([hydra, chimera])
    session.commit()

    # Import and invoke the data migration function.
    from mom_bot.reminders.seed import seed_tank_week_reminders  # noqa: PLC0415

    seed_tank_week_reminders(session)

    # Both new rows must exist.
    headsup = session.execute(
        select(Reminder).where(Reminder.name == "Hydra Tank Week Heads-up")
    ).scalar_one_or_none()
    tank_end = session.execute(
        select(Reminder).where(Reminder.name == "Hydra Tank Week End")
    ).scalar_one_or_none()

    assert headsup is not None, "Migration did not insert 'Hydra Tank Week Heads-up' row."
    assert tank_end is not None, "Migration did not insert 'Hydra Tank Week End' row."

    # Both must inherit channel_id and role_mention_id from Hydra.
    assert headsup.channel_id == _CHANNEL_ID
    assert headsup.role_mention_id == _ROLE_ID
    assert tank_end.channel_id == _CHANNEL_ID
    assert tank_end.role_mention_id == _ROLE_ID


def test_data_migration_is_idempotent(session: Session) -> None:
    """Running the data migration twice does not create duplicate rows."""
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

    from mom_bot.reminders.seed import seed_tank_week_reminders  # noqa: PLC0415

    seed_tank_week_reminders(session)
    seed_tank_week_reminders(session)

    headsup_count = session.scalar(
        select(func.count(Reminder.id)).where(Reminder.name == "Hydra Tank Week Heads-up")
    )
    tank_end_count = session.scalar(
        select(func.count(Reminder.id)).where(Reminder.name == "Hydra Tank Week End")
    )
    assert headsup_count == 1, f"Expected 1 heads-up row, got {headsup_count}"
    assert tank_end_count == 1, f"Expected 1 tank-end row, got {tank_end_count}"


def test_data_migration_noop_when_no_hydra_row(session: Session) -> None:
    """Migration is a safe no-op when no Hydra row exists (fresh DB path)."""
    from mom_bot.reminders.seed import seed_tank_week_reminders  # noqa: PLC0415

    # Must not raise; table stays empty.
    seed_tank_week_reminders(session)

    count = session.scalar(select(func.count(Reminder.id)))
    assert count == 0, f"Expected 0 rows on empty-table no-op, got {count}"


# ---------------------------------------------------------------------------
# Schema: delivery_target CHECK constraint (review-remediation hardening)
# ---------------------------------------------------------------------------


def test_delivery_target_check_rejects_invalid_value(session: Session) -> None:
    """delivery_target CHECK rejects values other than 'channel' or 'dm'.

    The model-level CheckConstraint is enforced by SQLite when the table is
    created via Base.metadata.create_all(), so an invalid value raises
    IntegrityError at commit time.
    """
    reminder = Reminder(
        name="BadTarget",
        channel_id=_CHANNEL_ID,
        weekday=1,
        fire_time_utc=datetime.time(7, 0, 0),
        message_template="test",
        role_mention_id=None,
        delivery_target="foo",
    )
    session.add(reminder)
    with pytest.raises(IntegrityError):
        session.commit()


def test_delivery_target_check_accepts_channel(session: Session) -> None:
    """delivery_target='channel' is accepted by the CHECK constraint."""
    reminder = Reminder(
        name="ChannelTarget",
        channel_id=_CHANNEL_ID,
        weekday=1,
        fire_time_utc=datetime.time(7, 0, 0),
        message_template="test",
        role_mention_id=None,
        delivery_target="channel",
    )
    session.add(reminder)
    session.commit()
    session.refresh(reminder)
    assert reminder.delivery_target == "channel"


def test_delivery_target_check_accepts_dm(session: Session) -> None:
    """delivery_target='dm' is accepted by the CHECK constraint."""
    reminder = Reminder(
        name="DmTarget",
        channel_id=_CHANNEL_ID,
        weekday=1,
        fire_time_utc=datetime.time(7, 0, 0),
        message_template="test",
        role_mention_id=None,
        delivery_target="dm",
    )
    session.add(reminder)
    session.commit()
    session.refresh(reminder)
    assert reminder.delivery_target == "dm"
