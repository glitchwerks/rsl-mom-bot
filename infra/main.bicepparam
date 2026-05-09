// main.bicepparam — parameter bindings for main.bicep (prod / A++ model).
//
// Values bound here match the locked spec from Epic 0.4.
// The ghaServicePrincipalObjectId must be filled in after running the AAD
// runbook (infra/aad-runbook.md) to create the mom-bot-gha app registration.

using './main.bicep'

param location = 'eastus2'
param resourceGroupName = 'mom-bot'
param keyVaultName = 'kv-mombot-eastus2'
param managedIdentityName = 'mi-mom-bot'
param containerAppsEnvironmentName = 'cae-mom-bot-eastus2'
param containerAppName = 'ca-mom-bot'

// TODO: fill in after running infra/aad-runbook.md step 2 (az ad sp create).
// Run: az ad sp show --id <appId from runbook> --query id -o tsv
param ghaServicePrincipalObjectId = 'REPLACE_AFTER_AAD_RUNBOOK'

// Container image — update to a real digest before first deploy.
// Image build+push to GHCR is Epic 1 work; for v0 testing, manually push
// and set this to ghcr.io/glitchwerks/mom-bot:<sha>.
param containerImage = 'ghcr.io/glitchwerks/mom-bot:latest'
