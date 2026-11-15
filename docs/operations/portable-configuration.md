# Portable configuration

These options prepare mom-bot for any Linux container host. They do not provision
infrastructure or change the existing Azure deployment. Keep Azure Bicep, identity,
and Key Vault configuration for future recovery. The default secret source remains
Azure Key Vault, and runtime PostgreSQL authentication remains managed identity.

## Secret sources

Set `MOM_BOT_ENV=prod` for a production deployment. Select exactly one
`MOM_BOT_SECRET_SOURCE` before starting the process:

| Source | Secret location | Azure credentials required for secrets |
| --- | --- | --- |
| `azure` (default) | Key Vault `<env>-<name>` in `MOM_BOT_KEY_VAULT_NAME` | Existing CLI identity in dev or `AZURE_CLIENT_ID` managed identity in prod |
| `environment` | `MOM_BOT_SECRET_<NAME>`; uppercase with hyphens replaced by underscores | No |
| `files` | `<MOM_BOT_SECRET_DIR>/<env>-<name>` | No |

For example, `load_secret("discord-token")` reads
`MOM_BOT_SECRET_DISCORD_TOKEN` in environment mode or `prod-discord-token` in
file mode with `MOM_BOT_ENV=prod`. Use the current [secret inventory](../secrets-inventory.md)
and actual `load_secret()` consumers to populate all required values; do not assume
this example is the complete inventory. Environment mode uses one deployment's
values rather than separate dev/prod prefixes, so isolate each deployment.

Mount the file directory read-only and restrict the host files to the service
operator. Portable names allow lowercase letters, digits, and separating hyphens.
File symlinks must resolve inside the configured directory. Terminal CR/LF characters
are removed; other whitespace is preserved. Missing, unreadable, or blank portable
values fail without contacting Azure. Successful loads are cached for the life of
the process: restart the affected service after rotating a secret. Do not commit
secret files or environment files, print their contents, or include them in reports.

## Database authentication

Runtime database connections use `MOM_BOT_DATABASE_URL`. Select
`MOM_BOT_DATABASE_AUTH` explicitly for a new deployment, including its migration job:

| Value | PostgreSQL behavior |
| --- | --- |
| `azure_identity` (runtime default) | Requires `AZURE_CLIENT_ID`; injects a fresh Azure token when a connection opens |
| `password` | Requires username and password in the URL; creates no Azure credential or token hook |

Use a `postgresql+psycopg` URL and percent-encode reserved characters in credentials.
Supply the URL through protected runtime configuration and supply the same URL and
authentication selection to Alembic. Keep PostgreSQL on a private network and use
transport encryption when crossing a network boundary. Each application should
have its own database and account. Do not put real URLs in command history or logs.
Secret source selection does not automatically configure the database URL.

With `MOM_BOT_DATABASE_AUTH` set, online migrations use the same engine builder and
authentication path as runtime. With it **unset**, migrations retain their existing
behavior: consume the configured URL directly, including tokens acquired externally
by current Azure jobs. This compatibility path does not change existing Azure
workflows. Explicit `azure_identity` enables managed identity in a new migration
job; leave existing jobs unchanged until their identity access is verified. SQLite
remains supported for local tests. Offline migrations generate SQL without opening
a connection.

Unknown modes fail. Authentication failures never select another mode or secret
source automatically. Password mode can still require Azure credentials for secrets
if the secret source was left at its Azure default; configure both choices deliberately.

## Rotation and recovery

Use the [shared portable credential inventory and rotation contract](https://github.com/glitchwerks/rsl-mom-apps/blob/main/docs/portable-credential-rotation.md)
for the current single-Mom topology. It names the separate sidecar inbound and
reverse protected API bearer pairs, affected consumers, and failure recovery.
The present secret loader caches successful values for the process lifetime;
editing a host environment or secret file does not update a running Mom process.
Keep the two bearer values paired with Siege's protected runtime inputs and
recreate only the affected containers under a reviewed write/notification pause.
The phase-scoped runner must use targeted
`docker compose up --no-deps --no-build --pull never --force-recreate --wait backend mom`;
`docker compose restart`
does not reload changed `env_file` values.
Neither current bearer protocol supports an old/new overlap token. A one-sided
recreation is not a completed rotation. Do not log or hash bearer values in evidence.
If the old bearer was exposed, never restore it during incident recovery; keep
the path isolated and stage another fresh pair after a failed activation.

`MOM_BOT_SECRET_DISCORD_BOT_API_KEY` is the Mom inbound sidecar bearer paired
with Siege `DISCORD_BOT_API_KEY`. `MOM_BOT_SECRET_SIEGE_WEB_BOT_TOKEN` is Mom's
outbound bearer paired with Siege `BOT_SERVICE_TOKEN`. They are independent
trust boundaries and must be rotated as separate authorized operations.
`MOM_BOT_SECRET_DISCORD_TOKEN` is the Discord application token; a Developer
Portal reset revokes the prior token and requires targeted Mom container
recreation to load the changed environment file.
Follow the [Discord token reset and recovery runbook](discord-token-reset.md).
Rehearse against the isolated application first; do not start a second
production-connected bot. Recovery after a portal reset uses a fresh token,
not the revoked old file. The independent inbound and outbound bearer-pair
rotation is owned by the [Siege phase-scoped runner](https://github.com/glitchwerks/rsl-siege-manager/blob/main/scripts/portable-vps-bearer-rotation.py),
whose `inspect`, `stage`, `activate`, and `recover` reports contain only
sanitized pass/fail evidence.

`MOM_BOT_DATABASE_URL` carries the `mom_app` role password for runtime and
migrations. The separate raw `MOM_DB_PASSWORD` in the protected portable
database environment is also used by the local readiness probe. Changing an
existing role requires a database administrator operation; editing either file
or rerunning first-boot SQL does not rotate the live password. Pause Mom and
its migration/probe clients, preserve a verified backup and rollback material,
change only that role, update both protected inputs, and verify a fresh
authenticated connection before resuming. Do not rotate Siege's distinct
`siege_app` role as an accidental side effect. No production operation is
authorized by this documentation change.

Preserve independently restorable database backups and protected secret
recovery material; mounted files alone are not a backup strategy. Use a
phase-scoped sanitized report for each separately approved rehearsal or live
operation, with no secret values or message/member content.

Returning to Azure uses the retained infrastructure and Azure defaults. Before any
cutover, stop the old writer/bot, reconcile and restore current database state, verify
identity and secret access, and start only one production bot. A configuration switch
alone does not synchronize databases. Live pilot, migrations, credential rotation,
and cutover require their own reviewed runbooks and authorization.

Unit tests cover Azure defaults, portable failures, and temporary SQLite migrations.
The PostgreSQL integration suite exercises password migrations and runtime against
its disposable CI database. This preparation does not prove live Azure identity access
or authorize a production migration.
