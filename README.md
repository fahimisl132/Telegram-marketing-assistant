# Telegram Marketing Assistant — Complete Starter

A permission-aware Telegram marketing assistant for groups/chats where the bot is
legitimately allowed to send messages.

## Features

- Owner-only Telegram control
- SQLite persistence
- Per-chat rules
- `/setrules`, `/rules`, `/addchat`, `/removechat`, `/status`
- `/check` for compliance review
- `/post` for a checked send
- `/schedule` and `/unschedule`
- Background scheduler
- Duplicate protection
- Per-chat cooldown and 24-hour limit
- Global pause/resume
- Audit log
- Conservative local compliance checks
- Optional OpenAI-compatible AI checker
- Dockerfile
- GitHub Actions deployment workflow template
- Health endpoint for simple hosting

## Safety model

The bot does not join arbitrary groups, impersonate a user, bypass Telegram
permissions, or bypass anti-spam systems. A chat must be explicitly configured
and Telegram must permit the bot to send there.

AI output is never treated as authoritative by itself. A low-confidence or
uncertain result becomes REVIEW rather than an automatic send.

## Local run

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python bot.py
```

Set at least:

- `BOT_TOKEN`
- `OWNER_ID`

AI is optional. Set `AI_ENABLED=true` and an OpenAI-compatible endpoint/key if
you want semantic rule checking.

## Telegram commands

- `/start`
- `/help`
- `/addchat <chat_id> [title]`
- `/removechat <chat_id>`
- `/setrules <chat_id> <rules>`
- `/rules <chat_id>`
- `/limit <chat_id> <posts_per_24h> <cooldown_minutes>`
- `/check <chat_id>` then send the content
- `/post <chat_id>` then send the content
- `/schedule <chat_id> <minutes> <message>`
- `/unschedule <chat_id>`
- `/schedules`
- `/status`
- `/logs [count]`
- `/pause`
- `/resume`

For `/schedule`, the message is stored and checked before each scheduled send.

## Deployment

Use a persistent service/VM/container for 24/7 operation. GitHub is the source
repository; GitHub Actions can deploy to a server, but a normal Actions job
should not be used as the permanent bot process.

Never commit `.env` or a bot token.
