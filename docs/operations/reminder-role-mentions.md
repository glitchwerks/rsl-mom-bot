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

For the current production finding under #386, first verify the six stored
reminders still point to the intended Member role and destination channel(s)
without editing them. Review the intended notification scope with a guild
administrator. The narrow channel-specific option is to grant the bot role
`Mention Everyone` only in the reminders channel; the alternative is making
Member mentionable to everyone guild-wide. Record the current permissions and
the exact change before applying either option. After a separately authorized
change, recheck effective permissions and use a naturally scheduled reminder
or a separately approved controlled test recipient to confirm notification.
Do not send an unsolicited mass mention. Preserve existing schedules, channels
and delivery history. Roll back only the exact permission change if needed.
