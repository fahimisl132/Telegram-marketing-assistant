import os
import re
import asyncio
import logging
import sqlite3
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv
from rapidfuzz import fuzz
from telethon import TelegramClient, functions, types
from telethon.tl.types import (
    InputPeerEmpty,
    InputMessagesFilterEmpty,
    Channel,
    Chat,
)
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("group-finder")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "").strip()
DB_PATH = os.getenv("DB_PATH", "finder.db")
MAX_RESULTS = int(os.getenv("MAX_RESULTS", "20"))
SEARCH_LIMIT = int(os.getenv("SEARCH_LIMIT_PER_QUERY", "50"))
POST_SAMPLE_LIMIT = int(os.getenv("POST_SAMPLE_LIMIT", "12"))

ADMIN_IDS = {
    int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

COUNTRIES = {
    "BD": ("🇧🇩", "Bangladesh", ["bangladesh", "bd", "বাংলাদেশ", "bangla", "ঢাকা", "dhaka"]),
    "IN": ("🇮🇳", "India", ["india", "indian", "in", "ভারত", "hindi", "delhi", "mumbai"]),
    "PK": ("🇵🇰", "Pakistan", ["pakistan", "pakistani", "pk", "پاکستان", "urdu", "lahore", "karachi"]),
    "US": ("🇺🇸", "United States", ["united states", "usa", "us", "american", "america", "new york", "california"]),
    "GB": ("🇬🇧", "United Kingdom", ["united kingdom", "uk", "britain", "british", "england", "london"]),
    "AE": ("🇦🇪", "United Arab Emirates", ["uae", "united arab emirates", "dubai", "abu dhabi", "emirates", "الإمارات"]),
    "SA": ("🇸🇦", "Saudi Arabia", ["saudi", "saudi arabia", "ksa", "riyadh", "jeddah", "السعودية"]),
    "MY": ("🇲🇾", "Malaysia", ["malaysia", "malaysian", "kuala lumpur", "malay"]),
    "SG": ("🇸🇬", "Singapore", ["singapore", "singaporean"]),
    "CA": ("🇨🇦", "Canada", ["canada", "canadian", "toronto", "vancouver"]),
    "AU": ("🇦🇺", "Australia", ["australia", "australian", "sydney", "melbourne"]),
    "OTHER": ("🌍", "Other / custom", []),
}

# Useful country-language hints. These are heuristic only.
LANG_HINTS = {
    "BD": ["বাংলা", "bangla", "bengali"],
    "IN": ["hindi", "हिंदी", "tamil", "telugu", "marathi", "bengali", "english"],
    "PK": ["urdu", "پنجابی", "pashto", "sindhi"],
    "AE": ["arabic", "عربي", "english"],
    "SA": ["arabic", "عربي"],
    "MY": ["malay", "bahasa melayu"],
}

def db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS groups (
            id INTEGER PRIMARY KEY,
            telegram_id INTEGER UNIQUE,
            username TEXT,
            title TEXT,
            about TEXT,
            country TEXT,
            last_posts TEXT,
            url TEXT,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    con.commit()
    return con

def is_admin(uid: int) -> bool:
    # If ADMIN_IDS is empty, refuse rather than accidentally exposing a utility bot.
    return uid in ADMIN_IDS

def normalize(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower()).strip()

def tokens(s: str):
    # Keep Unicode words; remove punctuation.
    return re.findall(r"[^\W_]+", normalize(s), flags=re.UNICODE)

def country_score(text: str, code: str) -> float:
    if code == "OTHER":
        return 50.0
    t = normalize(text)
    terms = COUNTRIES[code][2]
    hits = sum(1 for term in terms if term in t)
    lang_hits = sum(1 for term in LANG_HINTS.get(code, []) if term in t)
    if not hits and not lang_hits:
        return 0.0
    return min(100.0, hits * 28.0 + lang_hits * 12.0)

def relevance(query: str, country_code: str, title: str, username: str, about: str, posts: str):
    q = normalize(query)
    fields = {
        "name": normalize(title),
        "username": normalize(username),
        "description": normalize(about),
        "posts": normalize(posts),
    }
    # Fuzzy semantic-ish lexical matching. This is intentionally transparent
    # and does not claim to be an embedding/LLM probability.
    name = fuzz.token_set_ratio(q, fields["name"]) if fields["name"] else 0
    user = fuzz.token_set_ratio(q, fields["username"]) if fields["username"] else 0
    desc = fuzz.token_set_ratio(q, fields["description"]) if fields["description"] else 0
    post = fuzz.token_set_ratio(q, fields["posts"]) if fields["posts"] else 0

    # Exact query-token coverage boosts results with matching words.
    qt = set(tokens(q))
    combined = " ".join(fields.values())
    coverage = (sum(1 for x in qt if x in combined) / max(1, len(qt))) * 100

    cscore = country_score(
        " ".join([fields["name"], fields["username"], fields["description"], fields["posts"]]),
        country_code,
    )

    # Country is a filter/ranking signal, not a guarantee.
    score = (
        name * 0.18 +
        user * 0.08 +
        desc * 0.24 +
        post * 0.30 +
        coverage * 0.10 +
        cscore * 0.10
    )
    return round(min(100, score), 1), cscore

def make_url(entity) -> Optional[str]:
    username = getattr(entity, "username", None)
    if username:
        return f"https://t.me/{username}"
    return None

async def fetch_about(client, entity) -> str:
    try:
        if isinstance(entity, Channel):
            full = await client(functions.channels.GetFullChannelRequest(entity))
            return getattr(full.full_chat, "about", "") or ""
        if isinstance(entity, Chat):
            full = await client(functions.messages.GetFullChatRequest(entity.id))
            return getattr(full.full_chat, "about", "") or ""
    except Exception as e:
        log.debug("about lookup failed: %s", e)
    return ""

async def sample_posts(client, entity, limit=POST_SAMPLE_LIMIT) -> str:
    texts = []
    try:
        # Publicly readable messages only.
        async for msg in client.iter_messages(entity, limit=limit):
            if msg and getattr(msg, "message", None):
                texts.append(msg.message[:1200])
    except Exception as e:
        log.debug("post lookup failed: %s", e)
    return "\n".join(texts)

async def global_search(client, query: str, limit: int = SEARCH_LIMIT):
    # Telegram's global search returns messages plus related chats.
    result = await client(functions.messages.SearchGlobalRequest(
        q=query,
        filter=InputMessagesFilterEmpty(),
        min_date=None,
        max_date=None,
        offset_id=0,
        offset_rate=0,
        offset_peer=InputPeerEmpty(),
        limit=limit,
    ))
    entities = {}
    for chat in getattr(result, "chats", []):
        if isinstance(chat, (Channel, Chat)):
            entities[chat.id] = chat
    for msg in getattr(result, "messages", []):
        peer_id = getattr(msg, "peer_id", None)
        if peer_id is not None:
            cid = getattr(peer_id, "channel_id", None) or getattr(peer_id, "chat_id", None)
            if cid is not None and cid not in entities:
                try:
                    ent = await client.get_entity(peer_id)
                    if isinstance(ent, (Channel, Chat)):
                        entities[cid] = ent
                except Exception:
                    pass
    return list(entities.values())

async def discover(client, query: str, country_code: str):
    # Multiple query variants improve recall without pretending to enumerate all Telegram.
    country_terms = []
    if country_code != "OTHER":
        country_terms = COUNTRIES[country_code][2][:4]

    queries = [query]
    if country_terms:
        # Search both user wording and country-qualified wording.
        queries.extend([f"{query} {country_terms[0]}", f"{country_terms[0]} {query}"])
    queries = list(dict.fromkeys(queries))

    found = {}
    for q in queries:
        try:
            ents = await global_search(client, q)
            for ent in ents:
                uid = getattr(ent, "id", None)
                if uid is not None:
                    found[uid] = ent
        except Exception as e:
            log.warning("search failed for %r: %s", q, e)
        await asyncio.sleep(0.5)

    rows = []
    for ent in found.values():
        title = getattr(ent, "title", "") or ""
        username = getattr(ent, "username", "") or ""
        # Ignore broadcast channels unless the user explicitly wants channels.
        if isinstance(ent, Channel) and getattr(ent, "broadcast", False):
            continue

        about = await fetch_about(client, ent)
        posts = await sample_posts(client, ent)
        score, cscore = relevance(query, country_code, title, username, about, posts)

        # Country filter: keep strong country evidence, but allow semantically
        # strong results because Telegram has no authoritative country metadata.
        if country_code != "OTHER" and cscore < 10 and score < 62:
            continue

        url = make_url(ent)
        if not url:
            continue

        rows.append({
            "telegram_id": int(getattr(ent, "id")),
            "title": title,
            "username": username,
            "about": about,
            "posts": posts,
            "url": url,
            "score": score,
            "country_score": cscore,
        })

    rows.sort(key=lambda x: (-x["score"], -x["country_score"], x["title"].lower()))
    return rows[:MAX_RESULTS]

def save_rows(rows, country_code):
    con = db()
    for r in rows:
        con.execute("""
            INSERT INTO groups(telegram_id, username, title, about, country, last_posts, url)
            VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(telegram_id) DO UPDATE SET
              username=excluded.username,
              title=excluded.title,
              about=excluded.about,
              country=excluded.country,
              last_posts=excluded.last_posts,
              url=excluded.url,
              updated_at=CURRENT_TIMESTAMP
        """, (
            r["telegram_id"], r["username"], r["title"], r["about"],
            country_code, r["posts"], r["url"]
        ))
    con.commit()
    con.close()

@dataclass
class UserState:
    country: str = "OTHER"
    waiting_query: bool = False

STATES = {}

def country_keyboard():
    rows = []
    current = list(COUNTRIES.items())
    for i in range(0, len(current), 2):
        row = []
        for code, (flag, name, _) in current[i:i+2]:
            row.append(InlineKeyboardButton(f"{flag} {name}", callback_data=f"country:{code}"))
        rows.append(row)
    return InlineKeyboardMarkup(rows)

def menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔎 Search groups", callback_data="search")],
        [InlineKeyboardButton("🌍 Change country", callback_data="country_menu")],
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text("This bot is restricted to its configured admin user(s).")
        return
    STATES[uid] = UserState()
    await update.message.reply_text(
        "Telegram Public Group Finder\n\nChoose a country first, then describe the groups you want.",
        reply_markup=menu_keyboard(),
    )

async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    if not is_admin(uid):
        return

    state = STATES.setdefault(uid, UserState())

    if q.data == "country_menu":
        await q.edit_message_text("🌍 Select country:", reply_markup=country_keyboard())
        return

    if q.data == "search":
        state.waiting_query = True
        country = COUNTRIES[state.country]
        await q.edit_message_text(
            f"{country[0]} {country[1]} selected.\n\n"
            "Now send what kind of public groups you want.\n"
            "Example: `freelancing and remote jobs for Bangladesh`"
        )
        return

    if q.data.startswith("country:"):
        code = q.data.split(":", 1)[1]
        if code in COUNTRIES:
            state.country = code
            state.waiting_query = True
            flag, name, _ = COUNTRIES[code]
            await q.edit_message_text(
                f"{flag} {name} selected.\n\n"
                "Send the type/topic of public groups you want to find."
            )

async def text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return

    state = STATES.setdefault(uid, UserState())
    if not state.waiting_query:
        await update.message.reply_text("Press Search groups first.", reply_markup=menu_keyboard())
        return

    query = (update.message.text or "").strip()
    if len(query) < 2:
        await update.message.reply_text("Please enter a more specific search.")
        return

    state.waiting_query = False
    flag, cname, _ = COUNTRIES[state.country]
    status = await update.message.reply_text(
        f"🔎 Searching public Telegram results...\n"
        f"{flag} Country: {cname}\n"
        f"📝 Query: {query}\n\n"
        "This can take a little while because public descriptions and recent public posts are checked."
    )

    client = context.application.bot_data["client"]
    try:
        rows = await discover(client, query, state.country)
        save_rows(rows, state.country)
    except Exception as e:
        log.exception("discovery failed")
        await status.edit_text(
            "Search failed. Check that the Telegram user session is authorized and try again."
        )
        return

    if not rows:
        await status.edit_text(
            "No sufficiently relevant public groups were found for that country/query.\n\n"
            "Try broader keywords or another country."
        )
        return

    chunks = []
    for i, r in enumerate(rows, 1):
        about = re.sub(r"\s+", " ", r["about"]).strip()
        if len(about) > 180:
            about = about[:177] + "..."
        post_preview = re.sub(r"\s+", " ", r["posts"]).strip()
        post_preview = post_preview[:140] + ("..." if len(post_preview) > 140 else "")
        username = f"@{r['username']}" if r["username"] else "(no public username)"

        chunks.append(
            f"{i}. <b>{escape_html(r['title'])}</b>\n"
            f"👤 {escape_html(username)}\n"
            f"🎯 Relevance: <b>{r['score']}%</b>\n"
            f"📝 {escape_html(about or 'No public description')}\n"
            f"📢 Post match: {escape_html(post_preview or 'No readable recent public post sample')}\n"
            f"🔗 <a href=\"{r['url']}\">Open group</a>"
        )

    # Telegram message size is limited; split output safely.
    header = f"{flag} <b>{escape_html(cname)}</b> — {len(rows)} public group result(s)\n\n"
    current = header
    for item in chunks:
        if len(current) + len(item) + 2 > 3900:
            await update.message.reply_text(current, parse_mode="HTML", disable_web_page_preview=True)
            current = ""
        current += item + "\n\n"
    if current:
        await update.message.reply_text(current, parse_mode="HTML", disable_web_page_preview=True)

    await update.message.reply_text(
        "Search finished. Scores are program-generated relevance estimates, not Telegram ratings.",
        reply_markup=menu_keyboard(),
    )

def escape_html(s: str) -> str:
    return (
        str(s).replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )

async def post_init(application: Application):
    client = TelegramClient("finder", API_ID, API_HASH)
    await client.start()
    application.bot_data["client"] = client
    log.info("Telegram user session authorized.")

async def post_shutdown(application: Application):
    client = application.bot_data.get("client")
    if client:
        await client.disconnect()

def main():
    if not BOT_TOKEN or not API_ID or not API_HASH:
        raise SystemExit("Set BOT_TOKEN, API_ID and API_HASH in .env")
    if not ADMIN_IDS:
        raise SystemExit("Set ADMIN_IDS in .env")

    db().close()
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(buttons))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message))
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
