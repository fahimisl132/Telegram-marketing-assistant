# Telegram Group Finder — Railway package

This package is prepared for Railway using a Dockerfile pinned to Python 3.11.

## Railway variables

Add these Variables to the Railway service:

- `BOT_TOKEN` — token from @BotFather
- `API_ID` — Telegram API ID from https://my.telegram.org
- `API_HASH` — Telegram API hash from https://my.telegram.org
- `ADMIN_IDS` — your Telegram numeric user ID (or comma-separated IDs)

Optional:
- `MAX_RESULTS`
- `SEARCH_LIMIT_PER_QUERY`
- `POST_SAMPLE_LIMIT`

## Important: Telegram user session

The global/public Telegram search uses a Telethon user session. On the first run, Telethon needs to log in to a Telegram user account and create `/app/data/finder.session`.

For reliable production use on Railway, attach a Railway Volume and mount it at:

`/app/data`

This preserves the SQLite database and Telegram session across redeploys/restarts.

Do not share the generated `.session` file or API credentials.

## Railway deployment

1. Upload/deploy this package as the project source.
2. Make sure the service uses the included `Dockerfile` (the included `railway.json` explicitly selects it).
3. Add the four required Variables.
4. Deploy.
5. On the first run, complete the Telethon user-account login when prompted by the runtime environment. After `finder.session` is created, keep `/app/data` on a Railway Volume.

## What this fixes

- Python is pinned to 3.11 instead of 3.13.
- `/app/data` is created before SQLite or Telethon opens files.
- The container startup script creates `/app/data` on every start.
- The Telegram bot and Telethon client share the same asyncio loop; `run_polling(close_loop=False)` avoids the common event-loop shutdown error.

The bot only uses publicly discoverable Telegram information and does not bypass private-group permissions.
