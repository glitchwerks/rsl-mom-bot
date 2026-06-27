# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

<!-- Add a "### 📣 Highlights" sub-section here before cutting the next release.
     See RELEASING.md § "Discord Highlights convention" for what to write there. -->

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
