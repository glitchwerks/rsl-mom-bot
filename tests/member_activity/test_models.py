"""Tests for the ``MemberActivity`` ORM model (#300).

TDD: written before ``mom_bot.member_activity.models`` exists. Covers the
schema shape required by the issue's technical notes: a table keyed on
``(guild_id, member_id)`` recording ``joined_at`` and ``first_message_at``.

Binding contract decisions (flagged for router/spec-owner ratification,
following the ``test_on_member_join.py`` convention for narrowing an
under-specified AC):

- ``guild_id`` / ``member_id`` are stored as ``BigInteger`` (not ``Text``).
  Precedent: migration ``0003_widen_reminder_snowflakes`` exists precisely
  because an earlier ``sa.Integer`` (32-bit) column silently worked under
  SQLite but raised ``NumericValueOutOfRange`` on PostgreSQL for a real
  Discord snowflake. A new snowflake column should not repeat that mistake.
  (``member_notification.target_discord_id`` uses ``Text`` instead, but
  that column also crosses an ``int(...)`` boundary at every read/write —
  see its module docstring — which this table has no comparable need for.)
- ``(guild_id, member_id)`` is UNIQUE — at most one tracking row per member
  per guild at a time.
- ``joined_at`` is NOT NULL; ``first_message_at`` is nullable (NULL means
  "no message yet — still pending kick").
- Timestamps are stored as naive UTC ``DateTime`` values (no ``tzinfo``) —
  mirroring the codebase's established UTC convention (``Reminder.fire_time_utc``
  etc. are also naive). Callers are expected to strip ``tzinfo`` before
  passing datetimes to the service layer (see ``tests/member_activity/test_service.py``).
"""

from __future__ import annotations

import datetime
from typing import Any

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from mom_bot.db import Base

_GUILD_ID = 300000000000000001
_MEMBER_ID = 200000000000000042
_OTHER_MEMBER_ID = 200000000000000099


def _make_engine() -> Any:
    """Create an in-memory SQLite engine with all registered tables."""
    # Import so MemberActivity registers on Base.metadata before create_all.
    import mom_bot.member_activity.models  # noqa: F401

    engine = create_engine(
        "sqlite:///:memory:",
        echo=False,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return engine


def test_table_and_columns_exist() -> None:
    """``member_activity`` table exists with the four spec'd columns."""
    engine = _make_engine()
    inspector = inspect(engine)

    assert "member_activity" in inspector.get_table_names()
    columns = {col["name"] for col in inspector.get_columns("member_activity")}
    expected = {"id", "guild_id", "member_id", "joined_at", "first_message_at"}
    assert expected.issubset(columns), f"Missing columns: {expected - columns}"


def test_insert_row_with_null_first_message_at() -> None:
    """A freshly-joined member's row has ``first_message_at`` NULL."""
    from mom_bot.member_activity.models import MemberActivity

    engine = _make_engine()
    joined_at = datetime.datetime(2026, 7, 18, 12, 0, 0)

    with Session(engine) as session:
        row = MemberActivity(
            guild_id=_GUILD_ID,
            member_id=_MEMBER_ID,
            joined_at=joined_at,
            first_message_at=None,
        )
        session.add(row)
        session.commit()
        session.refresh(row)

    with Session(engine) as session:
        fetched = session.query(MemberActivity).filter_by(member_id=_MEMBER_ID).one()

    assert fetched.guild_id == _GUILD_ID
    assert fetched.member_id == _MEMBER_ID
    assert fetched.first_message_at is None


def test_unique_constraint_on_guild_and_member() -> None:
    """A second row for the same (guild_id, member_id) pair is rejected."""
    from mom_bot.member_activity.models import MemberActivity

    engine = _make_engine()
    joined_at = datetime.datetime(2026, 7, 18, 12, 0, 0)

    with Session(engine) as session:
        session.add(MemberActivity(guild_id=_GUILD_ID, member_id=_MEMBER_ID, joined_at=joined_at))
        session.commit()

    with Session(engine) as session:
        session.add(MemberActivity(guild_id=_GUILD_ID, member_id=_MEMBER_ID, joined_at=joined_at))
        with pytest.raises(IntegrityError):
            session.commit()


def test_same_member_id_different_guild_is_allowed() -> None:
    """The UNIQUE constraint is scoped to (guild_id, member_id), not member_id alone."""
    from mom_bot.member_activity.models import MemberActivity

    engine = _make_engine()
    joined_at = datetime.datetime(2026, 7, 18, 12, 0, 0)

    with Session(engine) as session:
        session.add(MemberActivity(guild_id=_GUILD_ID, member_id=_MEMBER_ID, joined_at=joined_at))
        session.add(
            MemberActivity(
                guild_id=_GUILD_ID + 1,
                member_id=_MEMBER_ID,
                joined_at=joined_at,
            )
        )
        session.commit()  # must not raise

    with Session(engine) as session:
        count = session.query(MemberActivity).filter_by(member_id=_MEMBER_ID).count()
    assert count == 2
