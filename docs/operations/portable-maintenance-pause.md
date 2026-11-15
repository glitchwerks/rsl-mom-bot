# Portable maintenance pause

The opt-in VPS deployment mounts a root-owned control directory read-only into
Mom. `MOM_BOT_MAINTENANCE_ENFORCED=true` makes missing or invalid `control/state`
fail closed. The host runner in Siege #640 owns pause and resume transitions;
this repository does not authorize a live transition.

While paused, Mom rejects sidecar writes, slash commands, gateway handlers,
reminder sends, and auto-kick work. Its authenticated maintenance status probe
reports the paused state and current in-flight count for the host drain. Health
reports an intentional scheduler pause without claiming a send occurred.
If a container starts while paused, skipped slash-command sync and startup
seeds are retried under admission after resume; transient failures retry.

`control/history.json` records the most recent pause and resume boundaries.
After a resume, auto-kick holds any member whose join predates the active state
file's modification time. A first message may have been missed while Mom was
paused or stopped, so these members must be reviewed before any manual removal.
The tracking row is retained and no removal DM is sent. Missing or malformed
history also holds auto-kicks. Newly joined members after the active boundary
continue through the normal 24-hour rule.

Gateway join notifications can be missed while the container is stopped. The
full deployment rehearsal must establish a reviewed reconciliation path for
these events before this pause is used in production.
