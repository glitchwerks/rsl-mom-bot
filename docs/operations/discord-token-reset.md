# Discord application token reset and recovery

This runbook covers Mom's `MOM_BOT_SECRET_DISCORD_TOKEN` on a portable host.
It is separate from the [paired sidecar/protected API bearer rotation](https://github.com/glitchwerks/rsl-siege-manager/blob/main/scripts/portable-vps-bearer-rotation.py).
The Discord Developer Portal reset revokes the previous token immediately;
there is no old/new overlap or rollback to the revoked value. Portal reset and
live container recreation each need separate authorization. This document
does not authorize either production action.

## Isolated rehearsal

1. Use the dedicated integration Discord application and guild. Confirm its
   application ID is different from production and that no production bot
   process is connected to the rehearsal guild. Record only a nonsecret
   application label and the deployed image digest in the evidence report.
2. Verify a restorable copy of the isolated environment file is protected by
   the operator, and that only the isolated Mom container consumes it. Confirm
   the current bot can connect and perform a read-only health check.
3. Reset the **isolated** application's token in the Developer Portal. Store
   the replacement directly in the protected secret source; do not paste it
   into a terminal command, issue, CI log, or evidence file. A reset cannot be
   undone by restoring the old environment file.
4. Recreate only the isolated Mom container using the reviewed deployment
   procedure. `docker compose restart` does not reload `env_file` values;
   the targeted recreation must read the new value. Verify exactly one
   instance of this integration application is connected and its sidecar
   health is ready. Check a read-only Discord interaction in the isolated
   guild. Keep production untouched throughout the rehearsal.
5. Record a sanitized pass/fail report: phase, image digest, application
   label, single-instance check, connection and read-only check, and recovery
   outcome. Exclude token values or hashes, Discord IDs, member details,
   message content, and environment-file contents.

## Production gate and recovery

Before a separately approved production reset, identify the exact Mom
container and protected environment file, preserve current configuration and
database backups, and verify the last isolated rehearsal evidence. Pause
writes and notifications with the reviewed maintenance runner; obtain a fresh
drain report. Confirm there is only one production-connected bot. Do not run
an overlapping old/new production instance.

After the portal reset, update the protected source and recreate **only** Mom
under the approved deployment procedure. Verify the new container connects,
the sidecar health is ready, the single-instance invariant holds, and the
maintenance status is still paused before resuming through the separately
gated maintenance phase. A failed connection keeps the pause active. Check
the protected source and deployment wiring without printing the token. If the
replacement token may be invalid or exposed, reset again to a fresh value;
never restore the revoked value. Resume only after a successful check and
write a sanitized report as above.

The Azure Key Vault secret source remains available for a future approved
return to Azure. Switching sources does not restore a revoked Discord token
or synchronize runtime state.
