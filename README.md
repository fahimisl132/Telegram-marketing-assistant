# Telegram Group Rules Compliance Bot

This bot is a private **rules analyzer + draft checker + rewrite assistant**.

## Features
- `/analyze <chat_id>` gets accessible Telegram chat metadata, description and pinned message.
- `/setrules <chat_id>` lets you paste/forward rules that the bot cannot access.
- AI extracts allowed/disallowed topics, format/language requirements, promotion and link rules.
- `/check <chat_id>` checks a draft and returns `APPROVE`, `REVIEW`, or `BLOCK`.
- If a compliant rewrite is reasonably possible, it provides a suggested rewrite.
- Owner-only commands.
- SQLite audit history.
- **No automatic posting** to third-party groups.

## Telegram limitation
A Bot API bot cannot read a complete private group history just from a chat ID. The relevant information must be accessible to the bot, or you must paste/forward the rules. Telegram's `getChat` exposes chat information such as description, and may expose a pinned message when available.

## Setup
1. Create a Telegram bot with BotFather.
2. Put `BOT_TOKEN` and your Telegram numeric `OWNER_ID` in environment variables.
3. Configure an OpenAI-compatible API:
   - `AI_ENABLED=true`
   - `AI_API_KEY=...`
   - `AI_BASE_URL=https://api.openai.com/v1`
   - `AI_MODEL=...`
4. Run: `pip install -r requirements.txt && python bot.py`

## Workflow
`/analyze -1001234567890`
If rules are missing:
`/setrules -1001234567890`
Then paste/forward the rules.

For a draft:
`/check -1001234567890`
Then send the message.

Review the result yourself before posting. AI cannot guarantee how a moderator will interpret a rule.

## Railway
Deploy this repository as a persistent service. Add the environment variables in Railway Variables. Do not commit `.env` or API keys. SQLite is fine for a simple single instance; use persistent storage or an external database if you need durable data across replacements.

## Safety
The bot does not impersonate users, bypass group permissions, bypass anti-spam systems, or automatically post to groups. It only helps analyze rules and prepare a draft for manual review.
