# AAD Provisioning Runbook — mom-bot-gha

Bicep cannot create AAD app registrations (Microsoft Graph operations are not
supported in ARM/Bicep). This runbook covers the one-time manual steps required
before Bicep can deploy the rest of the infrastructure.

Run these commands once, in order, as the subscription owner in tenant
`cmbdevoutlook333.onmicrosoft.com` (`48bca6c3-6d4f-4884-bc1a-648ae2362a32`).

---

## Prerequisites

```bash
# Log in to the correct tenant. Always pass --tenant to avoid cross-tenant confusion.
az login --tenant 48bca6c3-6d4f-4884-bc1a-648ae2362a32

# Set the target subscription.
az account set --subscription 213aa1f8-32d1-4ffe-8f4d-6e60f1cd9dc0

# Confirm you are in the right tenant and subscription.
az account show --query '{tenant:tenantId, sub:id, name:name}' -o table
```

---

## Step 1 — Create the AAD app registration

```bash
APP_ID=$(az ad app create \
  --display-name mom-bot-gha \
  --query appId \
  --output tsv)

echo "APP_ID=$APP_ID"
# Save this — you need it in the next step and for the repo variable.
```

---

## Step 2 — Create the service principal

The app registration needs a service principal so Azure RBAC can target it.

```bash
SP_OBJECT_ID=$(az ad sp create --id "$APP_ID" --query id --output tsv)

echo "SP_OBJECT_ID=$SP_OBJECT_ID"
# Save this — it is the ghaServicePrincipalObjectId parameter in main.bicepparam.
```

---

## Step 3 — Add federated credentials (OIDC trust)

Two federated credentials are required for the A++ dev/prod model:

### 3a — Trust pushes to `main`

```bash
az ad app federated-credential create \
  --id "$APP_ID" \
  --parameters '{
    "name": "mom-bot-main-push",
    "issuer": "https://token.actions.githubusercontent.com",
    "subject": "repo:glitchwerks/mom-bot:ref:refs/heads/main",
    "description": "GHA OIDC trust for pushes to main branch",
    "audiences": ["api://AzureADTokenExchange"]
  }'
```

### 3b — Trust pull requests (for IaC what-if checks on PRs)

```bash
az ad app federated-credential create \
  --id "$APP_ID" \
  --parameters '{
    "name": "mom-bot-pr",
    "issuer": "https://token.actions.githubusercontent.com",
    "subject": "repo:glitchwerks/mom-bot:pull_request",
    "description": "GHA OIDC trust for pull request workflows",
    "audiences": ["api://AzureADTokenExchange"]
  }'
```

> **Note:** Do NOT create `:environment:dev` or `:environment:prod` federations.
> The A++ model has no GitHub Environments distinction — one workflow, prod only.

---

## Step 4 — Update `main.bicepparam`

Open `infra/main.bicepparam` and replace the placeholder:

```
param ghaServicePrincipalObjectId = 'REPLACE_AFTER_AAD_RUNBOOK'
```

with the `SP_OBJECT_ID` value from Step 2.

---

## Step 5 — Deploy Bicep infrastructure

```bash
az deployment sub create \
  --location eastus2 \
  --template-file infra/main.bicep \
  --parameters infra/main.bicepparam \
  --subscription 213aa1f8-32d1-4ffe-8f4d-6e60f1cd9dc0 \
  --tenant 48bca6c3-6d4f-4884-bc1a-648ae2362a32
```

Bicep handles the Key Vault RBAC role assignments:

- `mi-mom-bot` → **Key Vault Secrets User** (runtime read-only)
- `mom-bot-gha` SP → **Key Vault Secrets Officer** (deploy-time read+write)

If the Bicep role assignments fail (e.g. ghaServicePrincipalObjectId was wrong),
assign manually:

```bash
KV_ID=$(az keyvault show -g mom-bot -n kv-mombot-eastus2 --query id -o tsv)

# Key Vault Secrets Officer for the GHA service principal
az role assignment create \
  --role "Key Vault Secrets Officer" \
  --assignee-object-id "$SP_OBJECT_ID" \
  --assignee-principal-type ServicePrincipal \
  --scope "$KV_ID"

# Key Vault Secrets User for the managed identity
MI_PRINCIPAL=$(az identity show -g mom-bot -n mi-mom-bot --query principalId -o tsv)
az role assignment create \
  --role "Key Vault Secrets User" \
  --assignee-object-id "$MI_PRINCIPAL" \
  --assignee-principal-type ServicePrincipal \
  --scope "$KV_ID"
```

---

## Step 6 — Set GitHub repo variables

In the `glitchwerks/mom-bot` repository settings, create these **Variables**
(not Secrets — they are non-sensitive OIDC identifiers):

| Variable name | Value |
|---|---|
| `AZURE_CLIENT_ID` | `$APP_ID` from Step 1 |
| `AZURE_TENANT_ID` | `48bca6c3-6d4f-4884-bc1a-648ae2362a32` |
| `AZURE_SUBSCRIPTION_ID` | `213aa1f8-32d1-4ffe-8f4d-6e60f1cd9dc0` |

---

## Step 7 — Grant yourself Key Vault access for local dev

Your own user account needs `Key Vault Secrets User` on the KV to read
`dev-*` secrets locally via `az login` + `DefaultAzureCredential`.

```bash
MY_OBJECT_ID=$(az ad signed-in-user show --query id -o tsv)

az role assignment create \
  --role "Key Vault Secrets User" \
  --assignee-object-id "$MY_OBJECT_ID" \
  --assignee-principal-type User \
  --scope "$KV_ID"
```

---

## Step 8 — Seed initial secrets

```bash
# Discord bot token (prod)
az keyvault secret set \
  --vault-name kv-mombot-eastus2 \
  --name prod-discord-token \
  --value "<paste prod token>"

# Discord bot token (dev — your test bot or same token for local dev)
az keyvault secret set \
  --vault-name kv-mombot-eastus2 \
  --name dev-discord-token \
  --value "<paste dev token>"

# Database URL (prod — SQLite path on Container Apps volume, or Postgres later)
az keyvault secret set \
  --vault-name kv-mombot-eastus2 \
  --name prod-database-url \
  --value "sqlite:////data/mom_bot.db"

# Database URL (dev — local SQLite)
az keyvault secret set \
  --vault-name kv-mombot-eastus2 \
  --name dev-database-url \
  --value "sqlite:///./mom_bot_dev.db"

# App Insights connection string (placeholder — provisioning is Epic 1+)
# Leave these empty or set placeholder until App Insights is provisioned.
az keyvault secret set \
  --vault-name kv-mombot-eastus2 \
  --name prod-app-insights-conn-string \
  --value "PLACEHOLDER"
az keyvault secret set \
  --vault-name kv-mombot-eastus2 \
  --name dev-app-insights-conn-string \
  --value "PLACEHOLDER"
```

---

## Step 9 — Run the deploy workflow

Trigger the first deploy via GitHub Actions:

```
GitHub repo → Actions → Deploy (mom-bot) → Run workflow → Run workflow
```

The workflow (`deploy.yml`) runs `az containerapp update` to push the container
image to `ca-mom-bot`. First run succeeds if:

1. AAD federated credentials are in place (Steps 1–3)
2. Repo variables are set (Step 6)
3. A container image exists at `ghcr.io/glitchwerks/mom-bot:<sha>`

> Image build+push to GHCR is Epic 1 work. For v0 smoke testing, push a
> placeholder image manually and rerun.

---

## Summary checklist

- [ ] Step 1 — AAD app created; `APP_ID` saved
- [ ] Step 2 — Service principal created; `SP_OBJECT_ID` saved
- [ ] Step 3a — Federated credential for `main` push created
- [ ] Step 3b — Federated credential for pull requests created
- [ ] Step 4 — `main.bicepparam` updated with `SP_OBJECT_ID`
- [ ] Step 5 — Bicep deployed successfully
- [ ] Step 6 — Repo variables set in GitHub
- [ ] Step 7 — Your user account has `Key Vault Secrets User`
- [ ] Step 8 — Initial secrets seeded
- [ ] Step 9 — First `workflow_dispatch` run of `deploy.yml` succeeds
