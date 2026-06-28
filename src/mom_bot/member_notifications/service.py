"""In-process service layer for the per-member notification system (#269).

:class:`MemberNotificationService` is the single point of DB access for
the feature — both the Discord slash commands and the reminder scheduler
call this layer directly (no HTTP, no loopback).

Provides:

- CRUD: :meth:`~MemberNotificationService.create`,
  :meth:`~MemberNotificationService.list_all`,
  :meth:`~MemberNotificationService.get`,
  :meth:`~MemberNotificationService.update`,
  :meth:`~MemberNotificationService.delete`.
- Scheduler read: :meth:`~MemberNotificationService.list_due` — returns
  enabled notifications that are both due on today's occurrence date and
  not already sent for this occurrence.

Typed exceptions (spec § 2.4):

- :class:`DuplicateNotificationError` — ``name`` collision on create.
- :class:`NotificationNotFoundError` — absent ``name`` on get/update/delete.

Spec reference: #269 per-member notifications § 2.4.
"""

from __future__ import annotations

import datetime
import logging
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from mom_bot.member_notifications.models import (
    MemberNotification,
    MemberNotificationSent,
)
from mom_bot.member_notifications.schedule import is_occurrence_date

__all__ = [
    "DuplicateNotificationError",
    "InvalidDiscordIdError",
    "MemberNotificationService",
    "NotificationNotFoundError",
]

_logger = logging.getLogger(__name__)

_VALID_CADENCES = frozenset({"weekly", "biweekly", "monthly"})

# Fields that callers may update via update().  Immutable fields (id,
# name, created_at) and unknown keys are rejected to prevent silent
# mutation of the lookup key or audit columns.
_MUTABLE_FIELDS = frozenset(
    {
        "target_discord_id",
        "anchor_date_utc",
        "fire_time_utc",
        "cadence",
        "message_template",
        "enabled",
    }
)

# SQLite / SQLAlchemy embeds the table+column in the UNIQUE error message.
_NAME_UNIQUE_MARKER = "member_notification.name"


# ---------------------------------------------------------------------------
# Typed exceptions
# ---------------------------------------------------------------------------


class DuplicateNotificationError(Exception):
    """Raised when a ``create`` call uses an existing notification name.

    Args:
        name: The conflicting notification name.
    """

    def __init__(self, name: str) -> None:
        """Initialise with the conflicting name.

        Args:
            name: The notification name that already exists.
        """
        super().__init__(f"A notification named {name!r} already exists.")
        self.name = name


class NotificationNotFoundError(Exception):
    """Raised when a ``get``, ``update``, or ``delete`` finds no row.

    Args:
        name: The notification name that was not found.
    """

    def __init__(self, name: str) -> None:
        """Initialise with the missing name.

        Args:
            name: The notification name that was not found.
        """
        super().__init__(f"No notification named {name!r} found.")
        self.name = name


class InvalidDiscordIdError(ValueError):
    """Raised when ``target_discord_id`` is not a valid all-digits string.

    A valid Discord snowflake is a non-empty string of ASCII digits with
    no leading zeros (spec § 2.4).  The round-trip invariant
    ``str(int(s)) == s`` is used to catch both non-digit characters and
    leading-zero forms.

    Args:
        value: The invalid value that was provided.
    """

    def __init__(self, value: str) -> None:
        """Initialise with the invalid value.

        Args:
            value: The invalid ``target_discord_id`` string.
        """
        super().__init__(
            f"target_discord_id must be a non-empty all-digits string "
            f"(no leading zeros); got {value!r}."
        )
        self.value = value


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _validate_discord_id(value: str) -> None:
    """Raise :class:`InvalidDiscordIdError` if *value* is not a valid snowflake.

    A valid Discord snowflake is a non-empty string of ASCII digits with no
    leading zeros.  The round-trip invariant ``str(int(s)) == s`` is a cheap
    check that catches both non-digit characters and leading-zero forms
    (spec § 2.4).

    Args:
        value: The ``target_discord_id`` string to validate.

    Raises:
        InvalidDiscordIdError: If *value* is empty, contains non-digit
            characters, or has a leading zero.
    """
    try:
        if not value or str(int(value)) != value:
            raise InvalidDiscordIdError(value)
    except (ValueError, OverflowError) as exc:
        raise InvalidDiscordIdError(value) from exc


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class MemberNotificationService:
    """CRUD and scheduler read interface for ``member_notification`` rows.

    All database access for the per-member notification feature flows
    through this class.  Each method opens a fresh session via
    ``session_factory`` and commits or rolls back independently — no
    long-lived session is held.

    Attributes:
        _session_factory: A zero-argument callable that returns a new
            :class:`~sqlalchemy.orm.Session`.  Typically a
            :class:`~sqlalchemy.orm.sessionmaker` instance.
    """

    def __init__(
        self,
        session_factory: Callable[[], Session],
    ) -> None:
        """Initialise the service.

        Args:
            session_factory: A zero-argument callable that returns a fresh
                :class:`~sqlalchemy.orm.Session` bound to the bot database.
        """
        self._session_factory = session_factory

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def create(
        self,
        name: str,
        target_discord_id: str,
        anchor_date_utc: datetime.date,
        fire_time_utc: datetime.time,
        cadence: str,
        message_template: str,
        enabled: bool = True,
    ) -> MemberNotification:
        """Create a new member notification row.

        Args:
            name: Human-readable unique label (the slash-command lookup key).
            target_discord_id: The DM recipient's Discord snowflake as an
                opaque string.
            anchor_date_utc: The first occurrence's UTC calendar date.
            fire_time_utc: UTC time-of-day gate (minute boundary).
            cadence: One of ``'weekly'``, ``'biweekly'``, or ``'monthly'``.
            message_template: Static message body sent as the DM.
            enabled: Whether the notification is active (default ``True``).

        Returns:
            The newly created :class:`MemberNotification` row.

        Raises:
            ValueError: If *cadence* is not one of the three valid values.
            InvalidDiscordIdError: If *target_discord_id* is not a
                non-empty all-digits string (spec § 2.4).
            DuplicateNotificationError: If a notification named *name*
                already exists.
        """
        if cadence not in _VALID_CADENCES:
            raise ValueError(
                f"Invalid cadence {cadence!r}; must be one of " f"{sorted(_VALID_CADENCES)}."
            )
        _validate_discord_id(target_discord_id)

        row = MemberNotification(
            name=name,
            target_discord_id=target_discord_id,
            anchor_date_utc=anchor_date_utc,
            fire_time_utc=fire_time_utc,
            cadence=cadence,
            message_template=message_template,
            enabled=enabled,
        )
        with self._session_factory() as session:
            try:
                session.add(row)
                session.commit()
                session.refresh(row)
            except IntegrityError as exc:
                session.rollback()
                # Only translate the name-uniqueness violation; let other
                # integrity errors (CHECK constraints, etc.) propagate so
                # callers get an accurate error, not a misleading
                # DuplicateNotificationError.
                if _NAME_UNIQUE_MARKER in str(exc.orig):
                    raise DuplicateNotificationError(name) from exc
                raise
        return row

    def list_all(self) -> list[MemberNotification]:
        """Return all notification rows ordered by name.

        Returns:
            A list of all :class:`MemberNotification` rows, ordered
            alphabetically by ``name``.
        """
        with self._session_factory() as session:
            rows = (
                session.execute(select(MemberNotification).order_by(MemberNotification.name))
                .scalars()
                .all()
            )
            # Expunge so they are usable outside the session.
            session.expunge_all()
            return list(rows)

    def get(self, name: str) -> MemberNotification | None:
        """Return the notification with the given name, or None.

        Args:
            name: The notification's human-readable label.

        Returns:
            The :class:`MemberNotification` row, or ``None`` if not found.
        """
        with self._session_factory() as session:
            row = session.execute(
                select(MemberNotification).where(MemberNotification.name == name)
            ).scalar_one_or_none()
            if row is not None:
                session.expunge(row)
            return row

    def update(
        self,
        name: str,
        **fields: object,
    ) -> MemberNotification:
        """Partially update a notification row.

        Only the fields present in *fields* are updated; all others are
        left unchanged.  Supports ``enabled``, ``target_discord_id``,
        ``anchor_date_utc``, ``fire_time_utc``, ``cadence``, and
        ``message_template``.

        Args:
            name: The notification's human-readable label.
            **fields: Field-name → new-value pairs to update.

        Returns:
            The updated :class:`MemberNotification` row.

        Raises:
            ValueError: If a ``cadence`` field is provided with an invalid
                value, or if an unknown or immutable field key is supplied.
            InvalidDiscordIdError: If ``target_discord_id`` is provided
                but is not a non-empty all-digits string (spec § 2.4).
            NotificationNotFoundError: If no notification named *name*
                exists.
        """
        # Reject unknown or immutable field keys upfront.
        unknown = set(fields) - _MUTABLE_FIELDS
        if unknown:
            raise ValueError(
                f"Unknown or immutable field(s): {sorted(unknown)!r}. "
                f"Mutable fields are: {sorted(_MUTABLE_FIELDS)!r}."
            )

        if "cadence" in fields and fields["cadence"] not in _VALID_CADENCES:
            raise ValueError(
                f"Invalid cadence {fields['cadence']!r}; must be one of "
                f"{sorted(_VALID_CADENCES)}."
            )

        if "target_discord_id" in fields:
            _validate_discord_id(str(fields["target_discord_id"]))

        with self._session_factory() as session:
            row = session.execute(
                select(MemberNotification).where(MemberNotification.name == name)
            ).scalar_one_or_none()
            if row is None:
                raise NotificationNotFoundError(name)

            for field_name, value in fields.items():
                setattr(row, field_name, value)

            session.commit()
            session.refresh(row)
            session.expunge(row)
        return row

    def delete(self, name: str) -> None:
        """Delete a notification row (CASCADE removes sent-log rows).

        Args:
            name: The notification's human-readable label.

        Raises:
            NotificationNotFoundError: If no notification named *name*
                exists.
        """
        with self._session_factory() as session:
            row = session.execute(
                select(MemberNotification).where(MemberNotification.name == name)
            ).scalar_one_or_none()
            if row is None:
                raise NotificationNotFoundError(name)
            session.delete(row)
            session.commit()

    def list_due(
        self,
        today: datetime.date,
        now_time: datetime.time,
    ) -> list[MemberNotification]:
        """Return enabled notifications due for the current tick.

        Applies the three-part due-occurrence predicate (spec § 2.3a):

        1. ``enabled = True``
        2. Not already sent for today's occurrence (``id NOT IN sent_today``)
        3. ``is_occurrence_date(anchor_date_utc, cadence, today)`` AND
           ``fire_time_utc <= now_time``

        The occurrence-date math runs in Python (not SQL) because the
        monthly clamp uses :func:`calendar.monthrange`, which is not
        portable SQL.  The enabled + not-sent filter is applied in SQL
        first to keep the Python iteration small.

        Args:
            today: The UTC calendar date for this tick.
            now_time: The UTC time (microseconds zeroed) for this tick.

        Returns:
            A list of :class:`MemberNotification` rows that are due and
            have not yet been sent for this occurrence.
        """
        with self._session_factory() as session:
            sent_today_subq = (
                select(MemberNotificationSent.member_notification_id).where(
                    MemberNotificationSent.occurrence_date_utc == today
                )
            ).scalar_subquery()

            candidates = (
                session.execute(
                    select(MemberNotification)
                    .where(MemberNotification.enabled.is_(True))
                    .where(MemberNotification.id.not_in(sent_today_subq))
                )
                .scalars()
                .all()
            )

            due: list[MemberNotification] = []
            for row in candidates:
                if row.fire_time_utc > now_time:
                    continue
                if is_occurrence_date(row.anchor_date_utc, row.cadence, today):
                    due.append(row)
                    session.expunge(row)

            return due
