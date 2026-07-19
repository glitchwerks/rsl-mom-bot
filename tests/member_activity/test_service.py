"""Tests for ``MemberActivityService`` (#300).

TDD: written before ``mom_bot.member_activity.service`` exists.

Covers the join-tracking / first-message / stale-lookup / cleanup surface
that ``on_member_join``, ``on_message``, and the auto-kick sweep scheduler
all depend on.

Binding contract decisions:

- All datetime arguments/returns are naive UTC ``datetime`` values (no
  ``tzinfo``) — see ``tests/member_activity/test_models.py`` docstring.
  Passing a tz-aware datetime is not exercised here; callers are expected
  to normalize before calling in.
- ``record_join`` is an upsert: calling it again for the same
  ``(guild_id, member_id)`` updates ``joined_at`` to the new value and
  resets ``first_message_at`` to ``NULL`` (a rejoin restarts the grace
  period — a member who left and rejoined should not be judged on a stale
  first-message record from a previous membership).
- ``record_first_message`` only ever sets ``first_message_at`` on its FIRST
  call for a given tracked member (idempotent — "first" message, not
  "latest"). If no tracking row exists (e.g. the member joined before this
  feature existed, or was never tracked for any reason), it is a silent
  no-op — it must never create a row itself, since ``record_join`` is the
  sole entry point that establishes tracking.
- ``list_stale`` returns rows where ``first_message_at IS NULL`` AND
  ``joined_at <= now - inactivity_window`` (default 24h, ``<=`` inclusive
  boundary, matching the ``fire_time_utc <= now_time`` convention in
  ``ReminderScheduler``).
- ``remove_tracking`` deletes the row for ``(guild_id, member_id)`` and is
  a no-op (does not raise) if no such row exists.
"""

from __future__ import annotations

import datetime
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from mom_bot.db import Base

_GUILD_ID = 300000000000000001
_MEMBER_ID = 200000000000000042
_OTHER_MEMBER_ID = 200000000000000099

_JOINED_AT = datetime.datetime(2026, 7, 18, 0, 0, 0)
_NOW_25H_LATER = _JOINED_AT + datetime.timedelta(hours=25)
_NOW_23H_LATER = _JOINED_AT + datetime.timedelta(hours=23)
_NOW_EXACTLY_24H_LATER = _JOINED_AT + datetime.timedelta(hours=24)


def _make_engine() -> Any:
    """Create an in-memory SQLite engine with all registered tables."""
    import mom_bot.member_activity.models  # noqa: F401

    engine = create_engine(
        "sqlite:///:memory:",
        echo=False,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return engine


def _make_session_factory(engine: Any) -> Any:
    """Return a sessionmaker bound to the given engine."""
    return sessionmaker(bind=engine)


def _make_service(engine: Any) -> Any:
    """Convenience factory for a MemberActivityService bound to *engine*."""
    from mom_bot.member_activity.service import MemberActivityService

    return MemberActivityService(_make_session_factory(engine))


# ---------------------------------------------------------------------------
# record_join
# ---------------------------------------------------------------------------


def test_record_join_inserts_row_with_null_first_message_at() -> None:
    """A first-time ``record_join`` call inserts a row with no first message."""
    from mom_bot.member_activity.models import MemberActivity

    engine = _make_engine()
    service = _make_service(engine)

    service.record_join(_GUILD_ID, _MEMBER_ID, _JOINED_AT)

    with Session(engine) as session:
        row = session.query(MemberActivity).filter_by(member_id=_MEMBER_ID).one()

    assert row.guild_id == _GUILD_ID
    assert row.joined_at == _JOINED_AT
    assert row.first_message_at is None


def test_record_join_twice_is_an_upsert_not_a_duplicate() -> None:
    """A second ``record_join`` for the same member updates, not duplicates."""
    from mom_bot.member_activity.models import MemberActivity

    engine = _make_engine()
    service = _make_service(engine)

    service.record_join(_GUILD_ID, _MEMBER_ID, _JOINED_AT)
    later_join = _JOINED_AT + datetime.timedelta(days=10)
    service.record_join(_GUILD_ID, _MEMBER_ID, later_join)

    with Session(engine) as session:
        rows = session.query(MemberActivity).filter_by(member_id=_MEMBER_ID).all()

    assert len(rows) == 1, f"Expected exactly one row (upsert), got {len(rows)}"
    assert rows[0].joined_at == later_join


def test_rejoin_resets_first_message_at() -> None:
    """Rejoining clears a previously-recorded first message.

    A member who posted, left, and rejoined should be tracked as a fresh
    join — their previous ``first_message_at`` must not carry over and
    silently exempt them from the new grace period.
    """
    from mom_bot.member_activity.models import MemberActivity

    engine = _make_engine()
    service = _make_service(engine)

    service.record_join(_GUILD_ID, _MEMBER_ID, _JOINED_AT)
    service.record_first_message(_GUILD_ID, _MEMBER_ID, _JOINED_AT + datetime.timedelta(hours=1))

    rejoin_at = _JOINED_AT + datetime.timedelta(days=30)
    service.record_join(_GUILD_ID, _MEMBER_ID, rejoin_at)

    with Session(engine) as session:
        row = session.query(MemberActivity).filter_by(member_id=_MEMBER_ID).one()

    assert row.joined_at == rejoin_at
    assert row.first_message_at is None


# ---------------------------------------------------------------------------
# record_first_message
# ---------------------------------------------------------------------------


def test_record_first_message_sets_value_when_unset() -> None:
    """First call sets ``first_message_at`` to the given timestamp."""
    from mom_bot.member_activity.models import MemberActivity

    engine = _make_engine()
    service = _make_service(engine)
    service.record_join(_GUILD_ID, _MEMBER_ID, _JOINED_AT)

    message_at = _JOINED_AT + datetime.timedelta(hours=2)
    service.record_first_message(_GUILD_ID, _MEMBER_ID, message_at)

    with Session(engine) as session:
        row = session.query(MemberActivity).filter_by(member_id=_MEMBER_ID).one()

    assert row.first_message_at == message_at


def test_record_first_message_is_idempotent_keeps_earliest() -> None:
    """A second call does not move an already-set ``first_message_at``."""
    from mom_bot.member_activity.models import MemberActivity

    engine = _make_engine()
    service = _make_service(engine)
    service.record_join(_GUILD_ID, _MEMBER_ID, _JOINED_AT)

    first_message_at = _JOINED_AT + datetime.timedelta(hours=2)
    second_message_at = _JOINED_AT + datetime.timedelta(hours=5)
    service.record_first_message(_GUILD_ID, _MEMBER_ID, first_message_at)
    service.record_first_message(_GUILD_ID, _MEMBER_ID, second_message_at)

    with Session(engine) as session:
        row = session.query(MemberActivity).filter_by(member_id=_MEMBER_ID).one()

    assert row.first_message_at == first_message_at, (
        f"Expected first_message_at to stay at the FIRST message "
        f"({first_message_at}); got {row.first_message_at}"
    )


def test_record_first_message_for_untracked_member_is_noop() -> None:
    """No tracking row exists → no row is created, no exception raised."""
    from mom_bot.member_activity.models import MemberActivity

    engine = _make_engine()
    service = _make_service(engine)
    # No record_join call for this member.

    service.record_first_message(_GUILD_ID, _MEMBER_ID, _JOINED_AT)

    with Session(engine) as session:
        count = session.query(MemberActivity).filter_by(member_id=_MEMBER_ID).count()
    assert count == 0


# ---------------------------------------------------------------------------
# list_stale
# ---------------------------------------------------------------------------


def test_list_stale_excludes_member_within_grace_period() -> None:
    """A member who joined 23h ago (no message) is not yet stale."""
    engine = _make_engine()
    service = _make_service(engine)
    service.record_join(_GUILD_ID, _MEMBER_ID, _JOINED_AT)

    stale = service.list_stale(now=_NOW_23H_LATER)

    assert stale == []


def test_list_stale_includes_member_past_grace_period_with_no_message() -> None:
    """A member who joined 25h ago with no message is stale."""
    engine = _make_engine()
    service = _make_service(engine)
    service.record_join(_GUILD_ID, _MEMBER_ID, _JOINED_AT)

    stale = service.list_stale(now=_NOW_25H_LATER)

    assert len(stale) == 1
    assert stale[0].guild_id == _GUILD_ID
    assert stale[0].member_id == _MEMBER_ID


def test_list_stale_boundary_is_inclusive() -> None:
    """Exactly 24h since join (no message) counts as stale (``<=`` boundary)."""
    engine = _make_engine()
    service = _make_service(engine)
    service.record_join(_GUILD_ID, _MEMBER_ID, _JOINED_AT)

    stale = service.list_stale(now=_NOW_EXACTLY_24H_LATER)

    assert len(stale) == 1


def test_list_stale_excludes_member_who_has_posted() -> None:
    """A member past 24h who DID post is never stale, regardless of elapsed time."""
    engine = _make_engine()
    service = _make_service(engine)
    service.record_join(_GUILD_ID, _MEMBER_ID, _JOINED_AT)
    service.record_first_message(_GUILD_ID, _MEMBER_ID, _JOINED_AT + datetime.timedelta(hours=1))

    stale = service.list_stale(now=_NOW_25H_LATER)

    assert stale == []


def test_list_stale_only_returns_the_stale_member_among_several() -> None:
    """A mixed population returns only the member(s) meeting the stale predicate."""
    engine = _make_engine()
    service = _make_service(engine)

    # Stale: past 24h, no message.
    service.record_join(_GUILD_ID, _MEMBER_ID, _JOINED_AT)
    # Not stale: past 24h, but posted.
    service.record_join(_GUILD_ID, _OTHER_MEMBER_ID, _JOINED_AT)
    service.record_first_message(
        _GUILD_ID, _OTHER_MEMBER_ID, _JOINED_AT + datetime.timedelta(hours=1)
    )

    stale = service.list_stale(now=_NOW_25H_LATER)

    assert [row.member_id for row in stale] == [_MEMBER_ID]


# ---------------------------------------------------------------------------
# remove_tracking
# ---------------------------------------------------------------------------


def test_remove_tracking_deletes_row() -> None:
    """After removal, the member no longer appears in any query."""
    from mom_bot.member_activity.models import MemberActivity

    engine = _make_engine()
    service = _make_service(engine)
    service.record_join(_GUILD_ID, _MEMBER_ID, _JOINED_AT)

    service.remove_tracking(_GUILD_ID, _MEMBER_ID)

    with Session(engine) as session:
        count = session.query(MemberActivity).filter_by(member_id=_MEMBER_ID).count()
    assert count == 0


def test_remove_tracking_on_untracked_member_does_not_raise() -> None:
    """Removing a member with no tracking row is a silent no-op."""
    engine = _make_engine()
    service = _make_service(engine)

    service.remove_tracking(_GUILD_ID, _MEMBER_ID)  # must not raise
