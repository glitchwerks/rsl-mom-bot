# mom-bot

Discord bot consolidating two existing bots — `siege-web`'s notifications sidecar and the reminder system from `I:\games\raid\siege\clan\` — into a single bot with interactive slash commands.

## What it does

- **Reminders** — scheduled channel posts for Hydra and Chimera clashes, with a Hydra Tank Week variant that swaps in a heads-up and an end-of-clash message.
- **Per-member DM notifications** — officers schedule recurring reminders to individual members via `/member-notify-add`, `-list`, `-get`, `-update`, `-remove` (weekly / biweekly / monthly cadence).
- **Day-role sync** — receives siege-web webhooks and applies/removes Discord day roles.
- **Post-conditions** — `/post-conditions`, `/post-conditions-get`, `/post-conditions-set` proxy siege-web's preferences API so members can view and set post-condition priorities from Discord.
- **New-member onboarding** — new joiners get an automatic welcome message asking for a profile screenshot; officers can subscribe to join alerts via `/notify-new-members`; members who post nothing within 24h receive a heads-up DM and are removed from the server.
- **`/ping`** — health check (version + uptime).
- **Sidecar HTTP API** — FastAPI service on port 8001 backing the siege-web integrations above.

See `CHANGELOG.md` for the full, dated history of every feature and fix, and the framework plan below for the original design rationale.

## Documentation

- **Framework plan:** [`docs/superpowers/plans/2026-05-08-mom-bot-framework.md`](docs/superpowers/plans/2026-05-08-mom-bot-framework.md) — locked design decisions, phasing, risks, and verification for the original v1.0 build-out (epics 0-4 + the PostgreSQL migration)
- **Release history:** [`CHANGELOG.md`](CHANGELOG.md) (Keep a Changelog format) and the [GitHub Releases page](https://github.com/glitchwerks/rsl-mom-bot/releases)
- **Release process:** [`RELEASING.md`](RELEASING.md) — tag/version/deploy procedure

## Prerequisites

- **Python 3.12** — `python --version` must show `3.12.x`
- **[uv](https://github.com/astral-sh/uv)** — fast Python package manager (`pip install uv` or see uv docs)
- **Docker** — for container smoke tests (`docker build .`)

## Local Development

```bash
# 1. Create a virtual environment
uv venv .venv

# 2. Install the package and dev dependencies
uv pip install -e ".[dev]"

# 3. Run the test suite
.venv/Scripts/python.exe -m pytest          # Windows
# .venv/bin/python -m pytest               # Linux / macOS

# 4. Lint and format checks
.venv/Scripts/python.exe -m ruff check src/ tests/
.venv/Scripts/python.exe -m black --check src/ tests/

# 5. Type checking
.venv/Scripts/python.exe -m mypy src/

# 6. Container smoke build
docker build .
```

## Local Azure Access

Mom-bot reads secrets from Azure Key Vault (`kv-mombot-eastus2`) at runtime via
`DefaultAzureCredential`. On a developer laptop this resolves to your `az login`
session — no managed identity or service principal needed locally.

**Prerequisites:**

```bash
# 1. Log in to the mom-bot tenant (always pass --tenant to avoid cross-tenant confusion)
az login --tenant 48bca6c3-6d4f-4884-bc1a-648ae2362a32

# 2. Set the target subscription
az account set --subscription 213aa1f8-32d1-4ffe-8f4d-6e60f1cd9dc0

# 3. Verify
az account show --query '{tenant:tenantId, sub:id}' -o table
```

**Role requirement:** your user account needs `Key Vault Secrets User` on
`kv-mombot-eastus2`. Request this from the repo admin (@cbeaulieu-gt), or grant
it yourself if you have Owner/User Access Administrator on the subscription:

```bash
MY_OID=$(az ad signed-in-user show --query id -o tsv)
KV_ID=$(az keyvault show -g mom-bot -n kv-mombot-eastus2 --query id -o tsv)
az role assignment create \
  --role "Key Vault Secrets User" \
  --assignee-object-id "$MY_OID" \
  --assignee-principal-type User \
  --scope "$KV_ID"
```

**Running locally with Key Vault secrets:**

```bash
# MOM_BOT_ENV=dev causes config.load_secret() to read dev-* secrets from KV.
MOM_BOT_ENV=dev .venv/Scripts/python.exe -m mom_bot          # Windows
# MOM_BOT_ENV=dev .venv/bin/python -m mom_bot                # Linux / macOS
```

`DefaultAzureCredential` picks up your `az login` session automatically — no
additional environment variables required. See `docs/secrets-inventory.md` for
the full list of secrets and their purposes.

### Running the bot locally

After `Local Azure Access` is set up and `dev-discord-token` + `dev-guild-id`
are seeded in `kv-mombot-eastus2`:

```powershell
$env:MOM_BOT_ENV = "dev"
.\.venv\Scripts\python.exe -m mom_bot
```

The bot connects, logs connection details, and registers `/ping` to the dev
guild. Test it from the dev guild's chat — the response is ephemeral (only
visible to you). Seed `dev-guild-id` via:

```bash
az keyvault secret set \
  --vault-name kv-mombot-eastus2 \
  --name dev-guild-id \
  --value "<your-discord-server-id>"
```

Enable Discord Developer Mode (User Settings → Advanced → Developer Mode) to
right-click the server icon and copy the guild ID.

## Database / Migrations

Mom-bot uses [Alembic](https://alembic.sqlalchemy.org/) for schema migrations backed by SQLAlchemy.
The local dev default is SQLite (developer convenience — no Azure credentials needed for schema work); production uses a PostgreSQL Flexible Server (`pg-mombot-*` in resource group `mom-bot`). The active database is selected via the `MOM_BOT_DATABASE_URL` environment variable (see `docs/secrets-inventory.md` for the canonical secret names).

**Apply all pending migrations:**

```bash
alembic upgrade head
```

**Generate a new migration after adding or changing models:**

```bash
# 1. Generate the migration file (review it before applying)
alembic revision --autogenerate -m "describe change"

# 2. Review migrations/versions/<rev>_describe_change.py — remove any spurious ops

# 3. Apply the migration
alembic upgrade head
```

Set `MOM_BOT_DATABASE_URL` to override the default SQLite URL for prod/staging
(e.g. `postgresql+psycopg://user:pass@host/dbname` — the project uses psycopg v3; `psycopg2` is not installed).

## Project Structure

```
mom-bot/
├── src/
│   └── mom_bot/                        # Main package (src-layout)
│       ├── __init__.py                 # Package version
│       ├── __main__.py                 # `python -m mom_bot` entrypoint
│       ├── main.py                     # Discord client, intents, slash commands
│       ├── config.py                   # MOM_BOT_ENV-aware config + KV secret load
│       ├── discord_authz.py            # Shared `require_manage_guild` authorization decorator
│       ├── telemetry.py                # OpenTelemetry / Azure Monitor wiring
│       ├── db/                         # SQLAlchemy DeclarativeBase
│       ├── health/                     # /health/* liveness/readiness probes
│       ├── migrations/                 # UAMI Container Apps Job entrypoint (acquire_token.py)
│       ├── post_conditions/            # `/post-conditions*` — siege-web preferences proxy
│       ├── reminders/                  # Channel reminders (Hydra/Chimera + Tank Week calendar logic)
│       ├── roles/                      # Day-role sync (`POST /api/internal/role-sync`)
│       ├── member_notifications/       # `/member-notify-*` per-member DM notification commands
│       ├── new_member_alerts/          # `/notify-new-members` officer join-alert subscriptions
│       ├── member_activity/            # 24h silent-joiner tracking + auto-kick
│       └── sidecar/                    # HTTP sidecar (FastAPI, port 8001)
├── migrations/                         # Alembic migration scripts (env.py, script.py.mako, versions/)
├── tests/                              # Pytest suite (unit + integration): per-package subdirectories plus top-level test modules
├── alembic.ini                         # Alembic config (local SQLite default)
├── docs/                               # Design docs, secrets inventory, framework plan
├── infra/                              # Bicep templates + AAD runbook
├── pyproject.toml                      # PEP 621 metadata, tool configs
├── Dockerfile                          # Container build (python:3.12-slim, non-root)
└── .dockerignore
```

## CI Workflows

All workflows live in `.github/workflows/`:

| Workflow | Trigger | Purpose |
| --- | --- | --- |
| `ci.yml` | PR, push to `main` | Lint (ruff + `uv lock --check`), format check (black), type check (mypy), pytest, Docker build smoke test, shellcheck, pip-audit (non-blocking) |
| `build-image.yml` | `workflow_run` after `ci.yml` succeeds on `main` | Builds and pushes the `:<sha>` GHCR image — structurally guaranteed to run only after CI is green for that exact SHA |
| `deploy.yml` | Manual (`workflow_dispatch`) | Deploys a commit's image to the prod Container App: verifies the GHCR image exists, runs Alembic migrations via a Container Apps Job, then updates `ca-mom-bot` |
| `infra-deploy.yml` | Manual (`workflow_dispatch`) | Applies Bicep templates to the prod subscription (mutates live Azure infra); records the deployed commit as a GitHub Deployment on the `prod-infra` environment (#321) |
| `infra-what-if.yml` | PR touching `infra/**` | Posts an `az deployment sub create --what-if` diff as a PR comment; informational only, not a merge gate |
| `release.yml` | Push of a `v*` tag | Publishes a GitHub Release (notes from `CHANGELOG.md`) and an immutable `:vX.Y.Z` GHCR image; posts the Discord release announcement |
| `notify-discord-release.yml` | Manual (`workflow_dispatch`) | Re-posts the Discord release announcement for a given tag if the automatic post in `release.yml` failed |
| `claude.yml` | Issue/PR comment created, PR review submitted, or issue opened/assigned | Delegates to the shared `glitchwerks/github-actions` `claude-tag-respond` reusable workflow (authorized users only) |
| `claude-ci-fix.yml` | `workflow_run` after `ci.yml` completes | Delegates to the shared `glitchwerks/github-actions` `ci-failure` reusable workflow to attempt an automated fix when CI fails |

`prod-infra` is a GitHub Deployments environment used only as a queryable ledger of what `infra-deploy.yml` last applied — it does not gate anything today. See `infra/aad-runbook.md` for first-time provisioning and `RELEASING.md` for the tag → release → deploy sequence.

## Versioning

Mom-bot is its own product on its own version track, following semver from `v1.0.0` onward (see `RELEASING.md` § Versioning policy), separate from siege-web. The runtime is coupled to siege-web by design (shared Discord token, sidecar HTTP contract, shared guild) — the separate-repo / separate-versioning is for code-organization clarity, not real separability.

## License

TBD — to be set before first public release.
