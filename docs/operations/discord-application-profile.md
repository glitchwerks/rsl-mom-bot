# Discord application profile

The user-facing **About Me** text on mom-bot's Discord profile comes from the
application **Description** field in the Discord Developer Portal. It is not
generated from this repository at runtime.

## Source of truth

- Application: `mom_bot`
- Application ID: `1362590154002530494`
- Portal location: Discord Developer Portal → Applications → `mom_bot` →
  General Information → Description
- User-facing location: open mom-bot's profile in Discord → About Me

Use this production description:

> Production guild operations bot for reminders, member notifications,
> onboarding, and siege-web integrations.

## Updating the description

1. Open the `mom_bot` application in the Discord Developer Portal.
2. On **General Information**, replace **Description** with the production
   description above.
3. Select **Save Changes**.
4. In the production Discord guild, open the bot's full profile and confirm
   the **About Me** section shows the saved description and contains no WIP,
   alpha, beta, unfinished, or under-construction wording.

Because this value is external configuration, a repository deployment does not
change it. Update this document in the same pull request whenever the canonical
wording changes, then apply and verify the matching portal change.
