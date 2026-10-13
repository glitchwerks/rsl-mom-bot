# Scheduler progress health

Mom-bot 1.6.0 adds private `GET /healthz/progress` on the existing health server
(port 8080). It reports whether reminder cycles and automatic member-activity
sweeps have completed recently. It does not replace Azure `GET /healthz`, the
legacy heartbeat sentinel, or sidecar `GET /api/health`/`bot_connected`. Those
existing liveness and gateway contracts keep their current behavior.

## Meaning of the signal

Each task begins with no successful progress. A completed database query/check
counts even when no reminders or stale members are due. Reminder query errors,
propagated transient delivery errors and caught per-member DM retry errors mark
the cycle failed; a later successful cycle clears failure. A disconnected gateway
skips work and cannot qualify as successful progress. Task exit/cancellation makes
that scheduler unhealthy immediately. Task restart cannot inherit a previous
successful timestamp.

Automatic member sweeps qualify after the query and member-processing pass
complete. Existing handled best-effort DM, permission, resolution, kick and cleanup
outcomes keep their business semantics; progress does not prove every message or
kick succeeded. Permanent/drop outcomes and idempotency skips are not reclassified
as retries. Existing delivery/audit logs still require review for business-level
failures. No scheduling, due-time, recipient, retry or member-removal policy is
changed by instrumentation.

Successful progress must be less than 180 seconds old. Both loops normally tick
every 60 seconds; the initial allowance covers three nominal intervals and needs
pilot verification under long-running member/image/database work. A tick that
stalls cannot refresh its success age. If synchronous database work blocks the
shared event loop, the HTTP request itself can time out: the independent monitor
must treat that as failure, not rely on a JSON response always being available.

HTTP 200 means both schedulers are running, have recent completion and are not
failed. HTTP 503 covers starting, stopped, failed or stale progress. The response
contains only schema/version, fixed scheduler names, status/healthy booleans and
relative last-success ages (or null), never member IDs, counts, names, raw errors,
absolute timestamps, tokens or workload content. The endpoint is read-only.

## Portable monitoring and Slack

Keep port 8080 private. Do not expose scheduler health or general sidecar routes
through Caddy/public ingress. A reviewed local probe must check both gateway
connection and scheduler progress, then export a sanitized success signal to an
independent monitoring service. Slack is the proposed operator destination.
**The Discord bot must not be the alert transport:** a failed bot or VPS cannot
notify about its own disappearance. The external service must detect missing
signals and deliver outage/recovery notifications to Slack even while this VM
is off. Slack delivery and an independent missed-signal monitor are separate
requirements; posting from the scheduler directly to a webhook is insufficient.

No monitor account, Slack webhook/channel, timer or heartbeat sender is configured
by this change. Keep webhook URLs in protected secret storage; do not put them in
Compose examples, reports, logs or Git. Reusing an existing operator Slack channel
is an option after its ownership and notification routing are confirmed. The
existing Azure alert transport is preserved.

Before live activation, separately approve the isolated target, local probe,
monitor/destination, cadence/grace, operator Slack channel and expected outage /
recovery behavior. Test no-progress, query failure, recovery, stopped bot and
unavailable VM with fake Discord or the isolated test identity. No production
Discord writes, live outage/restart tests, credential changes or VPS purchases are
authorized by merging this preparation. Durable pilot evidence must confirm that
thresholds do not misclassify normal long-running cycles.

## Opt-in portable container probe

`/app/.venv/bin/python -m mom_bot.health.check` exits zero only when localhost
sidecar gateway health and private scheduler progress both qualify. It emits no output, ignores
ambient HTTP proxies, refuses redirects, limits response size and uses two-second
request timeouts. Portable Compose applies a five-second outer healthcheck timeout.
The module is never invoked by the unchanged Azure liveness definitions.

The corresponding portable Compose change uses this probe instead of a
connection-only assertion; its configuration preflight rejects a disabled or
legacy probe. Container health is a local signal, not independent alert delivery.
`restart: unless-stopped` does not restart an unhealthy running process. This work
adds no watchdog or automatic restart policy.

## Release and recovery

The additive operational endpoint and probe require an immutable producer image
containing this change, planned for mom-bot 1.6.0. Record its verified source/CI
commit and registry digest; a version label alone is insufficient. A consumer
requiring the probe must not point at an older portable-auth-only candidate.
Deploy/release is independently gated. Roll back the candidate image and its
matching optional consumer probe together if needed; Azure liveness remains
unchanged. The progress state is intentionally process-local and resets on restart.
No persisted database migration or cleanup is needed.
