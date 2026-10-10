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

For a portable deployment, rotate the database credential and secret values through
the approved deployment procedure, then restart only affected services with matching
configuration. Verify database connectivity and service health without logging secret
values. Preserve independently restorable database backups and protected secret
recovery material; mounted files alone are not a backup strategy.

Returning to Azure uses the retained infrastructure and Azure defaults. Before any
cutover, stop the old writer/bot, reconcile and restore current database state, verify
identity and secret access, and start only one production bot. A configuration switch
alone does not synchronize databases. Live pilot, migrations, credential rotation,
and cutover require their own reviewed runbooks and authorization.

Unit tests cover Azure defaults, portable failures, and temporary SQLite migrations.
The PostgreSQL integration suite exercises password migrations and runtime against
its disposable CI database. This preparation does not prove live Azure identity access
or authorize a production migration.
