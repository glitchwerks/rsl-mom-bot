# mom-bot

Discord bot consolidating two existing bots — `siege-web`'s notifications sidecar and the reminder system from `I:\games\raid\siege\clan\` — into a single bot with interactive slash commands.

**Status:** Epic 0.1 scaffolding complete — package layout, tooling, and Dockerfile in place.

## Documentation

- **Framework plan:** [`docs/superpowers/plans/2026-05-08-mom-bot-framework.md`](docs/superpowers/plans/2026-05-08-mom-bot-framework.md) — locked design decisions, phasing, risks, and verification per epic
- **Cross-repo dependency:** Epic 2.5 lands as a v1.2 ticket in [glitchwerks/siege-web](https://github.com/glitchwerks/siege-web)

## Roadmap

The plan defines 5 epics + 1 cross-cut + 1 pre-epic gate:

| Phase | Scope |
| --- | --- |
| **Pre-Epic-0** | Discord application audit + reminder-bot deployment typing (gates Epic 0) |
| **Epic 0** | Skeleton: new repo wiring, Discord client, App Insights, SQLite baseline, `/ping` health-check |
| **Epic 1** | Reminder lift-and-shift (port from `I:\games\raid\siege\clan\`; JSON file → SQLite) |
| **Epic 2** | Sidecar lift-and-shift (port `siege-web/bot/`'s 6 HTTP endpoints into mom_bot's service half) |
| **Epic 2.5** | Siege-web cross-cut (`/me/preferences` endpoints + `X-Acting-Discord-Id` header support — lands in siege-web v1.2) |
| **Epic 3** | Interactive slash commands (~13 commands across `/siege` and `/reminder` groups) |
| **Epic 4** | Cutover (deploy to new Azure RG `mom-bot-prod`, retire siege-bot + old reminder-bot) |

See the framework plan for design decisions, scope locks, risks, and verification per epic.

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

## Database / Migrations

Mom-bot uses [Alembic](https://alembic.sqlalchemy.org/) for schema migrations backed by SQLAlchemy.
The local dev default is SQLite; production and staging inject a different URL via the
`MOM_BOT_DATABASE_URL` environment variable.

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
(e.g. `postgresql+psycopg2://user:pass@host/dbname`).

## Project Structure

```
mom-bot/
├── src/
│   └── mom_bot/                   # Main package (src-layout)
│       ├── __init__.py            # Package version
│       ├── __main__.py            # `python -m mom_bot` entrypoint (placeholder)
│       └── db/
│           └── __init__.py        # SQLAlchemy DeclarativeBase (Base)
├── migrations/                    # Alembic migration scripts
│   ├── env.py                     # Wired to Base.metadata; reads MOM_BOT_DATABASE_URL
│   ├── script.py.mako             # Migration file template
│   └── versions/
│       └── 2f03efc88bf2_baseline.py  # Empty baseline (sequence 0001)
├── tests/
│   ├── test_smoke.py              # Package importability smoke test
│   └── test_alembic.py            # Alembic config and revision wiring test
├── alembic.ini                    # Alembic config (local SQLite default)
├── docs/                          # Design docs, framework plan
├── pyproject.toml                 # PEP 621 metadata, tool configs
├── Dockerfile                     # Container build (python:3.12-slim, non-root)
└── .dockerignore
```

## References

- Framework plan: [`docs/superpowers/plans/2026-05-08-mom-bot-framework.md`](docs/superpowers/plans/2026-05-08-mom-bot-framework.md)
- Tracking issue: [#12 — Epic 0.1 repo scaffolding](https://github.com/glitchwerks/mom-bot/issues/12)

## Versioning

Mom-bot is its own product on its own version track (`mom-bot v0.1` → `v1.0`), separate from siege-web. The runtime is coupled to siege-web by design (shared Discord token, sidecar HTTP contract, shared guild) — the separate-repo / separate-versioning is for code-organization clarity, not real separability.

## License

TBD — to be set before first public release.
