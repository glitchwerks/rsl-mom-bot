"""mom_bot.member_notifications — per-member recurring DM notifications.

Provides the models, service layer, and Discord slash-command handlers for
the #269 per-member notification system.  Officers manage notifications via
five guild-scoped slash commands; the reminder scheduler delivers DMs by
reading from this package's service layer.

Sub-modules
-----------
- :mod:`models` — SQLAlchemy ORM tables ``member_notification`` and
  ``member_notification_sent``.
- :mod:`schedule` — pure occurrence-date math (``is_occurrence_date``,
  ``clamped_anchor_day``) with no DB or Discord dependency.
- :mod:`service` — :class:`MemberNotificationService` for CRUD and the
  scheduler's ``list_due`` query.
- :mod:`commands` — Discord slash-command handlers and ``register()``.
"""
