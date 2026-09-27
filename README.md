# Telegram Public Group Finder — Railway verified

## Railway variables
Set:
- `BOT_TOKEN`
- `API_ID`
- `API_HASH`
- `ADMIN_IDS`

Optional: `TG_STRING_SESSION` if you already have a Telethon StringSession.

## First run on Railway
This build NEVER calls Telethon `client.start()` interactively, so it will not crash with `EOFError: EOF when reading a line`.

1. Deploy the service.
2. Open the bot as the admin account.
3. Send `/login`.
4. Send the Telegram phone number, then the Telegram login code.
5. If 2-step verification is enabled, send the Telegram 2FA password.
6. The Telethon SQLite session is saved under `/app/data/finder.session`.

For persistence across redeploys/restarts, attach a Railway Volume mounted at `/app/data`.

## Important
- The Telegram user session is used only for public Telegram search/read access.
- Private groups are not bypassed.
- Country matching is heuristic; Telegram does not provide an authoritative group-country field.
