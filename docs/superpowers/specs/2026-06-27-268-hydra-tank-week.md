---
title: "Spec — #268 Hydra Tank Week notifications (calendar-conditional channel reminders)"
issue: 268
milestone: v1.2
date: 2026-06-27
status: proposed
touches:
  - src/mom_bot/reminders/models.py
  - src/mom_bot/reminders/scheduler.py
  - src/mom_bot/reminders/seed.py
  - src/mom_bot/reminders/calendar.py
  - migrations/versions/
  - tests/test_reminders_scheduler.py
  - tests/test_reminders_seed.py
  - tests/test_reminders_calendar.py
skills_relevant:
  - python
---

# Spec — #268 Hydra Tank Week notifications

**Issue:** [glitchwerks/rsl-mom-bot#268](https://github.com/glitchwerks/rsl-mom-bot/issues/268)
**Milestone:** v1.2 (Phase A — independent, separately-mergeable stream)
**Delivery channel:** CHANNEL BROADCAST (confirmed by user via coordinator — same channel as today's Hydra/Chimera reminders; the prior "DM" note belonged to #269).

> `unverified:` The issue body of #268 could not be read in the authoring session (the GitHub read tools were not available to this sub-agent). Domain rules below are sourced from the user's brief, which is authoritative since the user owns the issue. If #268's body carries acceptance criteria beyond the brief, reconcile before implementation.

---

## Recommendation

YES (high confidence) — implement as a small calendar-conditional extension to the existing reminder subsystem. No new scheduler, no DM path. The smallest mechanism that satisfies the stated rules is: a nullable `month_boundary_condition` descriptor on the `Reminder` row plus a pure-function calendar predicate consulted inside the existing minute-tick loop. This is Phase A and is fully self-contained in `src/mom_bot/reminders/`.

---

## 1. Context

Hydra clashes run Wednesday → the following Tuesday. **Tank Week** is the first Hydra clash that *ends in a new month* — i.e. the clash whose ending Tuesday is the **first Tuesday of the month** (first Tuesday-dated day of the month, including when the 1st is a Tuesday; tie-break confirmed by user).

Two new notices are wanted, both clan-wide and both fired to the existing reminder channel:

1. **Tank-week heads-up** — a *unique* (once-per-occurrence) heads-up that fires the day **before** the tank-week-start Wednesday — i.e. the Tuesday before that Wednesday. That Tuesday is the **ending Tuesday of the prior clash**, which is the Tuesday **7 days before** the tank-week ending Tuesday (= the prior clash's ending Tuesday; one week earlier, also a Tuesday). Fires at the existing Hydra slot (Tue 07:00 UTC, same channel) — confirmed by user. (Derivation: tank-week start Wed = ending Tue − 6; heads-up = start Wed − 1 = ending Tue − 7.)
2. **End-of-tank Hydra reminder** — a *unique* reminder that **replaces** the standard Hydra reminder for the tank-week occurrence (not in addition to it). Fires in the same Tue 07:00 UTC slot, suppressing the normal Hydra row for that one date.

### Current state (verified)

- The scheduler fire predicate is pure weekday + time with no calendar logic: `weekday == today AND fire_time_utc <= now_time AND id NOT IN sent_today` (`src/mom_bot/reminders/scheduler.py:193-202`, verified).
- The normal Hydra reminder fires **Tuesday (weekday=1) at 07:00 UTC**; Chimera fires Wed 12:00 UTC (`src/mom_bot/reminders/seed.py:296-308`, verified).
- All sends go to one channel via `channel.send()` (`scheduler.py:239-266`, verified). There is no DM path in the reminder subsystem.
- Per-day idempotency is enforced by `ReminderSent UNIQUE(reminder_id, fire_date_utc)` (`src/mom_bot/reminders/models.py:132-139`, verified).
- `message_template` is a static string; no variable substitution (`models.py:93`, verified).

---

## 2. Design

### 2.1 The calendar predicate (new pure module: `reminders/calendar.py`)

A single pure function, fully unit-testable with no DB or Discord dependency:

```python
def tank_week_ending_tuesday(year: int, month: int) -> datetime.date:
    """Return the first Tuesday-dated day of (year, month) — the tank-week
    ending Tuesday for that month."""
```

Derived dates (also pure helpers):

- **Heads-up fire date** for a given month's tank week = `tank_week_ending_tuesday(y, m) - timedelta(days=7)` (the prior clash's ending Tuesday — exactly one week before, so also a Tuesday).
- **End-of-tank fire date** = `tank_week_ending_tuesday(y, m)` itself.

Two boolean predicates the scheduler calls per tick, given `today_date`:

- `is_tank_week_headsup_date(today)` — true iff `today` equals the heads-up date for the month whose tank week it precedes. Note the heads-up for an early-month tank week can fall in the **previous** calendar month (when the first Tuesday is the 1st–7th, minus 7 days crosses the month boundary), so the predicate computes the candidate from `today + timedelta(days=7)`'s month, not `today`'s month. The spec's reference implementation derives the target month from `(today + timedelta(days=7))`.
- `is_end_of_tank_date(today)` — true iff `today == tank_week_ending_tuesday(today.year, today.month)`.

> **Simplicity-first note:** No recurrence-rule engine, no cron descriptor, no general "calendar condition DSL." The two predicates are the entire calendar surface. This is the smallest mechanism that satisfies the rules; a generic descriptor would be speculative scope and is explicitly rejected.

### 2.2 Schema change — `Reminder.month_condition` (shared Phase-A column)

Add ONE nullable column to `Reminder`:

```python
month_condition: Mapped[str | None] = mapped_column(Text, nullable=True)
```

Allowed values (enforced by a `CHECK` constraint, dialect-aware to match the existing `ck_fire_time_no_seconds` pattern at `models.py` / migration 0002):

- `NULL` — ordinary reminder (today's behavior, unchanged). The normal Hydra and Chimera rows keep `NULL`.
- `"tank_week_headsup"` — fires only on the heads-up date.
- `"tank_week_end"` — fires only on the end-of-tank date, AND suppresses any `NULL`-condition reminder sharing the same `(weekday, fire_time_utc, channel_id)` slot for that date (the replace semantics — see § 2.4).

The existing weekday/fire_time columns are still honored: a `month_condition` row is a *narrowing* filter layered on top of the weekday+time predicate, never a replacement. So `tank_week_end` rows are still `weekday=1, fire_time_utc=07:00` — the calendar predicate just further restricts *which* Tuesdays they fire on.

### 2.3 Scheduler predicate change

In `_process_tick` (`scheduler.py:187-205`), after the existing weekday+time+not-sent query returns eligible rows, apply an in-Python filter (the calendar logic is not expressible in portable SQL across SQLite/Postgres, and the eligible set per tick is tiny — 2-4 rows):

1. For each eligible row, branch on `month_condition`:
   - `NULL` → keep, **unless suppressed** (see § 2.4).
   - `"tank_week_headsup"` → keep iff `is_tank_week_headsup_date(today_date)`.
   - `"tank_week_end"` → keep iff `is_end_of_tank_date(today_date)`.
2. Send kept rows exactly as today.

Keeping the filter in Python (not SQL) is deliberate: it keeps the dialect-aware-CHECK surface small and the calendar logic unit-testable in isolation. The performance cost is nil (≤4 rows/tick).

### 2.4 Replace semantics (the "unique end-of-tank replaces standard Hydra")

On the end-of-tank date, BOTH the normal Hydra row (`month_condition=NULL`, Tue 07:00) and the `tank_week_end` row (Tue 07:00) would match the weekday+time predicate. The `tank_week_end` row must fire **instead of** the normal Hydra.

**Mechanism (smallest that works):** during the per-tick filter, if any kept row has `month_condition="tank_week_end"`, drop every `NULL`-condition row that shares the same `(channel_id, weekday, fire_time_utc)` slot for this tick. This is a pure in-memory suppression computed from the already-fetched eligible set — no extra query, no new table.

**Implementation contract — suppression is a PRE-FILTER, not a post-hoc skip.** Today's loop sends-as-it-goes: `_process_tick` iterates the eligible rows and calls `_handle_reminder` (which calls `mark_sent` THEN sends) one row at a time (`scheduler.py:204-205` → `225-236`, verified). A post-hoc filter is therefore WRONG — if the `NULL` Hydra row sorts before the `tank_week_end` row in the eligible list, it would already have fired (and written its `ReminderSent` row) before suppression ran. The required order is a strict four-step sequence with NO side effects until the last step:

1. **Collect** the full eligible set (the existing weekday + time + not-sent query).
2. **Apply calendar filters** (`month_condition` predicates from § 2.3) → the kept set.
3. **Apply suppression** over the kept set (drop `NULL`-slot rows that collide with a surviving `tank_week_end` row on `(channel_id, weekday, fire_time_utc)`) → the survivor set.
4. **THEN iterate** the survivors, calling `_handle_reminder` on each.

Steps 1-3 are pure list transforms; only step 4 calls `mark_sent` or sends. No `mark_sent`/send may occur before the survivor set is final.

**Required test (replace-semantics ordering):** seed BOTH the normal Hydra (`month_condition=NULL`) and the `tank_week_end` rows, run a tick on the end-of-tank Tuesday where both match the weekday+time query, and assert the normal Hydra row's `mark_sent` is **NEVER called** — spy/assert on the store call itself, not merely that no `ReminderSent` row survives afterward. Asserting on absence-of-row would pass even under a buggy send-then-delete implementation; asserting `mark_sent` is never invoked is the only assertion that catches the sends-as-it-goes regression.

Idempotency is unaffected: the normal Hydra's `ReminderSent` row is simply never written for that date (it was suppressed, not sent), so no stale "sent" record blocks a future date. The `tank_week_end` row gets its own `ReminderSent` row keyed on its own `reminder_id`.

> **Edge case to test:** the heads-up date (7 days before) and the end-of-tank date are different Tuesdays (one week apart), so heads-up and end never collide. The only collision is end-of-tank vs normal Hydra on the same Tuesday, handled above.

### 2.5 Seed change

`_maybe_seed_reminders` (`seed.py:174-318`) seeds two new rows alongside Hydra/Chimera, sharing the same resolved `channel_id` and `role_mention_id`:

- `name="Hydra Tank Week Heads-up"`, `weekday=1`, `fire_time_utc=07:00`, `month_condition="tank_week_headsup"`, `message_template="<TODO: officer to supply>"`.
- `name="Hydra Tank Week End"`, `weekday=1`, `fire_time_utc=07:00`, `month_condition="tank_week_end"`, `message_template="<TODO: officer to supply>"`.

> **Message text (RESOLVED — see § 7.1):** the two templates are **module-level string constants** in `seed.py`, alongside the existing `HYDRA_TEMPLATE`/`CHIMERA_TEMPLATE` (`seed.py:156-166`, verified). Initial value is a `<TODO: officer to supply>` placeholder; officers change wording by editing the constant in a PR (consistent with #268 being hardcoded calendar logic). The implementation MUST NOT invent wording — the placeholder ships until an officer supplies real text. Both the seed and the § 3 data migration insert whatever the constant holds.

> **Seed idempotency caveat (RESOLVED — data migration is required):** the seed only runs when `count(*) FROM reminders == 0` (`seed.py:225-231`, verified). On an environment that has **already seeded** Hydra+Chimera, these two new rows will **not** be auto-inserted — the table is non-empty. **User confirmed dev AND prod are already seeded**, so the one-time **§ 3 Data migration is mandatory** (it copies channel/role from the existing Hydra row — no Discord access needed). Two write paths therefore both seed all four rows: (a) first-boot `_maybe_seed_reminders` on a fresh empty DB (this function is extended to insert all four rows, not two); (b) the § 3 data migration for the already-seeded dev/prod DBs. They are mutually exclusive via the empty-table guard vs the `WHERE EXISTS Hydra` guard, so no environment double-inserts (the `INSERT ... WHERE NOT EXISTS` by `name` is idempotent regardless).

---

## 3. Migrations

Two Alembic migrations (slug-named, matching the `b2_member_role_sync_state` convention at `migrations/versions/b2_member_role_sync_state.py`):

1. **Schema:** add `Reminder.month_condition` (nullable Text) + dialect-aware `CHECK` constraint limiting values to the allowed strings. **CHECK NULL trap (required):** the constraint MUST be written
   `month_condition IS NULL OR month_condition IN ('tank_week_headsup', 'tank_week_end')`
   — NOT a bare `month_condition IN (...)`. SQL three-valued logic makes `NULL IN (...)` evaluate to `NULL` (not `TRUE`), so a bare `IN` clause would **reject every `NULL`-condition row** (i.e. the normal Hydra and Chimera rows) at insert/update time. The explicit `IS NULL OR` disjunction is mandatory. Same schema migration also adds `Reminder.delivery_target` (see § 6). `down_revision` = current head — **build-time check:** confirm the head via `alembic heads` before authoring; the reminder schema migrations (`0002`, `0003`) and the role-sync chain (`a26d62` → `b2_member_role_sync_state`) must be reconciled into one linear head. Do not hard-code the predecessor in this spec.
2. **Data (REQUIRED — dev/prod confirmed already seeded):** insert the two tank-week rows into already-seeded databases (idempotent: `INSERT ... WHERE NOT EXISTS` by `name`). This is **not optional** — both environments have already seeded Hydra+Chimera, so the empty-table seed guard (§ 2.5) will never auto-insert these rows. **channel_id / role_mention_id resolution:** the data migration runs inside Alembic with **no Discord gateway / guild API access** available, so it MUST NOT attempt name→snowflake resolution the way `seed.py` does. Instead, it **copies `channel_id` and `role_mention_id` from the existing seeded `Hydra` row** (`SELECT channel_id, role_mention_id FROM reminders WHERE name = 'Hydra'`) — the two tank-week notices share the same channel and role as the normal Hydra by design (§ 2.5), so copying from the Hydra row is correct and requires no Discord access. If no `Hydra` row exists (a fresh/empty DB that will seed via `_maybe_seed_reminders` on next boot), the data migration is a safe no-op — the `WHERE EXISTS (SELECT 1 FROM reminders WHERE name='Hydra')` guard skips it, and first-boot seeding (§ 2.5, updated to seed all four rows) covers that path instead.

---

## 4. Testing (TDD-first)

New `tests/test_reminders_calendar.py` (pure, no DB):

- `tank_week_ending_tuesday` for months where the 1st is a Tuesday (it IS the answer), and where it is not.
- Heads-up date = ending Tuesday − 7 days (one week earlier, also a Tuesday); verify the cross-month case (early-month tank week → heads-up in prior month).
- `is_tank_week_headsup_date` / `is_end_of_tank_date` true/false boundaries across a full year of months.
- **Cross-year boundary (required):** pin a year whose first Tuesday of January falls on Jan 1 and assert the heads-up date computes to **Dec 25 of the prior year**. Concretely: **Jan 1 2019 is a Tuesday** → first Tuesday of Jan 2019 = Jan 1 → heads-up = Jan 1 − 7 = **Dec 25 2018** (also a Tuesday). `date − timedelta(days=7)` handles year rollover natively, but the year-boundary case must have explicit coverage — it is the one arithmetic path most likely to be silently wrong in a hand-rolled month-derivation.

Extend `tests/test_reminders_scheduler.py` (in-memory SQLite, FakeBot/FakeChannel, time_machine — existing harness):

- `tank_week_headsup` row fires only on the heads-up Tuesday, silent on all other Tuesdays.
- `tank_week_end` row fires only on the end-of-tank Tuesday.
- **Replace semantics:** on the end-of-tank Tuesday, the `tank_week_end` row fires and the normal Hydra row does NOT (no `ReminderSent` written for normal Hydra that date); on every other Tuesday the normal Hydra fires and the tank-week rows are silent.
- Idempotency: each new row fires at most once per its date (UNIQUE collision path).

Extend `tests/test_reminders_seed.py`: the two new rows are seeded on an empty table with the expected `month_condition` values and shared channel/role.

---

## 5. Phasing & shared-change ownership

- **Phase A owns the `Reminder.month_condition` column.** It is introduced here.
- **Phase A also owns the `Reminder.delivery_target` column** (see #269 spec § 2.1) — that column is a shared foundational change, but it is introduced in Phase A's schema migration so Phase B (#269) depends on an already-merged column rather than introducing it. See § 6.

This stream is independently mergeable: nothing in #268 depends on #269.

---

## 6. Shared foundational change — `delivery_target` column (owned by Phase A)

#269 needs a `delivery_target` discriminator on `Reminder` (`"channel"` vs `"dm"`). Because both phases touch `Reminder`'s schema, introducing two separate `Reminder` migrations from two branches risks an Alembic head conflict. **Decision: Phase A's schema migration adds BOTH `month_condition` AND `delivery_target` (defaulting to `"channel"`, NOT NULL with server default).** Rationale:

- Avoids a migration-head collision between the two streams.
- `delivery_target="channel"` is the existing behavior, so adding the column in Phase A is behavior-preserving and ships safely even before #269 lands.
- Phase B then only adds its *new table* and the scheduler's DM-delivery branch — no further `Reminder` schema change.

This is the one place the two streams share a change. It is owned, introduced, and tested in Phase A (a single test asserting existing rows default to `delivery_target="channel"`). If the user prefers strict stream independence over head-collision avoidance, the fallback is for Phase B to add `delivery_target` itself and rebase onto Phase A's head — but the bundled approach is simpler and is the recommendation.

**Intentionally inert in Phase A (acknowledged, not an oversight):** `delivery_target` is *added* by Phase A but *read* only by Phase B's scheduler DM branch — Phase A's scheduler never branches on it (every row is `"channel"`, which is the existing behavior). This is a deliberate merge-order choice (introduce-then-consume across two PRs to avoid a migration-head collision), NOT a dead column left in by accident. Phase A's only coverage of the column is the single round-trip test below; the column's *behavior* is exercised in Phase B.

**Server-default test (NIT — round-trip required):** the test asserting `delivery_target` defaults to `"channel"` MUST INSERT a row (omitting `delivery_target`), `commit`, and re-read it via the ORM — server defaults are applied by the database at INSERT, so asserting on a freshly-constructed-but-uncommitted Python object would read `None`/unset, not `"channel"`, and give a false failure. The same round-trip discipline applies to any `created_at`/`updated_at`/server-default assertion.

---

## 7. Resolved decisions

1. **Message wording — RESOLVED: module-level code CONSTANT (officers edit via PR).** The two notice templates are module-level string constants in `seed.py` (alongside the existing `HYDRA_TEMPLATE` / `CHIMERA_TEMPLATE` at `seed.py:156-166`, verified), seeded into `message_template` like the existing reminders. Officers change the wording by editing the constant and opening a PR — consistent with #268 being hardcoded calendar logic (the whole feature is code, not runtime config). The initial constant value is a `<TODO: officer to supply>` placeholder; it is filled in the implementing PR or a fast follow-up PR, NOT via runtime SQL. This supersedes the earlier "SQL UPDATE" note in § 2.5 — both the seed and the § 3 data migration insert whatever the constant currently holds, so a later wording change is a one-line constant edit + a small data migration (or manual `UPDATE`) to refresh already-seeded rows. Non-blocking for the rest of the implementation.
2. **Already-seeded environments — RESOLVED: REQUIRED.** User confirmed dev AND prod are already seeded. The § 3 **Data migration is mandatory**, and it copies `channel_id`/`role_mention_id` from the existing `Hydra` row (no Discord access at migration time) per § 3 item 2.

---

## 8. Sources

- Scheduler fire predicate (weekday+time, no calendar): `src/mom_bot/reminders/scheduler.py:193-202` (verified).
- Normal Hydra schedule (Tue 07:00 UTC), Chimera (Wed 12:00 UTC), shared channel/role: `src/mom_bot/reminders/seed.py:296-308` (verified).
- Channel-only send path: `src/mom_bot/reminders/scheduler.py:239-266` (verified).
- Per-day idempotency UNIQUE: `src/mom_bot/reminders/models.py:132-139` (verified).
- Static `message_template`, no substitution: `src/mom_bot/reminders/models.py:93` (verified).
- Seed empty-table guard: `src/mom_bot/reminders/seed.py:225-231` (verified).
- Migration slug-naming + dialect-aware CHECK convention: `migrations/versions/b2_member_role_sync_state.py:37-56`, `migrations/versions/0002_reminders_schema.py` (referenced).
- #268 delivery=channel, calendar tie-breaks, fire slots, message-placeholder direction: user via coordinator (authoritative — user owns the issue). Issue body itself `unverified:` (GitHub read unavailable in authoring session).
