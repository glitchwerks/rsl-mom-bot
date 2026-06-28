"""Regression tests for MemberNotificationService (PR #277 findings).

Covers:

- Finding 1: Non-duplicate IntegrityError (CHECK violation) must NOT be
  surfaced as DuplicateNotificationError; genuine duplicate-name must.
- Finding 2: create/update with invalid target_discord_id raises
  InvalidDiscordIdError at write time (row not persisted).
- Finding 3: update() with an immutable/unknown key raises ValueError,
  does not silently mutate the row.

Spec reference: #269 per-member notifications § 2.4.
"""

from __future__ import annotations

import datetime
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from mom_bot.db import Base
from mom_bot.member_notifications.service import (
    DuplicateNotificationError,
    InvalidDiscordIdError,
    MemberNotificationService,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ANCHOR = datetime.date(2027, 6, 1)
_TIME = datetime.time(9, 0)
_CADENCE = "weekly"
_TARGET_ID = "999888777666555444"
_MESSAGE = "Hello!"


def _make_service() -> MemberNotificationService:
    """Return a MemberNotificationService backed by in-memory SQLite."""
    engine = create_engine(
        "sqlite:///:memory:",
        echo=False,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    return MemberNotificationService(session_factory=factory)


def _create_default(service: MemberNotificationService, name: str = "notif-a") -> None:
    """Create a notification row with default test values.

    Args:
        service: The service instance to write through.
        name: The notification name to use.
    """
    service.create(
        name=name,
        target_discord_id=_TARGET_ID,
        anchor_date_utc=_ANCHOR,
        fire_time_utc=_TIME,
        cadence=_CADENCE,
        message_template=_MESSAGE,
    )


# ---------------------------------------------------------------------------
# Finding 1 — IntegrityError discrimination
# ---------------------------------------------------------------------------


def test_non_duplicate_integrity_error_propagates_as_integrity_error() -> None:
    """A non-name-unique IntegrityError propagates, not DuplicateNotificationError.

    Regression for PR #277 finding 1: create() used to wrap every
    IntegrityError as DuplicateNotificationError regardless of cause.
    A CHECK constraint failure (or any other non-name-unique integrity
    error) must NOT be translated.
    """
    service = _make_service()

    # Simulate an IntegrityError whose .orig message does NOT contain
    # "member_notification.name" — mimics a CHECK constraint failure.
    fake_orig = Exception("CHECK constraint failed: ck_member_notification_cadence")
    fake_exc = IntegrityError("statement", {}, fake_orig)

    # Replace _session_factory with a mock whose context-manager commit raises
    # the fake CHECK-violation IntegrityError.
    mock_session = MagicMock()
    mock_session.__enter__ = MagicMock(return_value=mock_session)
    mock_session.__exit__ = MagicMock(return_value=False)
    mock_session.commit.side_effect = fake_exc
    service._session_factory = MagicMock(return_value=mock_session)

    with pytest.raises(IntegrityError) as exc_info:
        service.create(
            name="notif-check",
            target_discord_id=_TARGET_ID,
            anchor_date_utc=_ANCHOR,
            fire_time_utc=_TIME,
            cadence=_CADENCE,
            message_template=_MESSAGE,
        )

    # Must propagate as IntegrityError, not DuplicateNotificationError.
    assert not isinstance(exc_info.value, DuplicateNotificationError)


def test_genuine_duplicate_name_raises_duplicate_notification_error() -> None:
    """A name-UNIQUE violation on create() raises DuplicateNotificationError.

    Regression for PR #277 finding 1 (positive case): the genuine
    duplicate-name path must still raise DuplicateNotificationError.
    """
    service = _make_service()
    _create_default(service, name="notif-dup")

    with pytest.raises(DuplicateNotificationError) as exc_info:
        _create_default(service, name="notif-dup")

    assert exc_info.value.name == "notif-dup"


# ---------------------------------------------------------------------------
# Finding 2 — target_discord_id validation at write time
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_id",
    [
        "",  # empty
        "not-digits",  # letters and hyphens
        "123abc",  # mixed
        "01234",  # leading zero (fails round-trip)
        " 123 ",  # whitespace
    ],
)
def test_create_invalid_target_discord_id_raises_at_write_time(
    bad_id: str,
) -> None:
    """create() raises InvalidDiscordIdError for non-digit target_discord_id.

    Regression for PR #277 finding 2: a malformed id must be caught before
    the row is persisted, not at scheduler runtime.
    """
    service = _make_service()

    with pytest.raises(InvalidDiscordIdError):
        service.create(
            name="notif-bad-id",
            target_discord_id=bad_id,
            anchor_date_utc=_ANCHOR,
            fire_time_utc=_TIME,
            cadence=_CADENCE,
            message_template=_MESSAGE,
        )

    # Row must not have been persisted.
    assert service.get("notif-bad-id") is None


def test_create_valid_target_discord_id_succeeds() -> None:
    """create() with a valid all-digits snowflake succeeds.

    Finding 2 positive case: ensure the validation does not block valid ids.
    """
    service = _make_service()
    service.create(
        name="notif-valid-id",
        target_discord_id="999888777666555444",
        anchor_date_utc=_ANCHOR,
        fire_time_utc=_TIME,
        cadence=_CADENCE,
        message_template=_MESSAGE,
    )
    row = service.get("notif-valid-id")
    assert row is not None
    assert row.target_discord_id == "999888777666555444"


def test_update_invalid_target_discord_id_raises_at_write_time() -> None:
    """update() raises InvalidDiscordIdError for non-digit target_discord_id.

    Regression for PR #277 finding 2 (update path): the update handler
    must also validate target_discord_id before writing.
    """
    service = _make_service()
    _create_default(service, name="notif-upd-bad")

    with pytest.raises(InvalidDiscordIdError):
        service.update("notif-upd-bad", target_discord_id="not-a-snowflake")

    # Original row must be unchanged.
    row = service.get("notif-upd-bad")
    assert row is not None
    assert row.target_discord_id == _TARGET_ID


# ---------------------------------------------------------------------------
# Finding 3 — update() whitelist enforcement
# ---------------------------------------------------------------------------


def test_update_immutable_field_created_at_raises_value_error() -> None:
    """update() with key 'created_at' raises ValueError, does not mutate the row.

    Regression for PR #277 finding 3: setattr over arbitrary kwargs would
    allow immutable audit columns to be silently overwritten.  Note: 'name'
    cannot be tested via **fields because it is the positional lookup
    argument; 'created_at' and 'id' represent the same category.
    """
    service = _make_service()
    _create_default(service, name="notif-immut")

    with pytest.raises(ValueError, match="immutable"):
        service.update(
            "notif-immut",
            created_at=datetime.datetime(2000, 1, 1),
        )

    # Row must still exist unchanged.
    assert service.get("notif-immut") is not None


def test_update_immutable_field_id_raises_value_error() -> None:
    """update() with key 'id' raises ValueError, does not mutate the row.

    Regression for PR #277 finding 3: the surrogate PK must not be
    alterable via update().
    """
    service = _make_service()
    _create_default(service, name="notif-id-immut")

    with pytest.raises(ValueError, match="immutable"):
        service.update("notif-id-immut", id=9999)


def test_update_unknown_field_raises_value_error() -> None:
    """update() with an unknown field key raises ValueError.

    Regression for PR #277 finding 3: unknown keys must be rejected, not
    silently passed to setattr.
    """
    service = _make_service()
    _create_default(service, name="notif-unk")

    with pytest.raises(ValueError, match="immutable"):
        service.update("notif-unk", nonexistent_field="oops")


def test_update_mutable_fields_succeed() -> None:
    """update() with all whitelisted mutable fields succeeds.

    Finding 3 positive case: ensure the whitelist does not block valid
    field updates.
    """
    service = _make_service()
    _create_default(service, name="notif-mut")

    service.update(
        "notif-mut",
        target_discord_id="111222333444555666",
        anchor_date_utc=datetime.date(2028, 1, 1),
        fire_time_utc=datetime.time(14, 30),
        cadence="monthly",
        message_template="Updated message.",
        enabled=False,
    )

    row = service.get("notif-mut")
    assert row is not None
    assert row.target_discord_id == "111222333444555666"
    assert row.anchor_date_utc == datetime.date(2028, 1, 1)
    assert row.fire_time_utc == datetime.time(14, 30)
    assert row.cadence == "monthly"
    assert row.message_template == "Updated message."
    assert row.enabled is False
