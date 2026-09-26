# Documentation guide

Mom-bot is a production service. Use the documents in the first section for
current operation and maintenance. Plans and specifications in the historical
section preserve design rationale; their phases, placeholders, and future-tense
language describe the state of the project when they were written.

## Current operational documentation

- [`../README.md`](../README.md) — service overview, architecture, local development, and CI workflow index.
- [`../RELEASING.md`](../RELEASING.md) — release and production deployment procedure.
- [`../infra/aad-runbook.md`](../infra/aad-runbook.md) — Azure provisioning, identity, infrastructure deployment, and recovery.
- [`secrets-inventory.md`](secrets-inventory.md) — canonical secret inventory and consumers.
- [`discord-permissions-reference.md`](discord-permissions-reference.md) — current Discord permissions and intents.
- [`operations/day-role-sync-runbook.md`](operations/day-role-sync-runbook.md) — day-role synchronization operations and troubleshooting.
- [`operations/day-role-sync-smoke.md`](operations/day-role-sync-smoke.md) — live smoke validation for day-role synchronization.
- [`operations/discord-roles-preflight.md`](operations/discord-roles-preflight.md) — Discord role hierarchy and permission setup.

## Historical design records

- [`superpowers/plans/`](superpowers/plans/) — implementation plans captured before or during delivery.
- [`superpowers/specs/`](superpowers/specs/) — feature specifications and resolved design decisions.
- [`spike/`](spike/) — time-bound research findings that informed implementation.

Historical records are intentionally retained for traceability. Validate any
commands, URLs, status claims, or future-tense statements against the current
operational documentation and code before acting on them.
