# Reminder role notification preflight

The scheduler sends a role mention only when a channel reminder has a stored
`role_mention_id`. It sets `AllowedMentions(roles=True)` for that message.
Discord also requires the role to be mentionable **or** the bot to have effective
`MENTION_EVERYONE` permission in that destination channel before role members
can be notified. `MENTION_EVERYONE` can also notify `@everyone`, `@here`, and
other unmentionable roles, so a grant needs separate review. Role hierarchy
controls assigning/removing roles; it is not a substitute for mention permission.
See [Discord's message rules](https://docs.discord.com/developers/resources/message#allowed-mentions-object)
and [permission rules](https://docs.discord.com/developers/topics/permissions).

At startup the bot reads the configured **channel** reminders that have a stored
role ID and its connected
guild cache, then logs one aggregate preflight line with `configured`, `ready`,
`blocked`, and `unknown` counts. It does not log IDs, read message bodies, send a
test message, edit permissions, update schedules, or change delivery history.
The audit counts distinct channel/role pairs, so several reminders using one
pair count once. `unknown` means the channel, role, or bot member was absent
from the cache; inspect the guild and rerun before treating it as ready.
The audit covers the scheduler's prefixed role mention only. It does not inspect
`message_template` text or reminders with no stored role ID. Review those
templates for `@everyone`, `@here`, or other mentions before granting
`MENTION_EVERYONE`: `AllowedMentions(roles=True)` does not by itself suppress
`@everyone` in template content. The audit does not guarantee delivery:
permissions may change after startup,
Discord may suppress push notifications, and member notification settings vary.

## Current production decision

The operator decided on 2026-11-15 to keep Member role notifications disabled
under #386. Do not grant the bot `Mention Everyone` in reminder channels or make
Member mentionable guild-wide for this feature. The last read-only production
inspection found all six reminders still stored a Member role ID, but effective
permission to notify that role was false. The scheduler may still render role
markup in reminder text; this permission decision prevents the role ping in the
inspected configuration. A later Discord permission change could make those
stored mentions notify members, so recheck the aggregate startup audit and the
effective channel/role permissions after any guild permission change. Preserve
existing reminder schedules, channels and delivery history.

If the guild later chooses to enable role notifications, review the exact
stored targets and message templates first. The channel-specific option is to
grant the bot `Mention Everyone` only in the reminder channel; the alternative
is making Member mentionable guild-wide. Either option needs separate approval,
an effective-permission recheck and a natural or separately approved controlled
notification test. Do not send an unsolicited mass mention. Roll back only the
exact permission change if needed.
