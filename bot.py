import asyncio
import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Optional

import aiohttp
from dotenv import load_dotenv
from telegram import Update
from telegram.error import TelegramError
from telegram.ext import (
    Application, CommandHandler, ContextTypes, MessageHandler, filters
)

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("telegram-marketing-assistant")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0") or "0")
DB_PATH = os.getenv("DATABASE_PATH", "bot.db")
AI_ENABLED = os.getenv("AI_ENABLED", "false").lower() == "true"
AI_BASE_URL = os.getenv("AI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
AI_API_KEY = os.getenv("AI_API_KEY", "").strip()
AI_MODEL = os.getenv("AI_MODEL", "").strip()
DEFAULT_LIMIT = int(os.getenv("DEFAULT_POSTS_PER_24H", "2"))
DEFAULT_COOLDOWN = int(os.getenv("DEFAULT_COOLDOWN_MINUTES", "360"))
POLL_SECONDS = int(os.getenv("SCHEDULER_POLL_SECONDS", "15"))

if not BOT_TOKEN or not OWNER_ID:
    raise SystemExit("BOT_TOKEN and OWNER_ID are required.")

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.execute("PRAGMA journal_mode=WAL")

db.executescript("""
CREATE TABLE IF NOT EXISTS chats (
    chat_id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    rules TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    posts_per_24h INTEGER NOT NULL DEFAULT 2,
    cooldown_minutes INTEGER NOT NULL DEFAULT 360,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    content TEXT NOT NULL,
    status TEXT NOT NULL,
    verdict TEXT NOT NULL DEFAULT '',
    reasons TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS schedules (
    chat_id TEXT PRIMARY KEY,
    interval_minutes INTEGER NOT NULL,
    content TEXT NOT NULL,
    next_run INTEGER NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,
    chat_id TEXT,
    detail TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL
);
""")
db.commit()

pending = {}
send_enabled = True

@dataclass
class Verdict:
    status: str
    reasons: list[str]

def now() -> int:
    return int(time.time())

def h(text: str) -> str:
    return hashlib.sha256(text.strip().lower().encode("utf-8")).hexdigest()

def audit(action: str, chat_id: str | None, detail: str = ""):
    db.execute(
        "INSERT INTO audit(action,chat_id,detail,created_at) VALUES(?,?,?,?)",
        (action, chat_id, detail, now())
    )
    db.commit()

def is_owner(update: Update) -> bool:
    return bool(update.effective_user and update.effective_user.id == OWNER_ID)

def chat_row(chat_id: str):
    return db.execute("SELECT * FROM chats WHERE chat_id=?", (chat_id,)).fetchone()

def recent_sent(chat_id: str) -> int:
    return db.execute(
        "SELECT COUNT(*) FROM posts WHERE chat_id=? AND status='SENT' AND created_at>=?",
        (chat_id, now() - 86400)
    ).fetchone()[0]

def duplicate(chat_id: str, content: str) -> bool:
    return db.execute(
        "SELECT 1 FROM posts WHERE chat_id=? AND content_hash=? AND status='SENT' LIMIT 1",
        (chat_id, h(content))
    ).fetchone() is not None

def last_sent(chat_id: str) -> Optional[int]:
    row = db.execute(
        "SELECT created_at FROM posts WHERE chat_id=? AND status='SENT' ORDER BY created_at DESC LIMIT 1",
        (chat_id,)
    ).fetchone()
    return int(row["created_at"]) if row else None

def local_check(row, content: str) -> Verdict:
    if not row:
        return Verdict("BLOCK", ["This chat is not configured."])
    if not row["enabled"]:
        return Verdict("BLOCK", ["This chat is disabled."])
    if not content.strip():
        return Verdict("BLOCK", ["Empty content."])
    if len(content) > 4096:
        return Verdict("BLOCK", ["Text is longer than Telegram's normal single-message limit."])
    if not row["rules"].strip():
        return Verdict("REVIEW", ["No explicit group rules are stored."])

    reasons = []

    if duplicate(row["chat_id"], content):
        return Verdict("BLOCK", ["Exact duplicate content was already sent to this chat."])

    if recent_sent(row["chat_id"]) >= row["posts_per_24h"]:
        return Verdict("REVIEW", [
            f"The configured 24-hour limit ({row['posts_per_24h']}) has been reached."
        ])

    last = last_sent(row["chat_id"])
    if last:
        remaining = row["cooldown_minutes"] * 60 - (now() - last)
        if remaining > 0:
            return Verdict("REVIEW", [
                f"Cooldown is active for another {max(1, remaining // 60)} minute(s)."
            ])

    # Conservative local screening. This is not a claim that Telegram bans
    # these words; it is a project-specific review trigger.
    patterns = {
        "adult sexual content": r"\b(porn|xxx|sexcam)\b",
        "gambling language": r"\b(casino|betting|sportsbook)\b",
        "phishing/security abuse": r"\b(phishing|credential\s*steal|steal\s+password)\b",
        "fraud/scam language": r"\b(guaranteed\s+profit|double\s+your\s+money|send\s+otp)\b",
    }
    low = content.lower()
    hits = [name for name, pat in patterns.items() if re.search(pat, low)]
    if hits:
        return Verdict("REVIEW", ["Potentially restricted/risky category detected: " + ", ".join(hits)])

    return Verdict("APPROVE", ["Passed local checks."])

async def ai_check(rules: str, content: str) -> Verdict | None:
    if not AI_ENABLED:
        return None
    if not AI_API_KEY or not AI_MODEL:
        return Verdict("REVIEW", ["AI checking is enabled but AI_API_KEY or AI_MODEL is missing."])

    system = """You are a conservative compliance checker for a Telegram group.
Compare the provided group rules with the proposed marketing message.
Do not invent rules. If the rules are ambiguous or the message cannot be
confidently judged, return REVIEW. Never claim that your result guarantees
Telegram policy compliance.

Return JSON only:
{"verdict":"APPROVE|REVIEW|BLOCK","reasons":["short reason", "..."]}

APPROVE means the message clearly satisfies the supplied group rules.
BLOCK means it clearly violates an explicit rule.
REVIEW means ambiguity or insufficient information."""
    payload = {
        "model": AI_MODEL,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": f"GROUP RULES:\n{rules}\n\nMESSAGE:\n{content}"}
        ],
    }
    headers = {"Authorization": f"Bearer {AI_API_KEY}", "Content-Type": "application/json"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{AI_BASE_URL}/chat/completions",
                headers=headers,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=45),
            ) as resp:
                if resp.status >= 400:
                    return Verdict("REVIEW", [f"AI service returned HTTP {resp.status}."])
                data = await resp.json()
        raw = data["choices"][0]["message"]["content"]
        obj = json.loads(raw)
        verdict = str(obj.get("verdict", "REVIEW")).upper()
        reasons = obj.get("reasons", [])
        if verdict not in {"APPROVE", "REVIEW", "BLOCK"}:
            verdict = "REVIEW"
        if not isinstance(reasons, list) or not reasons:
            reasons = ["AI returned no usable reason."]
        return Verdict(verdict, [str(x) for x in reasons[:5]])
    except Exception as exc:
        log.exception("AI check failed")
        return Verdict("REVIEW", [f"AI check failed safely: {type(exc).__name__}."])

async def combined_check(chat_id: str, content: str) -> Verdict:
    row = chat_row(chat_id)
    local = local_check(row, content)
    if local.status in {"BLOCK", "REVIEW"}:
        return local

    ai = await ai_check(row["rules"], content)
    if ai is None:
        return local

    # AI can only make the outcome more conservative.
    if ai.status == "BLOCK":
        return ai
    if ai.status == "REVIEW":
        return ai
    return Verdict("APPROVE", local.reasons + ai.reasons)

def record_post(chat_id, content, status, verdict, reasons):
    db.execute(
        """INSERT INTO posts(chat_id,content_hash,content,status,verdict,reasons,created_at)
           VALUES(?,?,?,?,?,?,?)""",
        (chat_id, h(content), content, status, verdict, "\n".join(reasons), now())
    )
    db.commit()

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    await update.message.reply_text(
        "/addchat <chat_id> [title]\n"
        "/removechat <chat_id>\n"
        "/setrules <chat_id> <rules>\n"
        "/rules <chat_id>\n"
        "/limit <chat_id> <posts_per_24h> <cooldown_minutes>\n"
        "/check <chat_id>  তারপর message পাঠাও\n"
        "/post <chat_id>   তারপর message পাঠাও\n"
        "/schedule <chat_id> <minutes> <message>\n"
        "/unschedule <chat_id>\n"
        "/schedules\n"
        "/status\n"
        "/logs [count]\n"
        "/pause\n"
        "/resume"
    )

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_owner(update):
        await update.message.reply_text("Bot is ready. Use /help for commands.")

async def addchat_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update) or not context.args:
        return await update.message.reply_text("Usage: /addchat <chat_id> [title]")
    cid = context.args[0]
    title = " ".join(context.args[1:]) or cid
    try:
        c = await context.bot.get_chat(cid)
        title = c.title or c.username or title
    except TelegramError:
        pass
    db.execute(
        """INSERT INTO chats(chat_id,title,rules,posts_per_24h,cooldown_minutes,created_at)
           VALUES(?,?,?,?,?,?)
           ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title""",
        (cid, title, "", DEFAULT_LIMIT, DEFAULT_COOLDOWN, now())
    )
    db.commit()
    audit("ADD_CHAT", cid, title)
    await update.message.reply_text(
        f"Added {title} ({cid}). Now use /setrules {cid} <rules>."
    )

async def removechat_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update) or not context.args:
        return
    cid = context.args[0]
    db.execute("DELETE FROM chats WHERE chat_id=?", (cid,))
    db.execute("DELETE FROM schedules WHERE chat_id=?", (cid,))
    db.commit()
    audit("REMOVE_CHAT", cid)
    await update.message.reply_text("Chat removed from the assistant.")

async def setrules_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update) or len(context.args) < 2:
        return await update.message.reply_text("Usage: /setrules <chat_id> <rules>")
    cid, rules = context.args[0], " ".join(context.args[1:])
    if not chat_row(cid):
        return await update.message.reply_text("Run /addchat first.")
    db.execute("UPDATE chats SET rules=? WHERE chat_id=?", (rules, cid))
    db.commit()
    audit("SET_RULES", cid)
    await update.message.reply_text("Rules saved.")

async def rules_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update) or not context.args:
        return
    row = chat_row(context.args[0])
    await update.message.reply_text(
        row["rules"] if row else "No configured rules."
    )

async def limit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update) or len(context.args) != 3:
        return await update.message.reply_text(
            "Usage: /limit <chat_id> <posts_per_24h> <cooldown_minutes>"
        )
    cid = context.args[0]
    if not chat_row(cid):
        return await update.message.reply_text("Unknown chat.")
    try:
        limit = max(1, int(context.args[1]))
        cooldown = max(0, int(context.args[2]))
    except ValueError:
        return await update.message.reply_text("Numbers required.")
    db.execute(
        "UPDATE chats SET posts_per_24h=?,cooldown_minutes=? WHERE chat_id=?",
        (limit, cooldown, cid)
    )
    db.commit()
    await update.message.reply_text("Limits updated.")

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    rows = db.execute("SELECT * FROM chats ORDER BY title").fetchall()
    if not rows:
        return await update.message.reply_text("No chats configured.")
    lines = []
    for r in rows:
        lines.append(
            f"{r['title']} | {r['chat_id']} | "
            f"{'ON' if r['enabled'] else 'OFF'} | "
            f"{recent_sent(r['chat_id'])}/{r['posts_per_24h']} today"
        )
    await update.message.reply_text("\n".join(lines))

async def logs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    count = 10
    if context.args:
        try:
            count = min(50, max(1, int(context.args[0])))
        except ValueError:
            pass
    rows = db.execute(
        "SELECT action,chat_id,detail,created_at FROM audit ORDER BY id DESC LIMIT ?",
        (count,)
    ).fetchall()
    if not rows:
        return await update.message.reply_text("No logs.")
    lines = []
    for r in rows:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(r["created_at"]))
        lines.append(f"{stamp} | {r['action']} | {r['chat_id']} | {r['detail']}")
    await update.message.reply_text("\n".join(lines))

async def pause_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global send_enabled
    if is_owner(update):
        send_enabled = False
        audit("PAUSE", None)
        await update.message.reply_text("Sending paused.")

async def resume_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global send_enabled
    if is_owner(update):
        send_enabled = True
        audit("RESUME", None)
        await update.message.reply_text("Sending resumed.")

async def begin_pending(update, mode, cid):
    if not chat_row(cid):
        return await update.message.reply_text("Unknown chat. Use /addchat first.")
    pending[OWNER_ID] = {"mode": mode, "chat_id": cid}
    await update.message.reply_text("Now send the marketing message.")

async def check_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update) or not context.args:
        return
    await begin_pending(update, "CHECK", context.args[0])

async def post_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update) or not context.args:
        return
    await begin_pending(update, "POST", context.args[0])

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update) or not update.message or not update.message.text:
        return
    job = pending.pop(OWNER_ID, None)
    if not job:
        return
    cid, mode, content = job["chat_id"], job["mode"], update.message.text.strip()
    verdict = await combined_check(cid, content)
    await update.message.reply_text(
        f"{verdict.status}\n\n" + "\n".join("• " + x for x in verdict.reasons)
    )
    if mode == "CHECK" or verdict.status != "APPROVE":
        return
    if not send_enabled:
        return await update.message.reply_text("NOT SENT: global sending is paused.")
    try:
        sent = await context.bot.send_message(chat_id=cid, text=content)
        record_post(cid, content, "SENT", verdict.status, verdict.reasons)
        audit("SEND", cid, f"message_id={sent.message_id}")
        await update.message.reply_text(f"SENT. Telegram message ID: {sent.message_id}")
    except Exception as exc:
        record_post(cid, content, "FAILED", verdict.status, [str(exc)])
        audit("SEND_FAILED", cid, type(exc).__name__)
        await update.message.reply_text(
            "NOT SENT. The bot may not have permission to send in this chat."
        )

async def schedule_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update) or len(context.args) < 3:
        return await update.message.reply_text(
            "/schedule <chat_id> <minutes> <message>"
        )
    cid = context.args[0]
    if not chat_row(cid):
        return await update.message.reply_text("Unknown chat.")
    try:
        minutes = max(1, int(context.args[1]))
    except ValueError:
        return await update.message.reply_text("Minutes must be a number.")
    content = " ".join(context.args[2:]).strip()
    db.execute(
        """INSERT INTO schedules(chat_id,interval_minutes,content,next_run,enabled)
           VALUES(?,?,?,?,1)
           ON CONFLICT(chat_id) DO UPDATE SET
           interval_minutes=excluded.interval_minutes,
           content=excluded.content,
           next_run=excluded.next_run,
           enabled=1""",
        (cid, minutes, content, now() + minutes * 60)
    )
    db.commit()
    audit("SCHEDULE", cid, f"{minutes} minutes")
    await update.message.reply_text("Schedule saved.")

async def unschedule_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update) or not context.args:
        return
    cid = context.args[0]
    db.execute("DELETE FROM schedules WHERE chat_id=?", (cid,))
    db.commit()
    audit("UNSCHEDULE", cid)
    await update.message.reply_text("Schedule removed.")

async def schedules_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    rows = db.execute("SELECT * FROM schedules ORDER BY next_run").fetchall()
    if not rows:
        return await update.message.reply_text("No schedules.")
    lines = []
    for r in rows:
        stamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(r["next_run"]))
        lines.append(f"{r['chat_id']} | every {r['interval_minutes']}m | next {stamp}")
    await update.message.reply_text("\n".join(lines))

async def scheduler(app: Application):
    global send_enabled
    while True:
        try:
            if send_enabled:
                rows = db.execute(
                    "SELECT * FROM schedules WHERE enabled=1 AND next_run<=?",
                    (now(),)
                ).fetchall()
                for s in rows:
                    row = chat_row(s["chat_id"])
                    if not row:
                        db.execute("DELETE FROM schedules WHERE chat_id=?", (s["chat_id"],))
                        db.commit()
                        continue

                    verdict = await combined_check(s["chat_id"], s["content"])
                    if verdict.status == "APPROVE":
                        try:
                            sent = await app.bot.send_message(
                                chat_id=s["chat_id"], text=s["content"]
                            )
                            record_post(
                                s["chat_id"], s["content"], "SENT",
                                verdict.status, verdict.reasons
                            )
                            audit("SCHEDULE_SEND", s["chat_id"], f"message_id={sent.message_id}")
                        except Exception as exc:
                            audit("SCHEDULE_SEND_FAILED", s["chat_id"], type(exc).__name__)
                    else:
                        audit(
                            "SCHEDULE_SKIPPED", s["chat_id"],
                            verdict.status + ": " + "; ".join(verdict.reasons)
                        )

                    db.execute(
                        "UPDATE schedules SET next_run=? WHERE chat_id=?",
                        (now() + s["interval_minutes"] * 60, s["chat_id"])
                    )
                    db.commit()
        except Exception:
            log.exception("scheduler error")
        await asyncio.sleep(POLL_SECONDS)

async def post_init(app: Application):
    app.create_task(scheduler(app))

def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("addchat", addchat_cmd))
    app.add_handler(CommandHandler("removechat", removechat_cmd))
    app.add_handler(CommandHandler("setrules", setrules_cmd))
    app.add_handler(CommandHandler("rules", rules_cmd))
    app.add_handler(CommandHandler("limit", limit_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("logs", logs_cmd))
    app.add_handler(CommandHandler("pause", pause_cmd))
    app.add_handler(CommandHandler("resume", resume_cmd))
    app.add_handler(CommandHandler("check", check_cmd))
    app.add_handler(CommandHandler("post", post_cmd))
    app.add_handler(CommandHandler("schedule", schedule_cmd))
    app.add_handler(CommandHandler("unschedule", unschedule_cmd))
    app.add_handler(CommandHandler("schedules", schedules_cmd))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, handle_text))
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
