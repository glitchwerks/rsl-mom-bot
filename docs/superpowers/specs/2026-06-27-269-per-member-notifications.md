---
title: "Spec — #269 per-member notification system (standalone recurring DMs)"
issue: 269
milestone: v1.2
date: 2026-06-27
status: proposed
touches:
  - src/mom_bot/member_notifications/__init__.py
  - src/mom_bot/member_notifications/models.py
  - src/mom_bot/member_notifications/service.py
  - src/mom_bot/member_notifications/commands.py
  - src/mom_bot/reminders/scheduler.py
  - src/mom_bot/reminders/sent_store.py
  - src/mom_bot/main.py
  - migrations/versions/
  - tests/member_notifications/test_commands.py
  - tests/member_notifications/test_schedule.py
  - tests/test_reminders_scheduler.py
skills_relevant:
  - python
---

# Spec — #269 per-member notification system

**Issue:** [glitchwerks/rsl-mom-bot#269](https://github.com/glitchwerks/rsl-mom-bot/issues/269)
**Milestone:** v1.2 (Phase B — independent, separately-mergeable stream)
**Delivery:** DM to the targeted member.
**Schedule:** STANDALONE RECURRING via ANCHOR + CADENCE (`anchor_date_utc` + `fire_time_utc` + `cadence` ∈ weekly/biweekly/monthly; "tied-to-another-reminder" and event/calendar conditioning both CUT from v1.2, confirmed by user). See § 2.3a.
**Scheduler:** EXTEND the existing reminder scheduler with a DM delivery target (confirmed by user) — one tick loop, not two.
**Officer surface:** Discord slash commands ONLY (user decision). The earlier sidecar HTTP CRUD design is DROPPED — no HTTP endpoints, no Bearer surface, no HTTP status codes. Member targeting is via a native `discord.Member` command parameter (rename-safe by construction). See § 2.4–§ 2.6.

> `unverified:` The issue body of #269 could not be read in the authoring session (GitHub read tools unavailable to this sub-agent). Domain rules below come from the user's brief (authoritative — user owns the issue).

---

## Recommendation

YES (high confidence) — add ONE new table (`member_notification`) read by the *existing* scheduler tick, managed by a small set of **Discord slash commands** that call an **in-process service layer** directly (no HTTP). The only genuinely new runtime capability is a **DM delivery branch** on the existing send path, gated by the `Reminder.delivery_target` column that **Phase A introduces** (see #268 spec § 6). Standalone-recurring-only keeps this to a weekday+time schedule with zero coupling to base reminders. Member targeting uses Discord's native `discord.Member` picker, which eliminates the username-resolution race entirely.

---

## 1. Context

Officers want custom recurring notifications targeted at specific members (e.g. "Spike", "Ash"), managed via **Discord slash commands**, delivered as a DM to the targeted member.

### Current state (verified)

- The bot already has a slash-command subsystem: `post_conditions/commands.py` defines `app_commands` handlers and a `register(tree, ...)` function called once at startup (`src/mom_bot/post_conditions/commands.py:243-285`, verified). Registration happens in `setup_hook` via `tree.copy_global_to(guild=...)` + `tree.sync(guild=...)` — commands are **guild-scoped** (`src/mom_bot/main.py:171-175`, verified).
- The `post_conditions` commands are **per-invoking-user** (`interaction.user.id`), explicitly "no target-user parameter and no admin override" (`commands.py:11-13`, verified). #269's commands differ: they target OTHER members, so they need an officer-permission gate the per-user commands never required (§ 2.6).
- Error UX in `post_conditions` is **ephemeral interaction replies** with module-level message constants (`_LINK_YOUR_ACCOUNT_MSG`, `_OPS_ERROR_MSG`), sent via `interaction.followup.send(..., ephemeral=True)` after `await interaction.response.defer(ephemeral=True)` (`commands.py:53-58, 86-92`, verified). No HTTP status codes.
- The role-sync table `MemberRoleSyncState` is the persistence pattern to mirror: `discord_id TEXT PK`, opaque-string discord IDs never cast to int (`src/mom_bot/sidecar/models.py:64-66`, verified).
- The reminder scheduler tick reads `Reminder` rows and sends to a **channel** only — no DM branch (`src/mom_bot/reminders/scheduler.py:239-266`, verified). The scheduler has `bot`/`guild`/`session_factory` in scope.
- **No officer/admin authorization gate exists in code today** — a `src/`-wide grep for `default_permissions`, `app_commands.check`, `manage_guild`, `administrator`, `require_admin_role`, `officer` returned **zero matches** (verified). The convention is *documented* but not *implemented* — see § 2.6 (this is a flagged sub-decision).

---

## 2. Design

### 2.1 Reuse the `Reminder.delivery_target` column (introduced by Phase A)

Phase A (#268) adds `Reminder.delivery_target` (`"channel"` default, NOT NULL). Phase B does **not** introduce it — it depends on Phase A having merged. The scheduler gains a DM-delivery branch keyed on this column.

> **Simplicity-first decision:** per-member notifications are stored in a SEPARATE table (`member_notification`), NOT as `Reminder` rows with `delivery_target="dm"`. Rationale: `Reminder` rows are clan-wide and seeded from KV; per-member notifications are officer-managed records with a target `discord_id` and no channel/role. Forcing them into `Reminder` would mean a nullable `target_discord_id` column on `Reminder` that is meaningless for every existing row. A dedicated table is cleaner and the scheduler reads both. The `delivery_target` column still earns its place because the scheduler's *send dispatch* branches on delivery kind uniformly — see § 2.3.

### 2.2 New table — `member_notification` (module placement)

**Module placement decision: a new `src/mom_bot/member_notifications/` package** holding `models.py` (the two ORM tables), `service.py` (in-process CRUD + the scheduler's read query), and `commands.py` (the slash-command handlers). Justification (simplicity-first):

- The feature is self-contained and officer-facing; a dedicated package keeps the slash commands, their service, and their models co-located (mirrors how `post_conditions/` bundles `commands.py` + `client.py` + `views.py`).
- It does NOT fold into `reminders/` because the scheduler should depend on a *clean read interface* (`service.list_due(...)` / a query helper), not reach into another concern's table internals. The scheduler imports the model + a query function from `member_notifications`; that is the only cross-package coupling.
- The idempotency **store** is the one exception: it stays in `reminders/sent_store.py` beside `ReminderSentStore` (§ 2.3, finding 10) because the scheduler already owns that module and the two stores are structurally identical. The `member_notification` / `member_notification_sent` **tables** live in `member_notifications/models.py`; the **store class** lives in `reminders/sent_store.py`. (If a reviewer prefers strict co-location, moving `MemberNotificationSentStore` into `member_notifications/` is a no-cost alternative — flagged, not blocking.)

The table mirrors the `MemberRoleSyncState` conventions (`discord_id` opaque TEXT):

```python
class MemberNotification(Base):
    __tablename__ = "member_notification"
    id: Mapped[int]                        # surrogate PK, autoincrement
    name: Mapped[str]                      # human label, UNIQUE (CRUD lookup key)
    target_discord_id: Mapped[str]         # TEXT, opaque — the DM recipient
    anchor_date_utc: Mapped[datetime.date] # DATE — the FIRST occurrence's date
    fire_time_utc: Mapped[datetime.time]   # TIME — time-of-day; minute-boundary CHECK
    cadence: Mapped[str]                    # TEXT NOT NULL; CHECK in (weekly|biweekly|monthly)
    message_template: Mapped[str]          # static string; <TODO> until officer sets
    enabled: Mapped[bool]                  # soft on/off without delete
    created_at / updated_at
```

**Column-shape justification (anchor + cadence, kept as two columns).** The schedule is `anchor_date_utc` (DATE) + `fire_time_utc` (TIME) + `cadence` (enum). I deliberately keep **date and time as separate columns** rather than folding them into a single `anchor_start_utc` DATETIME:

- The scheduler predicate compares **two independent quantities**: an *occurrence-date* test (date arithmetic against `anchor_date_utc` — day-deltas mod 7/14, or clamped day-of-month) and a *time-of-day* gate (`now_time >= fire_time_utc`). A combined DATETIME would have to be decomposed back into `.date()` and `.time()` on every tick anyway, so the split is the natural shape.
- It mirrors the existing `Reminder` columns (`fire_time_utc` is already a bare `Time`, `reminders/models.py:92`), keeping the dialect-aware minute-boundary CHECK reusable verbatim.
- `anchor_date_utc` is a pure DATE (no time component), which makes `(today - anchor_date_utc).days` a clean integer with no DST/partial-day ambiguity.

The **weekly cadence subsumes the old `weekday`+`time` model**: the anchor's weekday is `anchor_date_utc.weekday()`, and weekly stepping fires on every date `anchor + 7k`. There is no separate `weekday` column — it is derived. `cadence` is `TEXT NOT NULL` with a dialect-aware CHECK `cadence IN ('weekly','biweekly','monthly')` (no NULL case, so no `IS NULL OR` guard is needed — contrast Phase A's `month_condition` which IS nullable; the dialect-aware-CHECK *authoring* pattern from migration `0002` still applies).

Idempotency log: reuse the existing `ReminderSent` pattern with a sibling table `member_notification_sent(member_notification_id FK CASCADE, occurrence_date_utc, sent_at)` and `UNIQUE(member_notification_id, occurrence_date_utc)`. **The pattern carries over unchanged** — the only difference from `ReminderSent` is the column's *semantic name*: it is the **occurrence date** (today's date when the notification fires), which for a per-day-firing notification is exactly today, identical in shape to `fire_date_utc`. The per-occurrence "fire at most once" guarantee is the same UNIQUE-constraint mechanism (`reminders/models.py:132-139` pattern). (Implementation may keep the column named `fire_date_utc` for literal parity with `ReminderSent`; this spec uses `occurrence_date_utc` to make the cadence semantics explicit. Either name is acceptable — the value is today's date at fire time.)

> **No uniqueness on `(target_discord_id, anchor_date_utc, fire_time_utc, cadence)` (finding 9 — by design).** There is intentionally NO constraint preventing two notifications targeting the same member on the same schedule. If an officer creates two such notifications, **both fire** — each is an independent row with its own `name` and its own `member_notification_sent` record keyed by its own notification `id`. Accepted operator configuration, not an accident. The only uniqueness is on `name`. Per-occurrence idempotency is per-notification, not per-member-schedule.

> **Why `target_discord_id` and not username:** the cross-cutting requirement says standardize on `discord_id` for new persisted records (username keys are rename-fragile). The discord_id is acquired via the native `discord.Member` command picker (§ 2.6), which reads `member.id` directly — no username lookup, rename-safe by construction. Confirmed by user.

### 2.3 Scheduler DM branch (the one new runtime capability)

The existing tick (`_process_tick`, `scheduler.py:187-205`) gains a second query over `member_notification` (where `enabled = true`), filtered by the **due-occurrence predicate** (§ 2.3a) instead of the channel reminders' plain `weekday == today`. For each due member notification, the order is **INSERT-first, then resolve, then send** — this ordering is a hard contract, not an incidental detail:

1. **INSERT the `member_notification_sent` idempotency row FIRST** (claim the slot — same insert-then-send pattern as `_handle_reminder`, `scheduler.py:225-236`). This MUST happen before member resolution.
2. **Resolve** the target member by `target_discord_id` via `guild.get_member(int(discord_id))` (cache) or `guild.fetch_member(...)` on miss — NOT by username.
3. **Send:** `await member.send(message_template)`.

**Why INSERT-first is mandatory (finding 6):** if a targeted member has **left the guild**, `fetch_member` raises `discord.NotFound` at step 2. Treat that as a **permanent drop — row stays, no retry** (identical to channel `NotFound` at `scheduler.py:273`). Without the insert-first claim, a departed member would produce no `member_notification_sent` row, so the not-sent-today predicate would re-select the notification on **every subsequent tick**, raising `NotFound` forever — an infinite per-tick retry with no record. Insert-first means the failed resolution still consumed the day's slot, so it is attempted at most once per date.

4. **Error taxonomy (finding 7 — state explicitly for the DM branch), mirroring `_handle_reminder` (`scheduler.py:273-321`):**
   - `discord.Forbidden` (recipient has DMs closed / blocks the bot) → **permanent drop, row stays, no retry**. Retrying a closed-DM every tick is pointless.
   - `discord.NotFound` (member left the guild, per step 2) → **permanent drop, row stays, no retry**.
   - `discord.RateLimited` / `discord.HTTPException` with `status >= 500` / `aiohttp.ClientError` / `asyncio.TimeoutError` → **transient: `unmark` the sent row, re-raise** so the next tick retries within the same calendar day (the `<=` time predicate catches it).
   - Other `HTTPException` (4xx) and any unexpected `Exception` → permanent drop (row stays), logged, matching the channel branch.

> **Simplicity-first:** the DM send does NOT route through the sidecar's `/api/notify` HTTP endpoint. The scheduler already holds the `bot`/`guild` objects and `member.send()` is one call — adding an internal HTTP round-trip would be pure overhead. The `/api/notify` endpoint stays as-is for request-driven one-off DMs; the scheduler calls `member.send()` directly, reusing the exact error taxonomy it already implements for channels.

The `Reminder.delivery_target` column is consulted for `Reminder` rows (all `"channel"` today); the `member_notification` table is inherently DM. A small shared `_deliver(target_kind, ...)` helper keeps the send+error-taxonomy logic in one place for both row sources (refactor opportunity — see § 4, behavior-preserving).

**Branch ordering & shared-session behavior (finding 8).** The `member_notification` (DM) query runs **after** the existing `Reminder` (channel) loop within the SAME `_process_tick` session and tick. The two branches share one SQLAlchemy session (the per-tick `session_factory()` context, `scheduler.py:187`). Consequence to accept explicitly: if a **transient** error in the DM branch re-raises (per the taxonomy above), it propagates up through `_process_tick` and is caught by the existing per-tick `except Exception` suppressor (`scheduler.py:160-165`), which ends the tick early — exactly as a transient channel error does today. The channel reminders for that tick have **already fired** (channel loop ran first), so they are unaffected; any *not-yet-processed* DM rows are simply retried on the next tick (≤60 s later, same calendar day). **This inherited, non-isolated behavior is accepted, not a defect** — it matches how the channel branch already behaves. Isolating the branches would require separate sessions and separate try/except scopes; **simplicity-first recommendation: do NOT isolate.** One session, one tick, channel-then-DM ordering, shared retry semantics.

**Store module (finding 10).** The DM idempotency store is `MemberNotificationSentStore`, added to the **existing** `src/mom_bot/reminders/sent_store.py` (NOT a new sibling module) — it mirrors `ReminderSentStore` exactly (`mark_sent` / `unmark` / `was_sent`, each committing immediately) and belongs beside it. `sent_store.py` is therefore in Phase B's `touches:`.

### 2.3a Due-occurrence predicate (anchor + cadence — the new schedule core)

This replaces the old `weekday == today` test. A notification is **due on a given tick** iff ALL THREE hold:

1. **Today is an occurrence date** for its `(anchor_date_utc, cadence)` — the cadence test below.
2. **`now_time >= fire_time_utc`** — the time-of-day gate (note `>=`, the same same-day-late-tolerant semantics the channel reminders use with `<=`; see "skip-to-next" below).
3. **Not already sent** for today's occurrence — `id NOT IN (SELECT … FROM member_notification_sent WHERE occurrence_date_utc == today)`.

The occurrence-date test is a **pure function** `is_occurrence_date(anchor_date, cadence, today) -> bool`, fully unit-testable with no DB or Discord dependency (it belongs in `member_notifications/service.py` or a small `schedule.py` helper beside it — implementer's choice; keep it pure). Per cadence:

- **weekly:** `delta = (today - anchor_date).days; delta >= 0 and delta % 7 == 0`.
- **biweekly:** `delta = (today - anchor_date).days; delta >= 0 and delta % 14 == 0`.
- **monthly:** `today.year, today.month` is at or after `anchor`'s month/year, AND `today.day == clamped_anchor_day(anchor_date.day, today.year, today.month)` — see the monthly clamp below.

The `delta >= 0` guard on every cadence is what makes the predicate **never fire before the anchor date** — a notification created with a future `anchor_date_utc` simply sits idle until its first occurrence.

#### Monthly clamp (user-confirmed — the calendar-edge risk surface)

If the anchor's day-of-month does not exist in a shorter target month, the occurrence is the **LAST day of that month** — never skipped:

```
clamped_anchor_day(anchor_day, year, month):
    last = monthrange(year, month)[1]   # calendar.monthrange → days in month
    return min(anchor_day, last)
```

Consequences to spec precisely (these are the cases tests MUST cover):

- Anchor day **31** → fires Jan 31, **Feb 28** (or **Feb 29** in a leap year), Mar 31, **Apr 30**, May 31, **Jun 30**, …
- Anchor day **30** → fires every month on the 30th except **Feb** (→ 28/29).
- Anchor day **29** → fires on the 29th every month except **non-leap Feb** (→ 28); in a leap year, Feb **29**.
- Anchor day **≤ 28** → exists in every month; no clamp ever applies.

**The clamp never skips a month and never double-fires:** exactly one occurrence date exists per month (the clamped day), so the per-occurrence idempotency key `(notification_id, occurrence_date_utc)` is naturally unique per month. **Subtle correctness note for review:** for a day-31 anchor, consecutive months can have *adjacent* clamped occurrence dates (e.g. a longer outage spanning Feb 28 → Mar 31 is fine, but there is no scenario where two occurrences land within the same week) — the clamp is computed independently per `(year, month)`, so there is no carry/drift between months. This independence is the property that makes "roughly monthly" stable rather than accumulating error.

#### Skip-to-next, NOT catch-up (user-confirmed)

A missed occurrence does **not** fire late beyond its own calendar day. This falls out of the predicate for free — no extra code, no backlog queue:

- **Delayed tick, same day:** if the scheduler is briefly down and recovers later on the occurrence date, the predicate still matches (today is still the occurrence date, and `now_time >= fire_time_utc` is still true), so the occurrence fires **later that day**. This preserves today's existing same-day tolerance (the channel branch's `<=` semantics, here `>=` on `now_time` vs `fire_time_utc`) at **occurrence-date granularity**.
- **Whole occurrence day missed (longer outage):** once the calendar rolls past the occurrence date, `is_occurrence_date` returns False for the skipped date and only matches the **next** cadence occurrence. The skipped occurrence is silently dropped — no late fire, no catch-up loop, no backlog. This is the deliberate, user-confirmed behavior: a per-member ping that missed its day is stale, so skipping to the next cadence step is correct.

State for reviewers: skip-to-next is the **natural consequence of the "today is an occurrence date" predicate**, not additional logic. There is no `while` loop walking missed occurrences and no "last_fired" cursor to advance — the only state is the per-occurrence `member_notification_sent` row, exactly as the channel reminders work.

### 2.4 In-process service layer (`member_notifications/service.py`)

The slash commands and the scheduler both call this layer directly — **no HTTP, no loopback.** It owns all DB access for the feature:

- `create(name, target_discord_id, anchor_date_utc, fire_time_utc, cadence, message_template, enabled=True) -> MemberNotification` — raises a typed `DuplicateNotificationError` on a `name` collision (the command translates that to an ephemeral message). Validates `cadence ∈ {weekly, biweekly, monthly}`.
- `list_all() -> list[MemberNotification]` — officers' management view.
- `get(name) -> MemberNotification | None`.
- `update(name, **fields) -> MemberNotification` — partial update incl. the `enabled` toggle AND `anchor_date_utc` / `fire_time_utc` / `cadence` edits; raises `NotificationNotFoundError` if absent.
- `delete(name) -> None` — CASCADE removes sent-log rows; raises `NotificationNotFoundError` if absent.
- `list_due(today, now_time) -> list[MemberNotification]` — the scheduler's read. Filters `enabled = true AND id NOT IN sent_today` in SQL, then applies the **pure occurrence-date predicate (§ 2.3a)** in Python (`is_occurrence_date(row.anchor_date_utc, row.cadence, today)`) plus the `now_time >= fire_time_utc` gate. The occurrence math is not portable SQL (monthly clamp needs `monthrange`), so it runs in Python over the small enabled set — same rationale as Phase A's calendar filter (#268 § 2.3). This is the clean interface that keeps the scheduler out of the table internals (§ 2.2).

The service takes a `session_factory` (same pattern as the scheduler, `scheduler.py:187`). `name` is the lookup key (matches the `Reminder.name`-as-key convention, `reminders/models.py:64-65`). `target_discord_id` is stored opaque TEXT, validated all-digits, never cast except at the `get_member(int(...))` boundary (the finding-11 round-trip argument still holds: snowflakes have no leading zeros, so `str(int(s)) == s`).

> **Simplicity-first — what is NOT in scope:** no notification history/audit surface, no delivery-receipt, no per-member timezone (all times UTC like every existing reminder), no recurrence model beyond the three cadences (weekly/biweekly/monthly). Each is speculative for v1.2.
>
> **Scope note — pure cadence timer, NOT event/calendar conditioning (user choice).** The user chose a pure anchor+cadence timer **over** event- or calendar-conditioned scheduling. Per-member DMs are **NOT** tied to #268's tank-week calculation, to any base `Reminder`'s schedule, or to any computed calendar event — both forms of coupling are explicitly out of scope. A consequence to record honestly: the **monthly** cadence approximates "roughly monthly" by clamped day-of-month, but it does **not** track computed drift such as #268's first-Tuesday-of-month tank-week date. If an officer wants a DM aligned to tank week, the cadence timer cannot express that — it would need the event-conditioned model that was deliberately not built. This is an accepted limitation of the simpler design, not a defect.

### 2.5 Discord slash commands (`member_notifications/commands.py`)

Mirrors the `post_conditions/commands.py` pattern exactly: `app_commands` handlers plus a `register(tree, service)` function called once at startup. Commands are guild-scoped via the existing `tree.copy_global_to(guild=...)` + `tree.sync(guild=...)` in `setup_hook` (`main.py:171-175`, verified). The CRUD surface:

| Command | Params | Behavior |
| --- | --- | --- |
| `/member-notify-add` | `member: discord.Member`, `name: str`, `start_date: str` (YYYY-MM-DD, → `anchor_date_utc`), `time: str` (HH:MM, → `fire_time_utc`), `cadence: choice` (weekly\|biweekly\|monthly), `message: str` | Create. Reads `member.id` → `target_discord_id` (§ 2.6). `cadence` is an `app_commands.Choice[str]` (native dropdown — no free-text typo path). Ephemeral confirm; ephemeral error on duplicate `name` or bad date/time. |
| `/member-notify-list` | — | List all (ephemeral embed). Display per row: member, `start_date`, `time`, `cadence`, `enabled`. |
| `/member-notify-get` | `name: str` | Show one (ephemeral) incl. anchor/time/cadence/enabled. Ephemeral error if absent. |
| `/member-notify-update` | `name: str`, optional `member` / `start_date` / `time` / `cadence` (choice) / `message` / `enabled` | Partial update incl. `enabled` toggle AND anchor/time/cadence edits. Ephemeral error if absent. |
| `/member-notify-remove` | `name: str` | Delete (CASCADE). Ephemeral confirm; ephemeral error if absent. |

`cadence` uses `app_commands.Choice[str]` so Discord renders a fixed dropdown — the officer cannot type an invalid cadence, which removes a whole validation-error class at the UI layer (the DB CHECK is still the backstop). `start_date`/`time` are parsed and validated by the handler (ISO date, HH:MM minute-boundary) before the service call; a parse failure → ephemeral validation message, service never called.

Each handler: `await interaction.response.defer(ephemeral=True)` → call the service → `await interaction.followup.send(..., ephemeral=True)`. This is the exact `post_conditions` shape (`commands.py:86-92`, verified).

### 2.6 Member targeting via native `discord.Member` param (eliminates the username race)

`/member-notify-add` takes `member: discord.Member` — the officer picks the member through Discord's native mention/autocomplete UI, and the handler reads `member.id` and stores `str(member.id)` as `target_discord_id`. This **supersedes the dropped HTTP username-resolution path**: the Member picker IS the convenience layer, and it is **rename-safe by construction** — Discord resolves the picker selection to a stable snowflake at command-invocation time, so there is no create-time name lookup, no cache miss, and **no 404 path** for member resolution. The canonical persisted key (`target_discord_id`) is unchanged; only the *acquisition* of that id changed (native picker instead of pasted string or username lookup).

### 2.7 Error UX — ephemeral replies, not HTTP codes

All command errors are **ephemeral interaction replies** with module-level message constants (mirroring `_LINK_YOUR_ACCOUNT_MSG` / `_OPS_ERROR_MSG` at `commands.py:53-58`, verified):

- Duplicate `name` on add → ephemeral "A notification named '<name>' already exists." (service raises `DuplicateNotificationError`).
- Absent `name` on get/update/remove → ephemeral "No notification named '<name>' found." (service raises `NotificationNotFoundError`).
- Invalid `start_date` / `time` on add/update → ephemeral validation message (handler parses + validates ISO date and HH:MM minute-boundary before calling the service). `cadence` cannot be invalid via the UI (native `Choice` dropdown); the DB CHECK is the backstop for any non-UI write path.
- Unexpected error → ephemeral generic ops-error message; full exception logged server-side (never leaked to the user), matching the `post_conditions` `_OPS_ERROR_MSG` + `_logger.exception` pattern (`commands.py:90-92`).

No HTTP status codes anywhere — the HTTP CRUD surface is gone.

### 2.8 Officer-permission gate (RESOLVED — A+B: `default_permissions` + in-handler `manage_guild` check)

These commands target OTHER members (unlike `post_conditions`, which is per-invoking-user with "no admin override", `commands.py:11-13`), so they MUST be officer/admin-gated. **Investigation result (retained for provenance):** a `src/`-wide grep for `default_permissions`, `app_commands.check`, `manage_guild`, `administrator`, `require_admin_role`, `is_owner`, `has_permissions`, `officer` returned **ZERO matches** — there was **no implemented authorization gate anywhere in the bot** (verified). The convention is *documented* only: `docs/discord-permissions-reference.md:93-107` specifies `default_member_permissions = Permissions.manage_guild` for admin commands as **Layer-6 soft enforcement** and names a `@require_admin_role` decorator as "the security boundary" (`:97`) — but that decorator **does not exist in code**.

**RESOLVED DECISION (user): A + B combined.** Each of the five commands gets BOTH:

- **(A) `@app_commands.default_permissions(manage_guild=True)`** — Discord-side soft UX enforcement; hides the commands from members who lack `Manage Server`. Matches the documented convention (`discord-permissions-reference.md:105`).
- **(B) An explicit in-handler check** — `if not interaction.user.guild_permissions.manage_guild: <ephemeral "officers only"> ; return` at the top of every handler, **before** any service call. This is the actual runtime security boundary (A alone is overridable by a server admin via Integrations and is explicitly "not the security boundary", `:97`).

A+B together give soft UX hiding *and* genuine runtime enforcement with no new RBAC infrastructure (~3 lines/command, no config, no KV secret). The heavier configured-officer-role-id option (C) is **rejected** as speculative for v1.2. **`#269` thereby establishes the first real authorization gate in the codebase** — the in-handler `manage_guild` check is the pattern future admin commands (`/reminder add`, etc.) should reuse; it is a small de-facto stand-in for the doc's still-unimplemented `@require_admin_role` boundary. (Extracting it into a shared decorator/helper is a reasonable follow-up but not required for v1.2 — flagged, not blocking.)

---

## 3. Migrations

ONE Alembic migration (slug-named, `b2_member_role_sync_state` convention):

- Create `member_notification` with: `anchor_date_utc` (DATE NOT NULL), `fire_time_utc` (TIME NOT NULL, minute-boundary CHECK — dialect-aware, reuse the `0002` pattern), and `cadence` (TEXT NOT NULL, CHECK `cadence IN ('weekly', 'biweekly', 'monthly')`). **No `IS NULL OR` guard** on the cadence CHECK — `cadence` is NOT NULL so there is no NULL row to admit (contrast Phase A's nullable `month_condition`, which DOES need the guard). The dialect-aware *authoring* convention from migration `0002` still applies to both CHECKs.
- Create `member_notification_sent` with `UNIQUE(member_notification_id, occurrence_date_utc)` + FK CASCADE (the column is today's date at fire time; may be named `fire_date_utc` for literal `ReminderSent` parity — § 2.2).
- `down_revision` = Phase A's schema migration head (#269 depends on Phase A's `delivery_target` column existing). **Build-time check:** confirm via `alembic heads`; Phase B rebases onto Phase A's merged head. Do not hard-code the predecessor.

No data seeding — `member_notification` starts empty; officers populate it via the slash commands (§ 2.5).

---

## 4. Testing (TDD-first)

New `tests/member_notifications/test_commands.py` — command-handler tests mirroring the `post_conditions` style (`tests/post_conditions/test_commands.py:65-84`, verified): there is **no `FakeInteraction` class** in the repo; the established pattern is `MagicMock(spec=discord.Interaction)` with `interaction.response.defer` / `interaction.followup.send` as `AsyncMock`s. Reuse that helper shape. Tests run the handler against the in-process service over an in-memory SQLite session (no HTTP, no TestClient):

- **CRUD happy paths:** `/member-notify-add` (with a fake `discord.Member` whose `.id` is read into `target_discord_id`) → `-list` → `-get` → `-update` (incl. `enabled` toggle) → `-remove`. Assert each sends an ephemeral followup.
- **Member targeting (§ 2.6):** add reads `member.id` and stores `str(member.id)`; no username lookup, no 404 path for member resolution (the picker can't yield an unresolvable member).
- **Duplicate name** on add → service raises `DuplicateNotificationError` → handler sends the ephemeral duplicate message, no row created.
- **Absent name** on get/update/remove → `NotificationNotFoundError` → ephemeral not-found message.
- **Validation:** malformed `start_date` (not ISO) / malformed `time` (not HH:MM or non-minute-boundary) → ephemeral validation message, service never called. `cadence` invalid-value is unreachable via the UI `Choice` dropdown; a direct service call with a bad cadence is rejected (service validates, DB CHECK backstops).
- **Officer gate (§ 2.8 — resolved A+B):** a non-`manage_guild` invoker gets the ephemeral "officers only" rejection and the service is never called (the in-handler check, option B); an officer proceeds. Also assert the `@app_commands.default_permissions(manage_guild=True)` decorator is present on each command (option A; not runtime-testable via a mock interaction, so assert on the command object's metadata).
- **Error isolation:** an unexpected service exception → ephemeral generic ops-error message; the exception is logged, never surfaced verbatim (mirror `commands.py:90-92`).
- `target_discord_id` stored opaque (large-snowflake string round-trips unchanged via the service).

**New `tests/member_notifications/test_schedule.py` — pure occurrence-math (no DB, no Discord), the highest-value test surface (§ 2.3a).** `is_occurrence_date(anchor_date, cadence, today)` and `clamped_anchor_day`:

- **weekly:** fires on `anchor`, `anchor+7`, `anchor+14`; does NOT fire on `anchor+1..6`, `anchor+8`, etc.; does NOT fire before `anchor` (`delta < 0`).
- **biweekly:** fires on `anchor`, `anchor+14`, `anchor+28`; does NOT fire on `anchor+7` (the key weekly-vs-biweekly discriminator), `anchor+1`, etc.; not before `anchor`.
- **monthly — clamp (the calendar-edge surface; cover ALL of these):**
  - Anchor day **31**: fires Jan 31; **Feb 28 in a non-leap year, Feb 29 in a leap year**; Apr 30; Jun 30; full 12-month walk for both a leap and a non-leap year.
  - Anchor day **30**: every month on the 30th except Feb (28/29).
  - Anchor day **29**: 29th every month; **non-leap Feb → 28**, **leap Feb → 29** (pin specific leap year e.g. 2028 and non-leap e.g. 2027).
  - Anchor day **≤ 28**: clamp never applies — fires on the anchor day every month.
  - **Never skips a month:** assert exactly one occurrence date exists in each of 12 consecutive months for a day-31 anchor.
  - **Before anchor month:** a monthly notification with a future-month anchor does not fire in earlier months.

**Extend `tests/test_reminders_scheduler.py` (in-memory SQLite, FakeBot/FakeGuild/FakeMember, time_machine):**

- An enabled `member_notification` fires a DM via `member.send()` on an **occurrence date** at/after `fire_time_utc`; resolves the member by `target_discord_id`, not username.
- **Skip-to-next, NOT catch-up (§ 2.3a):**
  - *Delayed same-day tick fires:* freeze time to the occurrence date but a few hours AFTER `fire_time_utc` (simulating a recovered outage), tick once → the occurrence fires that day (the `now_time >= fire_time_utc` + today-is-occurrence predicate still matches).
  - *Past occurrence does NOT fire late:* freeze time to the day AFTER a missed occurrence date (not itself an occurrence date) → no fire, no `member_notification_sent` row written, and the next tick on the following genuine occurrence date fires normally. Assert the skipped occurrence is silently dropped (no backlog).
- `enabled=false` rows never fire.
- **Per-occurrence idempotency:** at most one send per `(member_notification_id, occurrence_date_utc)`; a second tick the same occurrence day (e.g. 1 minute later) does NOT re-send (UNIQUE-collision path).
- `Forbidden` (DMs closed) → permanent drop, row stays, no retry; `NotFound` (left guild) → permanent drop; `5xx`/timeout → row deleted, retried next tick — assert via the existing taxonomy harness.
- **Insert-first ordering** (finding 6): a departed-member occurrence writes the `member_notification_sent` row before resolution, so a `NotFound` does not loop on subsequent same-day ticks.
- Existing channel reminders are unaffected by the new query (regression guard).

If the `_deliver` refactor (§ 2.3) is taken, it is **behavior-preserving** — existing channel-send tests must pass unchanged. Apply the refactoring-discipline skill.

---

## 5. Phasing & dependencies

- **Phase B depends on Phase A** for the `Reminder.delivery_target` column (introduced in Phase A's schema migration — see #268 spec § 6). Phase B does NOT introduce that column.
- Apart from that one shared column, the streams are independent: #269 adds its own package (table + service + slash commands), wires command registration in `main.py`, and adds the scheduler DM branch. It can be reviewed and merged on its own once Phase A's `delivery_target` column has landed on `main`.
- **Merge order:** Phase A (#268) first (it owns the shared column), then Phase B (#269) rebased onto Phase A's Alembic head.
- **The anchor+cadence schedule change is entirely internal to Phase B.** It does NOT affect #268, the shared `Reminder.delivery_target` column, or the A→B merge order — the schedule fields live only on the new `member_notification` table, and the scheduler's DM branch is still gated by `delivery_target` exactly as before. Confirmed: Phase A is unchanged by this revision.

---

## 6. Resolved decisions

1. **Message wording — TODO placeholder (non-blocking, user-confirmed).** Officers set real text via `/member-notify-add` / `-update`. Blocks nothing in code.
2. **Soft-disable — RESOLVED: KEEP.** User confirmed the `enabled` soft-disable column stays, with the scheduler's `WHERE enabled = true` filter (§ 2.3). Officers pause via `/member-notify-update ... enabled:false` without deleting. `/member-notify-remove` remains for permanent removal.
3. **Officer surface — RESOLVED: Discord slash commands ONLY (user decision).** The earlier sidecar HTTP CRUD design is DROPPED entirely (no HTTP endpoints, no Bearer surface, no HTTP status codes, no loopback). Officers manage notifications via five guild-scoped slash commands (§ 2.5) calling an in-process service (§ 2.4). Errors are ephemeral replies (§ 2.7).
4. **Member identity — RESOLVED: native `discord.Member` picker (supersedes the dropped username path).** `target_discord_id` remains the canonical persisted key, but it is now acquired via Discord's native `discord.Member` command parameter — the officer mentions/autocompletes the member and the handler reads `member.id` (§ 2.6). This **eliminates the username-resolution race entirely**: no create-time name lookup, no cache miss, no 404 path. The prior create-by-username HTTP path (and its documented rename race) is **removed** — the picker is rename-safe by construction.
5. **Officer-permission gate — RESOLVED: A+B (user decision).** Both `@app_commands.default_permissions(manage_guild=True)` (soft UX hiding) AND an in-handler `interaction.user.guild_permissions.manage_guild` check (the real runtime boundary, ~3 lines/command) on every command. No configured-role RBAC (option C rejected). #269 establishes the first real authorization gate in the codebase; the in-handler check is the reusable pattern. Full detail in § 2.8.
6. **Schedule model — RESOLVED: anchor + cadence (replaces weekday+time).** Schema is `anchor_date_utc` (DATE) + `fire_time_utc` (TIME) + `cadence` (`weekly`|`biweekly`|`monthly`, NOT NULL, CHECK-constrained). Weekly subsumes the old weekday+time model (weekday derived from the anchor). Due-occurrence predicate (§ 2.3a): today is an occurrence date for `(anchor, cadence)` AND `now_time >= fire_time_utc` AND not-already-sent-this-occurrence.
   - **Monthly = clamp-to-last-day (user-confirmed):** if the anchor day-of-month doesn't exist in a shorter month, fire on that month's LAST day (31st anchor → Feb 28/29, Apr 30). Never skips a month. The calendar-edge surface — demands thorough leap-year / short-month tests (§ 4).
   - **Skip-to-next, NOT catch-up (user-confirmed):** a delayed tick still fires the occurrence later the SAME day; a wholly-missed occurrence day is silently skipped to the next cadence step — no backlog, no catch-up loop. This is the natural consequence of the today-is-an-occurrence-date predicate, not extra code.
   - **Per-occurrence idempotency:** `member_notification_sent` keyed `(notification_id, occurrence_date_utc)` — the existing `ReminderSent` UNIQUE pattern carries over unchanged (the column is today's date at fire time).
   - **Pure cadence timer OVER event/calendar conditioning (user choice):** per-member DMs are NOT tied to #268's tank-week calculation or any base reminder (both explicitly out of scope). The monthly cadence approximates "roughly monthly" but does not track #268's computed first-Tuesday drift — an accepted limitation of the simpler design (§ 2.4 scope note).
7. **Phase-A impact — NONE (confirmed).** The schedule revision is internal to Phase B; #268, the shared `Reminder.delivery_target` column, and the A→B merge order are unaffected (§ 5).

---

## 7. Sources

- Slash-command pattern (`app_commands` handlers + `register(tree, ...)`): `src/mom_bot/post_conditions/commands.py:243-285` (verified).
- Per-user scope / "no admin override" of `post_conditions` (contrast with #269's target-other-member commands): `src/mom_bot/post_conditions/commands.py:11-13` (verified).
- Ephemeral error UX (`_LINK_YOUR_ACCOUNT_MSG`/`_OPS_ERROR_MSG`, defer→followup, log-don't-leak): `src/mom_bot/post_conditions/commands.py:53-58, 86-92` (verified).
- Guild-scoped command registration in `setup_hook` (`copy_global_to` + `sync`): `src/mom_bot/main.py:171-175` (verified).
- Command-test style (`MagicMock(spec=discord.Interaction)`, AsyncMock response/followup; no `FakeInteraction` class exists): `tests/post_conditions/test_commands.py:65-84` (verified).
- **No officer/admin gate in code (zero grep matches for `default_permissions`/`app_commands.check`/`manage_guild`/`administrator`/`require_admin_role`/`officer` across `src/`):** verified this session.
- Documented (but unimplemented) permission convention — `default_member_permissions = manage_guild` for admin commands, `@require_admin_role` named as the security boundary, `default_member_permissions` is soft UX not the boundary: `docs/discord-permissions-reference.md:93-107` (verified; `:97` and `:105` specifically).
- discord_id-opaque-TEXT persistence pattern (mirrored by `member_notification`): `src/mom_bot/sidecar/models.py:64-66` (verified).
- Scheduler channel-only send + Discord error taxonomy (mirrored by the DM branch): `src/mom_bot/reminders/scheduler.py:239-321` (verified).
- Insert-then-send idempotency pattern: `src/mom_bot/reminders/scheduler.py:225-236`; UNIQUE pattern `src/mom_bot/reminders/models.py:132-139` (verified).
- `Reminder.name`-as-lookup-key convention: `src/mom_bot/reminders/models.py:64-65` (verified).
- Migration slug-naming + dialect-aware CHECK: `migrations/versions/b2_member_role_sync_state.py:37-56`, `migrations/versions/0002_reminders_schema.py` (referenced).
- `delivery_target` column ownership (Phase A): `docs/superpowers/specs/2026-06-27-268-hydra-tank-week.md` § 6.
- #269 officer-surface=slash-commands-only, Member-param targeting, officer-gate-required, delivery=DM, standalone-recurring-only, message-placeholder: user via coordinator (authoritative). Issue body itself `unverified:` (GitHub read unavailable in authoring session).
