import os, json, sqlite3, hashlib, logging
from datetime import datetime, timezone
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
BOT_TOKEN=os.environ["BOT_TOKEN"]; OWNER_ID=int(os.environ["OWNER_ID"])
DB_PATH=os.getenv("DB_PATH","compliance_bot.sqlite3")
AI_ENABLED=os.getenv("AI_ENABLED","true").lower() in {"1","true","yes","on"}
AI_BASE_URL=os.getenv("AI_BASE_URL","https://api.openai.com/v1")
AI_API_KEY=os.getenv("AI_API_KEY",""); AI_MODEL=os.getenv("AI_MODEL","gpt-5.6-mini")
MAX_TEXT=8000; MAX_RULES=20000

def now(): return datetime.now(timezone.utc).isoformat()
def db():
    c=sqlite3.connect(DB_PATH); c.row_factory=sqlite3.Row; return c
def init_db():
    c=db(); c.executescript("""
    CREATE TABLE IF NOT EXISTS chats(
      chat_id TEXT PRIMARY KEY,title TEXT,username TEXT,chat_type TEXT,
      description TEXT,pinned_text TEXT,rules_text TEXT,created_at TEXT,updated_at TEXT);
    CREATE TABLE IF NOT EXISTS drafts(
      id INTEGER PRIMARY KEY AUTOINCREMENT,owner_id INTEGER,chat_id TEXT,original_text TEXT,
      decision TEXT,modified_text TEXT,reasons TEXT,created_at TEXT);
    CREATE TABLE IF NOT EXISTS audit(
      id INTEGER PRIMARY KEY AUTOINCREMENT,owner_id INTEGER,action TEXT,chat_id TEXT,
      details TEXT,created_at TEXT);
    """); c.commit(); c.close()
def owner(u): return bool(u.effective_user and u.effective_user.id==OWNER_ID)
async def guard(u):
    if owner(u): return True
    if u.effective_message: await u.effective_message.reply_text("⛔ এই bot শুধু owner ব্যবহার করতে পারবেন।")
    return False
def audit(action,chat_id=None,details=""):
    c=db(); c.execute("INSERT INTO audit(owner_id,action,chat_id,details,created_at) VALUES(?,?,?,?,?)",
                      (OWNER_ID,action,str(chat_id) if chat_id is not None else None,details[:4000],now()))
    c.commit(); c.close()
def upsert(chat_id,title="",username="",typ="",description="",pinned=""):
    c=db(); old=c.execute("SELECT rules_text FROM chats WHERE chat_id=?",(str(chat_id),)).fetchone()
    rules=old["rules_text"] if old else ""
    c.execute("""INSERT INTO chats VALUES(?,?,?,?,?,?,?,?,?)
      ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title,username=excluded.username,
      chat_type=excluded.chat_type,description=excluded.description,pinned_text=excluded.pinned_text,
      updated_at=excluded.updated_at""",
      (str(chat_id),title,username,typ,description,pinned,rules,now(),now()))
    c.commit(); c.close()
def get(chat_id):
    c=db(); r=c.execute("SELECT * FROM chats WHERE chat_id=?",(str(chat_id),)).fetchone(); c.close(); return r
def rules(row):
    if not row: return ""
    a=[]
    if row["rules_text"]: a.append("USER-PROVIDED RULES:\n"+row["rules_text"])
    if row["pinned_text"]: a.append("PINNED MESSAGE:\n"+row["pinned_text"])
    if row["description"]: a.append("CHAT DESCRIPTION:\n"+row["description"])
    return "\n\n".join(a)
def setrules(chat_id,text):
    c=db(); c.execute("UPDATE chats SET rules_text=?,updated_at=? WHERE chat_id=?",
                      (text[:MAX_RULES],now(),str(chat_id))); c.commit(); c.close()
async def ai_json(system,user):
    if not AI_ENABLED or not AI_API_KEY: raise RuntimeError("AI is not configured.")
    async with httpx.AsyncClient(timeout=60) as x:
        r=await x.post(AI_BASE_URL.rstrip("/")+"/chat/completions",
          headers={"Authorization":"Bearer "+AI_API_KEY,"Content-Type":"application/json"},
          json={"model":AI_MODEL,"temperature":0.1,"response_format":{"type":"json_object"},
                "messages":[{"role":"system","content":system},{"role":"user","content":user}]})
        r.raise_for_status(); return json.loads(r.json()["choices"][0]["message"]["content"])

RULE_SYSTEM="""You are a conservative Telegram group-rules compliance analyst.
Use ONLY the supplied group rules/source. Never invent rules. If rules are missing, vague,
contradictory or insufficient, choose REVIEW.
Return JSON only:
{"decision":"APPROVE|REVIEW|BLOCK","confidence":0.0,"allowed_topics":[],
"disallowed_topics":[],"reasons":[],"modified_text":"","changes":[]}
APPROVE=appears compatible. BLOCK=clearly violates an explicit rule. REVIEW=uncertain.
If a safe compliant rewrite is reasonably possible, put it in modified_text. Do not evade
moderation or hide prohibited intent. Preserve factual intent where possible."""
EXTRACT_SYSTEM="""Extract explicit group rules from supplied Telegram text.
Never invent rules. Return JSON only:
{"summary":"","allowed":[],"disallowed":[],"format_requirements":[],"language_requirements":[],
"promotion_rules":[],"link_rules":[],"uncertainties":[]}"""

async def start(u,ctx):
    if not await guard(u): return
    await u.message.reply_text("👋 Rules Compliance Bot ready.\n\n/analyze <chat_id>\n/setrules <chat_id>\n/check <chat_id>\n/rewrite <chat_id>\n/rules <chat_id>\n/chats\n/status\n/logs [count]\n/cancel\n\nএটি নিজে third-party group-এ post করে না।")
async def help_cmd(u,ctx):
    if not await guard(u): return
    await u.message.reply_text(
      "/analyze <chat_id> — accessible chat info/description/pinned message নেয়\n"
      "/setrules <chat_id> — পরের message-কে rules হিসেবে save করে\n"
      "/rules <chat_id> — saved source দেখায়\n"
      "/check <chat_id> — draft check\n"
      "/rewrite <chat_id> — compliant rewrite চেষ্টা\n"
      "/chats — saved chats\n/status — status\n/logs [count]\n/cancel\n\n"
      "শুধু chat ID দিলেই private/complete group history পাওয়া যায় না; প্রয়োজন হলে rules paste/forward করতে হবে।")
async def analyze(u,ctx):
    if not await guard(u): return
    if not ctx.args: return await u.message.reply_text("ব্যবহার: /analyze -1001234567890")
    cid=ctx.args[0]
    try:
        ch=await ctx.bot.get_chat(cid); p=getattr(ch,"pinned_message",None)
        pt=(getattr(p,"text",None) or getattr(p,"caption",None) or "") if p else ""
        upsert(ch.id,getattr(ch,"title","") or "",getattr(ch,"username","") or "",
               getattr(ch,"type","") or "",getattr(ch,"description","") or "",pt)
        src=rules(get(ch.id))
        if not src:
            return await u.message.reply_text(f"✅ Chat found: {ch.title or ch.id}\n\nRules source পাওয়া যায়নি। /setrules {ch.id} ব্যবহার করো।")
        if not AI_ENABLED or not AI_API_KEY:
            return await u.message.reply_text("✅ Source saved.\n\n"+src[:6000]+"\n\nAI configure করলে structured analysis পাওয়া যাবে।")
        res=await ai_json(EXTRACT_SYSTEM,src[:MAX_RULES])
        out=(f"🔎 <b>{ch.title or ch.id}</b>\n\n<b>Summary:</b> {res.get('summary','')}\n\n"
             f"✅ <b>Allowed:</b>\n"+"\n".join("• "+x for x in res.get("allowed",[]))+
             "\n\n❌ <b>Disallowed:</b>\n"+"\n".join("• "+x for x in res.get("disallowed",[]))+
             "\n\n📝 <b>Format:</b>\n"+"\n".join("• "+x for x in res.get("format_requirements",[]))+
             "\n\n🌐 <b>Language:</b>\n"+"\n".join("• "+x for x in res.get("language_requirements",[]))+
             "\n\n📢 <b>Promotion:</b>\n"+"\n".join("• "+x for x in res.get("promotion_rules",[]))+
             "\n\n🔗 <b>Links:</b>\n"+"\n".join("• "+x for x in res.get("link_rules",[]))+
             "\n\n⚠️ <b>Uncertainties:</b>\n"+"\n".join("• "+x for x in res.get("uncertainties",[])))
        await u.message.reply_text(out,parse_mode="HTML"); audit("analyze",ch.id,src[:1000])
    except Exception as e:
        await u.message.reply_text("❌ Chat info পাওয়া যায়নি। Bot-এর access/ID যাচাই করো।\n\n"+str(e))
async def setrules_cmd(u,ctx):
    if not await guard(u): return
    if not ctx.args: return await u.message.reply_text("ব্যবহার: /setrules <chat_id>")
    cid=ctx.args[0]
    if not get(cid): upsert(cid)
    ctx.user_data.update(mode="setrules",chat_id=cid)
    await u.message.reply_text(f"📥 এখন {cid}-এর rules paste বা forward করো। /cancel দিয়ে বাতিল।")
async def rules_cmd(u,ctx):
    if not await guard(u): return
    if not ctx.args: return await u.message.reply_text("ব্যবহার: /rules <chat_id>")
    r=get(ctx.args[0]); await u.message.reply_text((rules(r) if r else "কিছু নেই।")[:10000])
async def check_cmd(u,ctx):
    if not await guard(u): return
    if not ctx.args: return await u.message.reply_text("ব্যবহার: /check <chat_id>")
    cid=ctx.args[0]
    if not get(cid) or not rules(get(cid)): return await u.message.reply_text("❌ Rules নেই। আগে /analyze বা /setrules করো।")
    ctx.user_data.update(mode="check",chat_id=cid); await u.message.reply_text("📝 এখন draft message পাঠাও।")
async def rewrite_cmd(u,ctx):
    if not await guard(u): return
    if not ctx.args: return await u.message.reply_text("ব্যবহার: /rewrite <chat_id>")
    cid=ctx.args[0]
    if not get(cid) or not rules(get(cid)): return await u.message.reply_text("❌ Rules নেই। আগে /setrules করো।")
    ctx.user_data.update(mode="rewrite",chat_id=cid); await u.message.reply_text("✏️ Draft পাঠাও।")
async def process(u,ctx,text):
    if not await guard(u): return
    mode=ctx.user_data.get("mode"); cid=ctx.user_data.get("chat_id"); ctx.user_data.clear()
    if not mode or not cid: return await u.message.reply_text("প্রথমে /check বা /rewrite বা /setrules ব্যবহার করো।")
    if mode=="setrules":
        setrules(cid,text); audit("set_rules",cid,text[:1000]); return await u.message.reply_text("✅ Rules saved.")
    rr=rules(get(cid))
    if len(text)>MAX_TEXT: return await u.message.reply_text(f"❌ সর্বোচ্চ {MAX_TEXT} characters.")
    try:
        res=await ai_json(RULE_SYSTEM,f"GROUP ID: {cid}\n\nGROUP RULES/SOURCE:\n{rr[:MAX_RULES]}\n\nPROPOSED MESSAGE:\n{text}")
        dec=str(res.get("decision","REVIEW")).upper()
        if dec not in {"APPROVE","REVIEW","BLOCK"}: dec="REVIEW"
        mod=(res.get("modified_text") or "").strip(); reasons=res.get("reasons",[]); changes=res.get("changes",[])
        c=db(); cur=c.execute("INSERT INTO drafts(owner_id,chat_id,original_text,decision,modified_text,reasons,created_at) VALUES(?,?,?,?,?,?,?)",
          (OWNER_ID,str(cid),text,dec,mod,json.dumps(reasons,ensure_ascii=False),now())); did=cur.lastrowid; c.commit(); c.close()
        icon={"APPROVE":"✅","REVIEW":"⚠️","BLOCK":"❌"}[dec]
        out=f"{icon} <b>{dec}</b>\nDraft ID: <code>{did}</code>\n\n<b>Reasons:</b>\n"+("\n".join("• "+str(x) for x in reasons) or "• None")
        out+="\n\n<b>Changes:</b>\n"+("\n".join("• "+str(x) for x in changes) or "• None")
        if mod: out+="\n\n✏️ <b>Suggested version:</b>\n"+mod
        kb=InlineKeyboardMarkup([[InlineKeyboardButton("📋 Copy suggested text",callback_data=f"copy:{did}")],
                                 [InlineKeyboardButton("🗑 Delete result",callback_data=f"delete:{did}")]])
        await u.message.reply_text(out,parse_mode="HTML",reply_markup=kb)
        audit("check" if mode=="check" else "rewrite",cid,dec)
    except Exception as e:
        await u.message.reply_text("❌ AI check failed: "+str(e))
async def text_msg(u,ctx):
    if u.effective_message and u.effective_message.text: await process(u,ctx,u.effective_message.text)
async def callback(u,ctx):
    q=u.callback_query
    if q.from_user.id!=OWNER_ID: return await q.answer("Not allowed",show_alert=True)
    await q.answer(); a,i=q.data.split(":")
    if a=="delete": return await q.edit_message_text("🗑 Result removed.")
    c=db(); r=c.execute("SELECT modified_text FROM drafts WHERE id=? AND owner_id=?",(int(i),OWNER_ID)).fetchone(); c.close()
    await q.message.reply_text(r["modified_text"] if r and r["modified_text"] else "Suggested rewrite নেই।")
async def chats(u,ctx):
    if not await guard(u): return
    c=db(); rs=c.execute("SELECT chat_id,title,chat_type FROM chats ORDER BY updated_at DESC").fetchall(); c.close()
    await u.message.reply_text("\n".join(f"• {r['chat_id']} | {r['title'] or '-'} | {r['chat_type'] or '-'}" for r in rs)[:10000] or "No saved chats.")
async def status(u,ctx):
    if not await guard(u): return
    c=db(); a=c.execute("SELECT COUNT(*) n FROM chats").fetchone()["n"]; d=c.execute("SELECT COUNT(*) n FROM drafts").fetchone()["n"]; c.close()
    await u.message.reply_text(f"🤖 Compliance Bot\nAI: {'ON' if AI_ENABLED and AI_API_KEY else 'OFF'}\nSaved chats: {a}\nChecked drafts: {d}\nAuto-posting: OFF")
async def logs(u,ctx):
    if not await guard(u): return
    n=min(max(int(ctx.args[0]) if ctx.args and ctx.args[0].isdigit() else 20,1),100)
    c=db(); rs=c.execute("SELECT action,chat_id,details,created_at FROM audit ORDER BY id DESC LIMIT ?",(n,)).fetchall(); c.close()
    await u.message.reply_text("\n".join(f"{r['created_at']} | {r['action']} | {r['chat_id'] or '-'} | {r['details'][:100]}" for r in rs)[:10000] or "No logs.")
async def cancel(u,ctx):
    if not await guard(u): return
    ctx.user_data.clear(); await u.message.reply_text("✅ Cancelled.")
def main():
    init_db(); app=Application.builder().token(BOT_TOKEN).build()
    for name,fn in [("start",start),("help",help_cmd),("analyze",analyze),("setrules",setrules_cmd),
                    ("rules",rules_cmd),("check",check_cmd),("rewrite",rewrite_cmd),("chats",chats),
                    ("status",status),("logs",logs),("cancel",cancel)]:
        app.add_handler(CommandHandler(name,fn))
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,text_msg))
    app.run_polling(allowed_updates=Update.ALL_TYPES)
if __name__=="__main__": main()
