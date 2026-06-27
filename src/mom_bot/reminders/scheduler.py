"""Minute-level reminder scheduler for mom-bot (plan §§ 3-6).

Provides :class:`ReminderScheduler`, an async loop that wakes once per
minute, queries eligible reminders, and delivers them to Discord.

Architecture summary
--------------------
- **Single-replica assumption**: ``maxReplicas = 1`` on the Container App
  makes concurrent fires unlikely.  The UNIQUE constraint on
  ``reminder_sent(reminder_id, fire_date_utc)`` is cheap insurance — the
  loser of any race sees ``IntegrityError`` and skips (plan § 4 Concurrency
  note; revision-rollout overlap test in ``tests/test_reminders_scheduler.py``
  validates this path explicitly).
- **Send ordering**: INSERT the ``reminder_sent`` row first to claim the
  per-day slot, then attempt the Discord send (plan § 3 row 8).  Permanent
  errors leave the row in place; transient errors delete the row before
  re-raising so the next tick retries.
- **Gateway readiness gate**: two levels:
  1. Coarse (cold start) — caller must ``await bot.wait_until_ready()``
     before calling :meth:`~ReminderScheduler.run`.
  2. Per-tick — each iteration checks ``bot.is_ready()``; if False the
     tick is skipped (no insert, no send) to handle mid-day gateway
     reconnects.  ``wait_until_ready()`` is one-shot and does not re-block
     on reconnect (plan § 6).
- **No ``time.sleep``**: all waits are ``await asyncio.sleep(...)`` so the
  event loop stays responsive and ``time-machine`` interacts correctly in
  tests.
- **Liveness sentinel**: the scheduler calls
  :func:`mom_bot.health.record_heartbeat` at the start of every tick
  (before the readiness guard) so the Container Apps httpGet probe against
  ``GET /healthz`` can confirm the loop is alive even when the gateway is
  disconnected.  The legacy sentinel file (``/tmp/scheduler-heartbeat``) is
  also touched for backward compatibility during any transition period.

Discord error taxonomy (plan § 5)
----------------------------------
Each send error falls into one of two buckets:

Drop (row stays — permanent failure, no retry)
  - ``discord.Forbidden`` (HTTP 403): bot lacks channel permission.
  - ``discord.NotFound`` (HTTP 404): channel was deleted.
  - Any other ``Exception``: unknown failure mode; logged CRITICAL.

Retry (row deleted before re-raise — transient failure)
  - ``discord.HTTPException`` with ``status >= 500``: server-side transient.
  - ``discord.RateLimited`` (HTTP 429): treated as transient.
  - ``aiohttp.ClientError``: network-layer transient.
  - ``asyncio.TimeoutError``: network-layer transient.

The deleting-then-raising pattern means the next scheduler tick (≤60 s
later) retries the send via the same fire-time predicate (``<=``, not
``==``), which catches the retry within the same calendar day.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import aiohttp
import discord
from sqlalchemy import select
from sqlalchemy.orm import Session

import mom_bot.health as health
from mom_bot.member_notifications.service import MemberNotificationService
from mom_bot.reminders.calendar import (
    is_end_of_tank_date,
    is_tank_week_headsup_date,
)
from mom_bot.reminders.models import Reminder, ReminderSent  # noqa: F401
from mom_bot.reminders.sent_store import (
    MemberNotificationSentStore,
    ReminderSentStore,
)

__all__ = ["ReminderScheduler"]

_logger = logging.getLogger(__name__)

_HEARTBEAT_PATH = Path("/tmp/scheduler-heartbeat")


class ReminderScheduler:
    """Async loop that fires Discord reminders on a minute-level tick.

    Attributes:
        _bot: The discord.py client (or a test double implementing
            ``is_ready()`` and ``get_channel()``).
        _guild: Optional guild reference used to resolve members for DM
            delivery.  ``None`` when the scheduler is used in channel-only
            mode (e.g. the 23 pre-existing channel tests).
        _session_factory: A zero-argument callable that returns a new
            :class:`~sqlalchemy.orm.Session` bound to the bot database.
        _tick_seconds: Seconds to sleep between ticks.  Defaults to 60.0
            for production; tests inject smaller values.
    """

    def __init__(
        self,
        bot: discord.Client,
        session_factory: Callable[[], Session],
        tick_seconds: float = 60.0,
        guild: Any = None,
    ) -> None:
        """Initialise the scheduler.

        Args:
            bot: The discord.py client instance.  Must implement
                ``is_ready()`` and ``get_channel(channel_id)``.
            session_factory: A zero-argument callable that returns a fresh
                :class:`~sqlalchemy.orm.Session` for each tick.  Typically
                a :class:`~sqlalchemy.orm.sessionmaker` instance.
            tick_seconds: Seconds to sleep between scheduler ticks.
                Default 60.0; inject a smaller value in tests.
            guild: Optional :class:`discord.Guild` (or test double)
                implementing ``get_member(int)`` and
                ``fetch_member(int)`` for DM delivery.  When ``None``,
                the DM branch is skipped silently (channel-only mode).
        """
        self._bot = bot
        self._guild = guild
        self._session_factory = session_factory
        self._tick_seconds = tick_seconds

    async def run(self) -> None:
        """Run the scheduler loop until the task is cancelled.

        Each iteration:

        1. Touch the liveness sentinel file at ``/tmp/scheduler-heartbeat``.
        2. If ``bot.is_ready()`` is False: sleep and continue (per-tick
           readiness guard for mid-day reconnects).
        3. Query eligible reminders using the fire-time predicate (plan § 4).
        4. For each eligible reminder: attempt to INSERT a ``reminder_sent``
           row.  If the INSERT collides (UNIQUE), skip this reminder (another
           scheduler won the race).
        5. Attempt the Discord send.  Handle errors per the taxonomy in the
           module docstring.
        6. ``await asyncio.sleep(tick_seconds)``.

        Raises:
            asyncio.CancelledError: Propagated cleanly when the task is
                cancelled (e.g. on SIGTERM).
        """
        while True:
            # Step 1 — liveness sentinel (before readiness guard so a
            # healthy-but-disconnected scheduler still updates the heartbeat).
            # The in-process heartbeat is the primary signal; the legacy file
            # touch is kept for operator debugging convenience.
            health.record_heartbeat()
            try:
                _HEARTBEAT_PATH.touch()
            except OSError:
                # Non-fatal — the probe will eventually fail if this persists.
                _logger.warning("scheduler: could not touch %s", _HEARTBEAT_PATH)

            # Step 2 — per-tick readiness guard.
            if not self._bot.is_ready():
                _logger.debug("scheduler: bot not ready; skipping tick")
                await asyncio.sleep(self._tick_seconds)
                continue

            # Step 3 — compute fire-time predicate parameters once per tick.
            now_utc = datetime.datetime.now(datetime.UTC)
            today_weekday = now_utc.weekday()
            now_time = now_utc.time().replace(microsecond=0)
            today_date = now_utc.date()

            try:
                await self._process_tick(today_weekday, now_time, today_date)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Transient per-tick errors (e.g. 5xx, RateLimited,
                # aiohttp, timeout) are logged inside _handle_reminder and
                # the row is already deleted for retry.  The loop continues
                # so the next tick retries; we do not crash the scheduler.
                _logger.debug("scheduler: per-tick error suppressed; will retry next tick")

            # Step 6 — sleep before the next tick.
            await asyncio.sleep(self._tick_seconds)

    async def _process_tick(
        self,
        today_weekday: int,
        now_time: datetime.time,
        today_date: datetime.date,
    ) -> None:
        """Query and fire all eligible reminders for this tick.

        Opens a fresh session for the tick, queries eligible reminders,
        and handles each one via :meth:`_handle_reminder`.

        Args:
            today_weekday: UTC weekday integer (Mon=0, Sun=6).
            now_time: UTC time with microseconds zeroed, used for the
                ``fire_time_utc <= now_time`` predicate.
            today_date: UTC calendar date used for idempotency attribution.
        """
        with self._session_factory() as session:
            # Step 1 — collect the full eligible set (weekday + time + not
            # sent today).  Calendar-conditional filtering happens below in
            # Python; the eligible set per tick is small (2-4 rows) so the
            # cost is negligible and it keeps calendar logic unit-testable.
            sent_today_subq = (
                select(ReminderSent.reminder_id).where(ReminderSent.fire_date_utc == today_date)
            ).scalar_subquery()

            eligible = (
                session.execute(
                    select(Reminder)
                    .where(Reminder.weekday == today_weekday)
                    .where(Reminder.fire_time_utc <= now_time)
                    .where(Reminder.id.not_in(sent_today_subq))
                )
                .scalars()
                .all()
            )

            # Step 2 — apply calendar-condition filters.  Rows with a
            # month_condition value only fire on their specific calendar date;
            # NULL-condition rows always pass.
            kept: list[Reminder] = []
            for reminder in eligible:
                cond = reminder.month_condition
                if cond is None:
                    kept.append(reminder)
                elif cond == "tank_week_headsup":
                    if is_tank_week_headsup_date(today_date):
                        kept.append(reminder)
                elif cond == "tank_week_end":
                    if is_end_of_tank_date(today_date):
                        kept.append(reminder)
                # Unknown values are silently dropped (defensive).

            # Step 3 — suppression pre-filter (spec §2.4).  On the
            # end-of-tank date, any NULL-condition row sharing the same
            # (channel_id, weekday, fire_time_utc) slot as a tank_week_end
            # row must be suppressed — even if the tank_week_end row was
            # already sent in a prior tick of the same day.
            #
            # We compute the suppression set in two parts:
            # (a) tank_week_end rows in the current kept set (first tick), and
            # (b) tank_week_end rows already sent today (subsequent ticks).
            #
            # Part (a): tank_week_end rows in the current eligible kept set.
            tank_end_slots: set[tuple[int, int, datetime.time]] = {
                (r.channel_id, r.weekday, r.fire_time_utc)
                for r in kept
                if r.month_condition == "tank_week_end"
            }

            # Part (b): tank_week_end rows already sent today (handles the
            # multi-tick case where TankEnd fires on tick 1 but Hydra has
            # not yet fired — without this, Hydra would fire on tick 2).
            if is_end_of_tank_date(today_date):
                already_sent_tank_end = (
                    session.execute(
                        select(Reminder)
                        .join(
                            ReminderSent,
                            (ReminderSent.reminder_id == Reminder.id)
                            & (ReminderSent.fire_date_utc == today_date),
                        )
                        .where(Reminder.month_condition == "tank_week_end")
                        .where(Reminder.weekday == today_weekday)
                    )
                    .scalars()
                    .all()
                )
                for r in already_sent_tank_end:
                    tank_end_slots.add((r.channel_id, r.weekday, r.fire_time_utc))

            if tank_end_slots:
                survivors: list[Reminder] = []
                for reminder in kept:
                    if reminder.month_condition is None:
                        slot = (
                            reminder.channel_id,
                            reminder.weekday,
                            reminder.fire_time_utc,
                        )
                        if slot in tank_end_slots:
                            _logger.debug(
                                "scheduler: suppressing reminder %r on %s "
                                "(replaced by tank_week_end row)",
                                reminder.name,
                                today_date,
                            )
                            continue
                    survivors.append(reminder)
            else:
                survivors = kept

            # Step 4 — iterate survivors and send.  No side effects before
            # this point for any suppressed row.
            for reminder in survivors:
                await self._handle_reminder(reminder, today_date, session)

        # Step 5 — DM branch: process per-member notifications.  Runs AFTER
        # the channel loop (spec § 2.3 finding 8) in a fresh session.
        # The guild reference is required for member resolution; if absent
        # (channel-only mode), skip this branch silently.
        if self._guild is not None:
            dm_service = MemberNotificationService(self._session_factory)
            due_notifications = dm_service.list_due(today_date, now_time)
            # Each notification needs its own fresh session for the
            # insert-first idempotency claim.  Wrap each call so a single
            # member's failure (including transient re-raises) does not abort
            # the whole loop — the next member's DM still fires this tick.
            # Transient errors already call unmark() before re-raising inside
            # _handle_member_notification, so the unmarked row is retried next
            # tick regardless of whether we catch the re-raise here.
            for notif in due_notifications:
                try:
                    await self._handle_member_notification(notif, today_date)
                except Exception:
                    _logger.exception(
                        "scheduler: DM loop: unhandled exception for "
                        "notification %r — continuing to next notification",
                        notif.name,
                    )

    async def _handle_reminder(
        self,
        reminder: Reminder,
        today_date: datetime.date,
        session: Session,
    ) -> None:
        """Attempt to fire one reminder: INSERT then send.

        Follows the insert-then-send-with-drop-on-failure pattern (plan
        § 5).  If the INSERT collides (UNIQUE), another scheduler won the
        race and this method returns without attempting a send.

        Args:
            reminder: The :class:`~mom_bot.reminders.models.Reminder` row
                to fire.
            today_date: The UTC calendar date to attribute the fire to.
            session: The active SQLAlchemy session for this tick.
        """
        store = ReminderSentStore(session)

        # Step 4 — INSERT the idempotency row.
        inserted = store.mark_sent(reminder.id, today_date)
        if not inserted:
            # UNIQUE collision — another scheduler claimed this slot.
            _logger.debug(
                "scheduler: UNIQUE collision for reminder %r on %s; skipping",
                reminder.name,
                today_date,
            )
            return

        # Step 5 — attempt the Discord send.
        channel = self._bot.get_channel(reminder.channel_id)
        if channel is None:
            _logger.error(
                "scheduler: channel %d not found for reminder %r; dropping",
                reminder.channel_id,
                reminder.name,
            )
            return

        message = reminder.message_template
        allowed_mentions: discord.AllowedMentions | None = None
        if reminder.role_mention_id is not None:
            message = f"<@&{reminder.role_mention_id}> {message}"
            # Without AllowedMentions(roles=True), Discord renders the
            # <@&id> markup as a clickable mention visually, but suppresses
            # the actual ping/notification to role members — this is
            # Discord's safe-default behavior to prevent runaway role pings.
            # We explicitly opt in only when role_mention_id IS NOT NULL,
            # matching the per-env configuration intent from KV (#51).
            allowed_mentions = discord.AllowedMentions(roles=True)

        try:
            if allowed_mentions is not None:
                await channel.send(  # type: ignore[union-attr]
                    message, allowed_mentions=allowed_mentions
                )
            else:
                await channel.send(message)  # type: ignore[union-attr]
            _logger.info(
                "scheduler: fired reminder %r to channel %d on %s",
                reminder.name,
                reminder.channel_id,
                today_date,
            )
        except (discord.Forbidden, discord.NotFound) as exc:
            # Permanent failure — row stays, no retry.
            _logger.error(
                "scheduler: permanent Discord error for reminder %r on %s: "
                "%s — dropping (row stays)",
                reminder.name,
                today_date,
                exc,
            )
        except (TimeoutError, discord.RateLimited, aiohttp.ClientError) as exc:
            # Transient failure — delete row so next tick retries.
            store.unmark(reminder.id, today_date)
            _logger.error(
                "scheduler: transient error for reminder %r on %s: " "%s — row deleted for retry",
                reminder.name,
                today_date,
                exc,
            )
            raise
        except discord.HTTPException as exc:
            if exc.status >= 500:
                # Transient server-side error — delete row so next tick retries.
                store.unmark(reminder.id, today_date)
                _logger.error(
                    "scheduler: HTTPException status=%d for reminder %r "
                    "on %s — row deleted for retry: %s",
                    exc.status,
                    reminder.name,
                    today_date,
                    exc,
                )
                raise
            # Other HTTP errors (e.g. 400, 401) — treat as permanent drop.
            _logger.error(
                "scheduler: HTTPException status=%d for reminder %r on %s: "
                "%s — dropping (row stays)",
                exc.status,
                reminder.name,
                today_date,
                exc,
            )
        except Exception as exc:
            # Unknown failure mode — log CRITICAL, leave row, do not retry.
            _logger.critical(
                "scheduler: unexpected error for reminder %r on %s: " "%s — dropping (row stays)",
                reminder.name,
                today_date,
                exc,
            )

    async def _handle_member_notification(
        self,
        notif: Any,
        today_date: datetime.date,
    ) -> None:
        """Attempt to fire one per-member DM notification: INSERT then send.

        Follows the same insert-first, send-second, drop-on-failure pattern
        as :meth:`_handle_reminder` (spec § 2.3).

        Order (spec § 2.3 finding 6 — INSERT-first is mandatory):

        1. INSERT the idempotency row to claim the occurrence slot.
        2. Resolve the member by ``target_discord_id`` via
           ``guild.get_member`` (cache) or ``guild.fetch_member`` (miss).
        3. Send the DM via ``member.send(message_template)``.

        Error taxonomy (spec § 2.3 finding 7):

        - ``discord.Forbidden`` → permanent drop (row stays), DMs closed.
        - ``discord.NotFound`` (from resolution or send) → permanent drop.
        - ``discord.RateLimited`` / ``discord.HTTPException`` status >= 500 /
          ``aiohttp.ClientError`` / ``asyncio.TimeoutError`` → transient:
          ``unmark`` the row and re-raise so the next tick retries.
        - Other ``HTTPException`` (4xx) or unexpected ``Exception`` →
          permanent drop (row stays), logged.

        Args:
            notif: The :class:`~mom_bot.member_notifications.models.\
MemberNotification` row to fire.
            today_date: The UTC calendar date for this occurrence.
        """
        with self._session_factory() as session:
            store = MemberNotificationSentStore(session)

            # Step 1 — INSERT the idempotency row first (insert-first ordering
            # — spec § 2.3 finding 6).  This MUST happen before member
            # resolution so that a departed member does not cause an infinite
            # per-tick loop (NotFound without a sent row → re-selected every
            # tick on the same calendar day).
            inserted = store.mark_sent(notif.id, today_date)
            if not inserted:
                # UNIQUE collision — already fired this occurrence.
                _logger.debug(
                    "scheduler: DM UNIQUE collision for notification %r " "on %s; skipping",
                    notif.name,
                    today_date,
                )
                return

            # Step 2 — resolve the target member by discord_id.
            discord_id = int(notif.target_discord_id)
            member = self._guild.get_member(discord_id)
            if member is None:
                try:
                    member = await self._guild.fetch_member(discord_id)
                except discord.NotFound:
                    # Member has left the guild — permanent drop, row stays.
                    _logger.error(
                        "scheduler: DM NotFound (member left) for "
                        "notification %r on %s — dropping (row stays)",
                        notif.name,
                        today_date,
                    )
                    return
                except discord.Forbidden as exc:
                    # Should not happen at fetch time, but handle defensively.
                    _logger.error(
                        "scheduler: DM Forbidden during fetch_member for "
                        "notification %r on %s: %s — dropping (row stays)",
                        notif.name,
                        today_date,
                        exc,
                    )
                    return
                except (
                    TimeoutError,
                    discord.RateLimited,
                    aiohttp.ClientError,
                ) as exc:
                    # Transient lookup failure — unmark row so next tick
                    # retries (spec § 2.3 finding 7, resolve step taxonomy).
                    store.unmark(notif.id, today_date)
                    _logger.error(
                        "scheduler: transient fetch_member error for "
                        "notification %r on %s: %s — row deleted for retry",
                        notif.name,
                        today_date,
                        exc,
                    )
                    raise
                except discord.HTTPException as exc:
                    if exc.status >= 500:
                        # Transient server-side error during member lookup.
                        store.unmark(notif.id, today_date)
                        _logger.error(
                            "scheduler: fetch_member HTTPException "
                            "status=%d for notification %r on %s "
                            "— row deleted for retry: %s",
                            exc.status,
                            notif.name,
                            today_date,
                            exc,
                        )
                        raise
                    # Other 4xx — permanent drop (row stays).
                    _logger.error(
                        "scheduler: fetch_member HTTPException "
                        "status=%d for notification %r on %s: %s "
                        "— dropping (row stays)",
                        exc.status,
                        notif.name,
                        today_date,
                        exc,
                    )
                    return

            # Step 3 — send the DM.
            try:
                await member.send(notif.message_template)
                _logger.info(
                    "scheduler: DM sent for notification %r to member %s " "on %s",
                    notif.name,
                    notif.target_discord_id,
                    today_date,
                )
            except (discord.Forbidden, discord.NotFound) as exc:
                # Permanent failure — row stays, no retry.
                _logger.error(
                    "scheduler: permanent DM error for notification %r "
                    "on %s: %s — dropping (row stays)",
                    notif.name,
                    today_date,
                    exc,
                )
            except (TimeoutError, discord.RateLimited, aiohttp.ClientError) as exc:
                # Transient failure — delete row so next tick retries.
                store.unmark(notif.id, today_date)
                _logger.error(
                    "scheduler: transient DM error for notification %r "
                    "on %s: %s — row deleted for retry",
                    notif.name,
                    today_date,
                    exc,
                )
                raise
            except discord.HTTPException as exc:
                if exc.status >= 500:
                    # Transient server-side error.
                    store.unmark(notif.id, today_date)
                    _logger.error(
                        "scheduler: DM HTTPException status=%d for "
                        "notification %r on %s — row deleted for retry: %s",
                        exc.status,
                        notif.name,
                        today_date,
                        exc,
                    )
                    raise
                # Other 4xx — permanent drop.
                _logger.error(
                    "scheduler: DM HTTPException status=%d for "
                    "notification %r on %s: %s — dropping (row stays)",
                    exc.status,
                    notif.name,
                    today_date,
                    exc,
                )
            except Exception as exc:
                # Unknown — log CRITICAL, leave row, no retry.
                _logger.critical(
                    "scheduler: unexpected DM error for notification %r "
                    "on %s: %s — dropping (row stays)",
                    notif.name,
                    today_date,
                    exc,
                )
