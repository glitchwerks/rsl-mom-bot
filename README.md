# mom-bot

Production Discord service for a Raid: Shadow Legends guild. It provides
scheduled reminders, member workflows, siege-web integrations, and operational
automation from Azure Container Apps.

## Features

| Area | Current behavior |
| --- | --- |
| Scheduled reminders | Posts Hydra, Chimera, Siege, and Hydra Tank Week notices from persisted schedules. |
| Member notifications | Officers manage recurring weekly, biweekly, or monthly member DMs. |
| Day-role synchronization | Receives authenticated siege-web events and applies or removes Discord siege-day roles. |
| Post-condition preferences | Members view and update siege-web post-condition priorities from Discord. |
| New-member onboarding | Welcomes new members, alerts subscribed officers, tracks first-message activity, and follows up on silent joins. |
| Health and integrations | Exposes health endpoints and an authenticated FastAPI sidecar used by siege-web. |

## Architecture at a glance

- A `discord.py` client and FastAPI sidecar run together in Azure Container Apps.
- Azure Database for PostgreSQL stores application state.
- Azure Key Vault supplies runtime secrets through managed identity.
- Azure Monitor and Application Insights receive application and platform telemetry.
- Bicep defines the production Azure resources; GitHub Actions builds, tests, and deploys immutable application images.

Application and infrastructure deployments are independently gated. See the
[release process](RELEASING.md) and [infrastructure runbook](infra/aad-runbook.md)
for operational procedures.

## Supported commands

| Command | Purpose |
| --- | --- |
| `/ping` | Show the running version and uptime. |
| `/member-notify-add`, `-list`, `-get`, `-update`, `-remove` | Manage recurring member DM notifications. |
| `/post-conditions`, `-get`, `-set` | View and update siege-web post-condition preferences. |
| `/notify-new-members` | Turn officer DM alerts for new guild members on or off. |

## Developer quick start

Requirements: Python 3.12, [uv](https://github.com/astral-sh/uv), and Docker.

```bash
uv venv
uv pip install -e ".[dev]"

# Run checks
uv run ruff check src/ tests/
uv run black --check src/ tests/
uv run mypy src/
uv run pytest
docker build .
```

Local development uses SQLite by default. To run the connected bot, authenticate
to Azure, ensure the `dev-*` secrets are available in Key Vault, set
`MOM_BOT_ENV=dev`, and run:

```bash
./scripts/dev-launch.sh
```

Use `MOM_BOT_DATABASE_URL` to override the database connection. Apply schema
migrations with `uv run alembic upgrade head`.

See the [secrets inventory](docs/secrets-inventory.md) for required configuration
and the [documentation index](docs/README.md) for detailed development and
operations guidance.

## Documentation

- [Documentation index](docs/README.md)
- [Release process](RELEASING.md)
- [Infrastructure runbook](infra/aad-runbook.md)
- [Secrets inventory](docs/secrets-inventory.md)
- [Discord application profile](docs/operations/discord-application-profile.md)
- [Changelog](CHANGELOG.md)
- [GitHub Releases](https://github.com/glitchwerks/rsl-mom-bot/releases)

Historical framework plans remain available under `docs/superpowers/` for
design rationale. They describe the original implementation sequence and are
not current runbooks or a statement of product maturity.

## Versioning

Mom-bot follows semantic versioning on its own release track. Runtime contracts
with siege-web are versioned and documented separately from application releases.

## License

Mom-bot is open-source software licensed under the [MIT License](LICENSE).
