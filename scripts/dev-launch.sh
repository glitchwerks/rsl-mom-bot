#!/usr/bin/env bash
# dev-launch.sh — Start mom-bot locally with the development environment.
#
# Usage:
#   bash scripts/dev-launch.sh
#
# Behavior:
#   - Loads Azure tenant and subscription IDs from the repo-root .env.dev.
#   - Reuses the current Azure login when it already targets the dev tenant.
#   - Selects the configured subscription and runs mom-bot in dev mode.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/../.env.dev"

if [ ! -f "$ENV_FILE" ]; then
  printf 'Error: required development environment file not found: %s\n' "$ENV_FILE" >&2
  exit 1
fi

# The environment file path is resolved dynamically from this script's location.
# shellcheck disable=SC1090
source "$ENV_FILE"

: "${AZURE_TENANT_ID:?AZURE_TENANT_ID must be set in .env.dev}"
: "${AZURE_SUBSCRIPTION_ID:?AZURE_SUBSCRIPTION_ID must be set in .env.dev}"

CURRENT_TENANT_ID=""
if ! CURRENT_TENANT_ID="$(az account show --query tenantId -o tsv)"; then
  CURRENT_TENANT_ID=""
fi

if [ "$CURRENT_TENANT_ID" != "$AZURE_TENANT_ID" ]; then
  az login --tenant "$AZURE_TENANT_ID"
fi

az account set --subscription "$AZURE_SUBSCRIPTION_ID"

export MOM_BOT_ENV=dev
exec python -m mom_bot
