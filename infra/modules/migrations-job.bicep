// migrations-job.bicep — Container Apps Job for alembic upgrade head.
//
// This job replaces the exec-based migration step in deploy.yml (issue #255).
// It runs 'alembic upgrade head' inside the bot's image using mi-mom-bot
// (UAMI) for Postgres Entra authentication — the same identity the runtime
// bot uses.  No second service principal is required (spec #241 CONDITIONAL YES).
//
// Design choices:
// - triggerType: 'Manual' — invoked by GHA via 'az containerapp job start'.
//   The job is not scheduled and does not run on events.
// - replicaTimeout: 1800 — 30-minute cap per OQ-1 binding decision.
//   unverified: no documented hard ceiling; Learn quickstart uses 1800s.
// - replicaRetryLimit: 0 — fail fast; do not retry a failed migration.
// - Identity: UAMI (mi-mom-bot) only, same as containerapp.bicep.
//   No registries[] block — the image is on public GHCR (ghcr.io/glitchwerks/
//   mom-bot) which does not require authentication for pull.  The existing
//   containerapp.bicep has no registries[] block for the same reason.
//   OQ-2 resolved: AcrPull on ACR does not apply; no role assignment needed.
// - Command override: ['/bin/sh', '/app/migrate.sh'] replaces the CMD
//   entrypoint so the Discord bot does not start (pitfall from spec §3 Q10).
// - PGHOST / PGDATABASE are plain env vars (not secrets); PGPASSWORD is
//   acquired at runtime by migrate.sh via IMDS token exchange.

@description('Azure region.')
param location string

@description('Resource ID of the Container Apps Environment (cae-mom-bot-eastus2).')
param environmentId string

@description('Container image reference — same image used by ca-mom-bot (ghcr.io/glitchwerks/mom-bot:<sha>).')
param containerImage string

@description('Resource ID of the user-assigned managed identity mi-mom-bot.')
param managedIdentityId string

@description('Client ID of the user-assigned managed identity — supplied as AZURE_CLIENT_ID so migrate.sh ManagedIdentityCredential / IMDS token call selects it unambiguously.')
param managedIdentityClientId string

@description('Fully qualified domain name of the Postgres Flexible Server.')
param postgresHost string

@description('Name of the database to migrate (default: mom_bot).')
param postgresDatabase string = 'mom_bot'

// ---------------------------------------------------------------------------
// Migrations job
// ---------------------------------------------------------------------------

resource migrationsJob 'Microsoft.App/jobs@2024-03-01' = {
  name: 'job-mom-bot-migrate'
  location: location
  // Identity: UserAssigned only — same convention as containerapp.bicep.
  // Empty-object value required by ARM schema (spec §3 Q2 citation).
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${managedIdentityId}': {}
    }
  }
  properties: {
    environmentId: environmentId
    configuration: {
      triggerType: 'Manual'
      // replicaTimeout: 1800 s (30 min) — OQ-1 binding decision.
      // unverified: Learn quickstart uses 1800s; no documented hard ceiling.
      replicaTimeout: 1800
      // replicaRetryLimit: 0 — do not retry failed migrations.
      // A failed migration must be investigated before retrying; silent retries
      // could partially re-apply and leave the schema in an inconsistent state.
      replicaRetryLimit: 0
      manualTriggerConfig: {
        parallelism: 1
        replicaCompletionCount: 1
      }
      // No registries[] block — image is on public GHCR (no auth required).
      // OQ-2 resolved: this project uses ghcr.io/glitchwerks/mom-bot (public)
      // not a private ACR; containerapp.bicep has no registries[] for the same
      // reason.  No AcrPull role assignment is needed or added.
    }
    template: {
      containers: [
        {
          name: 'migrate'
          image: containerImage
          // command: fully replaces the image ENTRYPOINT (Dockerfile CMD) so
          // the Discord bot never starts.  migrate.sh acquires an Entra token,
          // sets PGPASSWORD, and runs 'alembic upgrade head'.  See spec §3 Q10.
          command: ['/bin/sh', '/app/migrate.sh']
          env: [
            {
              // Required by migrate.sh and ManagedIdentityCredential to
              // unambiguously select the UserAssigned MI.  See containerapp.bicep
              // comment on AZURE_CLIENT_ID for the IMDS default-selection caveat.
              name: 'AZURE_CLIENT_ID'
              value: managedIdentityClientId
            }
            {
              // Postgres Flexible Server FQDN — consumed by migrate.sh to
              // build MOM_BOT_DATABASE_URL before calling alembic.
              name: 'PGHOST'
              value: postgresHost
            }
            {
              // Target database name — passed to migrate.sh.
              name: 'PGDATABASE'
              value: postgresDatabase
            }
          ]
          resources: {
            cpu: json('0.25')
            memory: '0.5Gi'
          }
        }
      ]
    }
  }
}

@description('Name of the Container Apps Job (used in GHA az containerapp job start --name).')
output jobName string = migrationsJob.name
