# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

<!-- Add a "### 📣 Highlights" sub-section here before cutting the next release.
     See RELEASING.md § "Discord Highlights convention" for what to write there. -->

## [1.4.0] - 2026-07-20

### 📣 Highlights

v1.4.0 is all about making new members feel welcome and making sure nobody falls through the cracks:

- **A proper welcome** — new members joining the server now get an automatic welcome message in `#new-members`, asking for a screenshot of their in-game player profile so officers can get them set up faster.
- **Officer join alerts** — officers can now opt in to a DM ping whenever a new member joins, via `/notify-new-members`, instead of relying on catching it in chat.
- **A safety net for silent joiners** — if a new member doesn't post anything within 24 hours of joining, the bot now sends them a friendly heads-up DM and removes them from the server, keeping the member list accurate without requiring manual officer cleanup.

### Added

- **Welcome message for new members** — new joiners now get an automatic welcome post in `#new-members`; a follow-up pass replaced the earlier `#roles` self-assignment line with an ask for a screenshot of the member's in-game player profile (#303, #307).
- **`/notify-new-members` officer alert command** — officers can subscribe to a DM ping whenever a new member joins, instead of relying on catching it live in chat (#305).
- **Auto-kick for silent new members** — members who post nothing within 24h of joining now receive a best-effort DM heads-up before being removed from the server, keeping membership accurate without manual officer follow-up (#304).

### Infrastructure

- **`claude-pr-review` GitHub Actions workflow removed** — PR review is now handled by CodeRabbit, making the Claude-powered review workflow redundant (#298).

## [1.3.0] - 2026-06-29

### 📣 Highlights

v1.3.0 is a reliability and observability release — no new commands, but several things that make the bot more self-correcting and easier to operate:

- **Startup role-mapping preflight** — the bot now self-verifies its day-role mapping once per boot (reconnect-safe, and a preflight error can never crash the bot).
- **App Insights service name** — the service now identifies itself as `mom-bot` instead of `unknown_service` in Azure Application Insights (takes effect after the next infra deploy).
- **Faster due-notification lookups** — a new database index on `occurrence_date_utc` speeds per-member notification queries.
- **Security** — pip upgraded past CVE-2026-6357; Dependabot now opens automatic PRs to keep GitHub Actions pins current.

### Added

- **Startup role preflight** — `run_preflight()` is now called from `MomBot.on_ready()` (after `seed_day_role_map`), guarded by a `_preflight_done` flag so a Discord reconnect can't re-run it, and defensively wrapped so a preflight error can't crash the bot; emits the `role_preflight_complete` log line once per revision boot (#194, #292).
- **Dev-only partial-response test seam** — `MOM_BOT_FORCE_PARTIAL_FOR_DISCORD_ID` env var in `_handle_assign()` forces a partial role-sync result for smoke Scenario 5; absent or non-matching means zero behavior change (#74, #292).

### Changed

- **Authorization refactor (behavior-preserving)** — extracted the duplicated manage-guild check from the five `/member-notify-*` handlers into a shared `require_manage_guild` decorator in new `src/mom_bot/discord_authz.py`; removed dead `_check_officer` / `_OFFICERS_ONLY_MSG`; renamed `_LINK_YOUR_ACCOUNT_MSG` → `_NOT_REGISTERED_MSG` (#154, #289).

### Infrastructure

- **Index on `member_notification_sent.occurrence_date_utc`** — Alembic migration `b4` + matching ORM index backing the `list_due()` date filter (previously only covered by the composite UNIQUE) (#278, #291).
- **`OTEL_SERVICE_NAME=mom-bot`** added to the container env so App Insights `cloud_RoleName` resolves to `mom-bot` instead of `unknown_service`; needs an infra-deploy apply to take effect (#271, #291).
- **pip-audit hardening** — pip upgraded past CVE-2026-6357 in the pip-audit job; added `.github/dependabot.yml` (github-actions ecosystem, weekly) to auto-update Action SHA pins (#59, #60, #282).

### Documentation

- **Rollback runbook §7.1** — filled the prod siege-web placeholders with the verified names `siege-web-prod` / `siege-web-api-prod` (confirmed via `az containerapp show`) (#208, #293).

## [1.2.0] - 2026-06-28

### 📣 Highlights

v1.2.0 brings two new automation features for officers and the whole clan:

- **Tank Week reminders** — the bot now posts a heads-up notice the Tuesday before Tank Week starts, and replaces the normal Hydra reminder with a "final hours" Tank Week message on the ending Tuesday. No more manual pings.
- **Per-member DM notifications** — officers can now schedule recurring direct-message reminders for any individual member using five new slash commands: `/member-notify-add`, `/member-notify-list`, `/member-notify-get`, `/member-notify-update`, and `/member-notify-remove`. Cadence options are weekly, biweekly, or monthly.

Both features are live on next deploy.

### Added

- **Tank Week channel reminders** — calendar-conditional reminder rows for Hydra clash: a heads-up notice fires the Tuesday before Tank Week begins, and a Tank Week end-of-clash reminder replaces the standard Hydra reminder for that occurrence. "Tank Week" is defined as the Hydra clash whose ending Tuesday is the first Tuesday of the month (#268, #276).
- **Per-member notification slash commands** — five officer-gated Discord slash commands for managing recurring DM notifications to a targeted guild member: `/member-notify-add`, `/member-notify-list`, `/member-notify-get`, `/member-notify-update`, `/member-notify-remove`. Schedule is defined by anchor date + cadence (weekly / biweekly / monthly); monthly cadence clamps to last day of month and skips to next occurrence rather than catching up. Uses Discord's native `Member` picker and a cadence dropdown. All commands require **Manage Server** permission — the codebase's first runtime authorization gate (#269, #277).

### Infrastructure

- **Release CI: Discord announcement moved inline** — the Discord release notification is now posted directly inside `release.yml` as a final `notify` job. `notify-discord-release.yml` is now a `workflow_dispatch`-only manual remediation tool for re-posting a failed announcement; it no longer fires automatically (#275).
- **New DB columns on `reminders`** — `delivery_target` (NOT NULL, default `'channel'`) and `month_condition` (nullable, CHECK-constrained to `tank_week_headsup` / `tank_week_end`) added via migrations 0004/0005 (#268).
- **New DB tables for per-member notifications** — `member_notification` and `member_notification_sent` tables created via migration `b3`; the reminder scheduler gained a DM-delivery branch to route these notifications (#269).

## [1.1.0] - 2026-06-27

### 📣 Highlights

v1.1.0 makes mom-bot observable and hardens its infrastructure for the long run:

- **Production observability** — OpenTelemetry traces and logs now flow to Azure Application Insights, giving operators structured visibility into sidecar and bot activity for the first time.
- **Managed-identity migrations** — Postgres schema migrations now run as a dedicated UAMI Container Apps Job with Entra token auth, replacing the manual migration step and eliminating the need for password-based credentials.
- **Bicep-provisioned secrets and infrastructure** — Log Analytics, App Insights, non-credential Key Vault values, and environment parameterisation are now declared in Bicep and applied through the standard deploy pipeline; no more ad-hoc manual provisioning.
- **Ingress rate-limiting** — `/api/internal/*` endpoints are now rate-limited before bearer auth, protecting the sidecar from unauthenticated flood traffic.
- **Dead stopgap removed** — the SQLite-on-AzureFile interim plumbing (storage account, file share, volume mount) has been fully deleted; Postgres is the unambiguous production database.
- **Documentation correctness sweep** — runbooks, the README, the secrets inventory, and ADR/spec files updated to reflect the current production state.

### Added

- **OpenTelemetry / Azure Monitor observability** — OTel SDK wired with Azure Monitor exporter; traces and logs from both the bot and sidecar now flow to Application Insights (#199, #267).
- **Rate-limiting on `/api/internal/*`** — sidecar enforces a per-IP request rate limit on internal endpoints before bearer auth is checked, protecting against unauthenticated flood traffic (#209, #232).
- **UAMI Container Apps Job for Postgres migrations** — `alembic upgrade head` now runs as a separate Container Apps Job using a User-Assigned Managed Identity and Entra token auth, replacing the manual migration step in the deploy pipeline (#256).

### Changed

- README: audit for staleness — Status, Roadmap, Database/Migrations, Project Structure, References sections refreshed to reflect v1.0 ship + v1.1 in progress (#249, #250).
- Runbook + secrets-inventory: resolve stale TBDs and "Epic 1+" placeholders (#249, #251).

### Fixed

- **Postgres Entra token acquisition** — token for Postgres auth is now obtained via `azure-identity` (`ManagedIdentityCredential`), replacing a fragile `curl`-based approach that failed in the Container App environment (#260).
- **ACA IP-deny vs app-auth response codes** — day-role-sync runbook corrected to document the actual HTTP responses returned by ACA ingress IP-deny rules vs. application-level auth failures (#196, #270).
- **OIDC federated credentials** updated to repo's canonical name `rsl-mom-bot` (was `mom-bot` pre-rename). Both `mom-bot-pr` and `mom-bot-main-push` FICs now match GitHub's current OIDC subject claim; unblocks the `Bicep what-if preview` workflow on PR-triggered runs and the next `workflow_dispatch` of `deploy.yml` (#248, #252).

### Infrastructure

- **Log Analytics + App Insights provisioned via Bicep** — workspace and Application Insights instance declared in Bicep and wired to the Container App Environment (#239).
- **Non-credential Key Vault values provisioned via Bicep** — configuration secrets (non-credential KV entries) are now set through the Bicep deployment rather than applied manually (#121, #237).
- **`MOM_BOT_ENV` / database-url parameterisation** — environment name and derived database URL secret name now flow through Bicep parameters, removing hard-coded values (#230).
- **Resource group parameterised in CI** — `AZURE_RESOURCE_GROUP` environment variable drives the deploy workflow; no more hard-coded RG name (#263).
- **Dead AzureFile plumbing removed** — `storage.bicep` (storage account + file share), the CAE `storageBinding` resource, and the `/data` volume mount deleted. The SQLite-on-AzureFile stopgap (#92) has been fully superseded by Postgres (#240, #265).

### Documentation

- UAMI Container Apps Job migration spec committed to `docs/specs/` (#241, #254).
- Postgres role-ownership cutover plan and runbook Step 5.5 added (#262).
- AAD runbook TBDs resolved; App Insights rows added to secrets inventory (#249, #251).
- FIC rename (`mom-bot` → `rsl-mom-bot`) documented (#248, #252).
- Runbook flip-sequence Step 0 pre-deployment sanity check inserted (#244).
- Preflight checklist corrected for stale role-name and KV-secret claims (#242).
- Stale SQLite-as-production framing removed from framework plan (#243, #245).
- Five completed plan files deleted per lifecycle policy (#246, #247).

### CI

- Discord release announcement posted automatically on GitHub Release publication via `notify-discord-release.yml` (#228).
- `uv lock --check` added to CI to fail fast on `pyproject.toml` ↔ `uv.lock` divergence (#229).

### Tests

- Behavioral coverage added for `acquire_token __main__` and `migrate.sh` (#264).
- `_EXPECTED_TABLES` completed and realistic snowflake regression added to Alembic test suite (#238).
- Flaky concurrent-serialization test in sidecar stabilised (#223, #227).

## [1.0.0] - 2026-05-26

### Added

- **Day-role-sync receiver** — `POST /api/internal/role-sync` sidecar endpoint receives day-role webhooks from siege-web, applies or removes Discord roles via the `mom_bot/roles/` service, and persists idempotency state so exact replays short-circuit the service call. Per-`discord_id` `asyncio.Lock` prevents concurrent stale-write races; corrupted stored JSON self-heals on the next write. Contract: `glitchwerks/rsl-mom-apps` `contracts/sidecar-api.yaml`. (#71)
- **Full northbound sidecar HTTP API** — FastAPI app served on port 8001 at bot startup, with reusable Bearer auth (`secrets.compare_digest`), structured request/response models, and HTTP-level error translation. Endpoints: `GET /api/version`, `GET /api/health` (#184); `GET /api/members`, `GET /api/members/{discord_user_id}` (#185); `POST /api/notify` (#190); `POST /api/post-message` (#191); `POST /api/post-image` (#192). Sidecar wired into `make_client()` / bot startup sequence. (#163, #184, #185, #190, #191, #192)
- **Post-condition slash commands** — `/post-conditions catalog` and `/post-conditions me` proxy the siege-web preferences API, letting Discord members view and set their post-condition priorities without leaving Discord. UX evolved through several iterations: initial Select widget (#129), live-updating embed (#137), Button + Modal + CheckboxGroup flow (#139), button-grid V1 (#147), unified `/post-conditions` + `/post-conditions-get` with set-summary embed (#150). Includes catalog cache, Retry-After backoff, and Discord `defer` for slow responses. (#129, #133, #135, #137, #139, #147, #150)
- **`X-Acting-Discord-Username` header** — outgoing requests to siege-web now include the acting member's Discord username for actor identification on proxied calls. (#156)

### Changed

- **Member-not-registered error message** rewritten to direct users to contact admins rather than exposing an internal state description. (#153)

### Fixed

- **`day_number` resolution on unassign** — sidecar now resolves `day_number` from stored role-sync state on unassign, rather than requiring the caller to supply it. (#205)
- **Sidecar auth: 403 on missing `Authorization` header** — previously returned 401; corrected to 403 per contract. (#188)
- **Sidecar per-boundary validation** — sidecar now returns 422 with structured error bodies on invalid request payloads; previously surfaced as unhandled 500s. (#189)
- **Day-role name seeder** — corrected expected role names to `Siege - Day {n} Attacker`; enriched `DAY_ROLE_NOT_FOUND` log with expected and available role names. (#131, #132)
- **Snowflake columns widened to BIGINT** — reminder table snowflake columns were `INTEGER`, truncating large Discord IDs. (#123)
- **Docker / venv invocation** — bot startup now invokes the venv Python directly, avoiding a `uv` cache-dir permission denial on container restart. (#117, #119)
- **`psycopg` moved to runtime dependencies** — was incorrectly scoped to `[dev]`, causing import failures in production. (#115)
- **`python-multipart` added to lock** — `uv.lock` regenerated to include `python-multipart`, required by FastAPI form parsing. (#201)

### Infrastructure

- **PostgreSQL Flexible Server** — full migration from SQLite: provisioned ACA-integrated Postgres instance (#105), Postgres-portable Alembic dialect branching (#108), AAD-token engine with managed-identity auth + startup migrations removed (#110), Postgres admin-race fixed (#111), `alembic upgrade head` step in deploy pipeline (#113).
- **HTTPS ingress on port 8001** — Container App configured for public HTTPS ingress with IP allowlist; siege-web-api-dev CAE egress allowlisted. (#162, #193)
- **`scale.minReplicas = 1`** — prevents the Container App from scaling to zero and dropping the Discord gateway connection. (#183)
- **RBAC hardening** — `SystemAssigned` identity dropped (#82); `AZURE_CLIENT_ID` wired through Bicep to `ManagedIdentityCredential` (#86); `mom-bot-gha` service principal granted constrained RBAC Admin at RG scope for Key Vault role management (#170); redundant GHA SP role assignments removed and ABAC conditions narrowed (#175).
- **`infra-deploy.yml` workflow** — `workflow_dispatch`-only pipeline for Bicep applies; includes `set +e` wrapper to preserve `az deployment` error output. (#164, #168)
- **Bicep what-if PR preview** — automated what-if diff posted as PR comment on infrastructure changes. (#100)
- **SQLite via AzureFile** (pre-Postgres interim) — volume mount, secret reference, replica lock, and snapshot config. Superseded by the Postgres migration. (#92)

### Observability

- **Startup URL log** — `make_client()` emits `INFO mom_bot.main Configured siege-web base URL: <url>` at cold start, giving operators an instant cross-environment sanity check before any Discord gateway traffic. (#211)
- **`/healthz` liveness probe** — Container App liveness probe switched from `exec`-type (rejected by ARM) to `httpGet` against `/healthz`. (#88)

---

**Pre-1.0 history**: Initial pre-1.0 development — see `git log` and the merged PR history for full provenance.

[Unreleased]: https://github.com/glitchwerks/rsl-mom-bot/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/glitchwerks/rsl-mom-bot/releases/tag/v1.0.0
