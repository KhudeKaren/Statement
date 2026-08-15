"""
ربات بیانیه‌رسان روبیکا — نسخه‌ی ساده‌شده
========================================
- هر ۵ دقیقه یک بیانیه
- فیلتر کلمات ممنوعه = ۶ ساعت محرومیت
- مدیریت کامل کاربران توسط ادمین
"""

from __future__ import annotations
import asyncio, logging, os, sqlite3, time
from datetime import datetime, timedelta
from pathlib import Path
import aiosqlite
from rubpy.bot import BotClient, filters

# ========== تنظیمات ==========
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN ست نشده.")
DB_PATH = Path(__file__).resolve().parent / "announcement_bot.db"
MIN_WORDS = 30
MAX_WARNINGS = 5
COOLDOWN_MINUTES = 5
MUTE_HOURS = 6

FORBIDDEN_WORDS = ["خامنه ای", "خامنه‌ای", "مرگ بر خامنه ای", "کسننه", "مادرجنده", "کیر تو خامنه ای", "مادرکونی", "خایه مال", "ننه جنده"]

app = BotClient(BOT_TOKEN)
admin_state = {}

# ========== دیتابیس ==========
async def db_execute(query, params=()):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(query, params)
        await db.commit()
        return cur

async def db_fetchone(query, params=()):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(query, params)
        return await cur.fetchone()

async def db_fetchall(query, params=()):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(query, params)
        return await cur.fetchall()

async def init_db():
    await db_execute("""
        CREATE TABLE IF NOT EXISTS users (
            sender_id TEXT PRIMARY KEY, country TEXT NOT NULL,
            warnings INTEGER DEFAULT 0, banned INTEGER DEFAULT 0,
            last_statement_at TEXT, muted_until TEXT,
            registered_at TEXT DEFAULT (datetime('now'))
        )
    """)
    await db_execute("CREATE TABLE IF NOT EXISTS admins (sender_id TEXT PRIMARY KEY, added_at TEXT DEFAULT (datetime('now')))")
    await db_execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    await db_execute("CREATE TABLE IF NOT EXISTS violations (id INTEGER PRIMARY KEY AUTOINCREMENT, sender_id TEXT, text TEXT, created_at TEXT DEFAULT (datetime('now')))")
    print("✅ دیتابیس آماده شد")

# ========== توابع کمکی ==========
def is_admin(sid): return bool(db_fetchone("SELECT 1 FROM admins WHERE sender_id=?", (str(sid),)))
async def any_admin(): return (await db_fetchone("SELECT 1 FROM admins")) is not None
async def get_channel(): return (await db_fetchone("SELECT value FROM settings WHERE key='channel_id'"))[0] if await db_fetchone("SELECT value FROM settings WHERE key='channel_id'") else None
async def get_user(sid): return await db_fetchone("SELECT * FROM users WHERE sender_id=?", (str(sid),))
async def add_admin(sid): await db_execute("INSERT OR IGNORE INTO admins (sender_id) VALUES (?)", (str(sid),))
async def register_user(sid, country): await db_execute("INSERT INTO users (sender_id, country) VALUES (?, ?) ON CONFLICT(sender_id) DO UPDATE SET country=excluded.country", (str(sid), country))
async def remove_user(sid): return (await db_execute("DELETE FROM users WHERE sender_id=?", (str(sid),))).rowcount > 0
async def set_ban(sid, banned): await db_execute("UPDATE users SET banned=? WHERE sender_id=?", (1 if banned else 0, str(sid)))
async def add_warning(sid):
    await db_execute("UPDATE users SET warnings = warnings + 1 WHERE sender_id=?", (str(sid),))
    user = await get_user(sid)
    if user and user["warnings"] >= MAX_WARNINGS:
        await set_ban(sid, True)
        return True, user["warnings"]
    return False, user["warnings"] if user else 0
async def set_mute(sid, hours):
    muted_until = (datetime.now() + timedelta(hours=hours)).isoformat()
    await db_execute("UPDATE users SET muted_until=? WHERE sender_id=?", (muted_until, str(sid)))
async def unmute_user(sid): await db_execute("UPDATE users SET muted_until=NULL WHERE sender_id=?", (str(sid),))
async def is_muted(sid):
    user = await get_user(sid)
    if user and user["muted_until"]:
        if datetime.fromisoformat(user["muted_until"]) > datetime.now():
            return True, user["muted_until"]
    return False, None
async def can_send(sid):
    user = await get_user(sid)
    if user and user["last_statement_at"]:
        last = datetime.fromisoformat(user["last_statement_at"])
        if (datetime.now() - last) < timedelta(minutes=COOLDOWN_MINUTES):
            rem = COOLDOWN_MINUTES - int((datetime.now() - last).seconds / 60)
            return False, f"⏳ {rem} دقیقه دیگه"
    return True, ""
async def touch_statement(sid): await db_execute("UPDATE users SET last_statement_at=datetime('now') WHERE sender_id=?", (str(sid),))
async def log_violation(sid, text): await db_execute("INSERT INTO violations (sender_id, text) VALUES (?, ?)", (str(sid), text[:500]))
def contains_forbidden(text): return [w for w in FORBIDDEN_WORDS if w in text]
def word_count(text): return len([w for w in text.split() if w.strip()])
def safe_sid(msg): return str(getattr(msg, "sender_id", "")) if getattr(msg, "sender_id", None) else ""
def format_statement(text, country): return f"【 𝗥𝗲𝘂𝘁𝗲𝗿𝘀 | 𝗢𝗳𝗳𝗶𝗰𝗶𝗮𝗹 𝗡𝗲𝘄𝘀 】\n\n📢- بیانیه رسمی مقام کشور {country}\n\n{text}"
ADMIN_MENU = (
    "🇺🇳 پنل ادمین\n"
    "1. ➕ ثبت کاربر\n2. ➖ حذف کاربر\n3. ⚠️ اخطار\n"
    "4. 🚫 بن\n5. ✅ آنبن\n6. 📋 لیست کاربران\n"
    "7. 👤 افزودن ادمین\n8. 🔇 رفع محدودیت\n0. بستن"
)

# ========== هندلرها ==========
@app.middleware()
async def log_updates(bot, update, call_next):
    nm = getattr(update, "new_message", None)
    if nm:
        print(f"📩 از {getattr(nm, 'sender_id', '???')}: {(getattr(nm, 'text', '') or '')[:50]}")
    await call_next()

@app.on_update(filters.private & filters.file)
async def on_photo(client, message):
    msg = getattr(message, "new_message", None)
    if not msg: return
    sid = safe_sid(msg); chat_id = message.chat_id
    caption = (getattr(msg, "text", None) or "").strip()
    file_obj = getattr(msg, "file", None)
    if not file_obj: return await client.send_message(chat_id, "❌ فایلی نیست")
    
    user = await get_user(sid)
    if not user: return await client.send_message(chat_id, f"❌ ثبت‌نام نشدی. شناسه: `{sid}`")
    if user["banned"]: return await client.send_message(chat_id, "🚫 بن شدی")
    
    muted, until = await is_muted(sid)
    if muted: return await client.send_message(chat_id, f"🔇 تا {until} محرومی")
    
    ok, msg = await can_send(sid)
    if not ok: return await client.send_message(chat_id, msg)
    if not caption: return await client.send_message(chat_id, "❌ کپشن بنویس")
    if word_count(caption) < MIN_WORDS: return await client.send_message(chat_id, f"❌ حداقل {MIN_WORDS} کلمه")
    
    forbidden = contains_forbidden(caption)
    if forbidden:
        await set_mute(sid, MUTE_HOURS)
        await log_violation(sid, caption)
        admins = await db_fetchall("SELECT sender_id FROM admins")
        for a in admins:
            try: await client.send_message(a["sender_id"], f"🚨 بیانیه مشکل‌دار از {sid}\n🚫 {', '.join(forbidden)}\n📝 {caption[:300]}")
            except: pass
        return await client.send_message(chat_id, f"🚫 کلمات ممنوعه: {', '.join(forbidden)}\n🔇 {MUTE_HOURS} ساعت محرومی")
    
    channel = await get_channel()
    if not channel: return await client.send_message(chat_id, "❌ کانال ثبت نشده")
    
    file_id = getattr(file_obj, "file_id", None)
    if not file_id: return await client.send_message(chat_id, "❌ شناسه فایل نامعتبر")
    
    tmp_path = f"/tmp/announce_{sid}_{int(time.time())}.jpg"
    saved = await client.download_file(file_id, save_as=tmp_path)
    if saved:
        await client.send_file(channel, saved, caption=format_statement(caption, user["country"]))
        await touch_statement(sid)
        await client.send_message(chat_id, "✅ بیانیه منتشر شد")

@app.on_update(filters.text)
async def on_text(client, message):
    msg = getattr(message, "new_message", None)
    if not msg: return
    sid = safe_sid(msg); chat_id = message.chat_id
    text = (getattr(msg, "text", None) or "").strip()
    if getattr(msg, "file", None): return
    
    # ثبت کانال
    if text == "ثبت کانال":
        existing = await get_channel()
        if existing and not await is_admin(sid):
            return await client.send_message(chat_id, "❌ کانال ثبت شده")
        await db_execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('channel_id', ?)", (chat_id,))
        return await client.send_message(chat_id, f"✅ کانال ثبت شد: {chat_id}")
    
    # ثبت ادمین
    if text == "ثبت ادمین":
        if await any_admin():
            return await client.send_message(chat_id, "❌ ادمین داره" if not await is_admin(sid) else "شما ادمینی")
        await add_admin(sid)
        return await client.send_message(chat_id, "✅ اولین ادمین شدی\nبرای منو: مدیریت")
    
    # منو
    if text in ("مدیریت", "ادمین", "/admin") and await is_admin(sid):
        admin_state.pop(sid, None)
        return await client.send_message(chat_id, ADMIN_MENU)
    
    # عدد منو
    if sid not in admin_state and await is_admin(sid) and text in "123456780":
        await handle_menu(client, chat_id, sid, text)
        return
    
    # فلوی ادمین
    if sid in admin_state:
        await handle_flow(client, chat_id, sid, text)
        return
    
    # کاربر عادی
    user = await get_user(sid)
    if not user:
        return await client.send_message(chat_id, f"❌ ثبت‌نام نشدی.\nشناسه: `{sid}`")
    await client.send_message(chat_id, f"📝 عکس با کپشن بفرست. حداقل {MIN_WORDS} کلمه.")

# ========== منوی ادمین ==========
async def handle_menu(client, chat_id, sid, choice):
    if choice == "0": admin_state.pop(sid, None); return await client.send_message(chat_id, "✅ منو بسته شد")
    steps = {"1": "register", "2": "remove", "3": "warn", "4": "ban", "5": "unban", "7": "add_admin", "8": "unmute"}
    if choice in steps:
        admin_state[sid] = {"step": steps[choice]}
        msgs = {"register": "📝 user_id کشور:", "remove": "🗑️ user_id:", "warn": "⚠️ user_id:", "ban": "🚫 user_id:", "unban": "✅ user_id:", "add_admin": "👤 user_id:", "unmute": "🔇 user_id:"}
        await client.send_message(chat_id, msgs[steps[choice]])
    elif choice == "6":
        users = await db_fetchall("SELECT * FROM users ORDER BY registered_at DESC")
        if not users: return await client.send_message(chat_id, "📋 هیچ کاربری نیست")
        msg = "📋 لیست کاربران:\n"
        for u in users:
            muted, _ = await is_muted(u["sender_id"])
            msg += f"🆔 {u['sender_id']} → {u['country']} | اخطار: {u['warnings']}/{MAX_WARNINGS} | بن: {'🚫' if u['banned'] else '✅'} | محرومیت: {'🔇' if muted else '✅'}\n"
        await client.send_message(chat_id, msg)

# ========== فلوی ادمین ==========
async def handle_flow(client, chat_id, sid, text):
    state = admin_state.get(sid); step = state.get("step") if state else None
    if not state: return
    
    if step == "register":
        parts = text.split(maxsplit=1)
        if len(parts) < 2: return await client.send_message(chat_id, "❌ فرمت: user_id کشور")
        await register_user(parts[0], parts[1])
        await client.send_message(chat_id, f"✅ {parts[0]} ثبت شد")
    elif step == "remove":
        if await remove_user(text.strip()): await client.send_message(chat_id, f"✅ {text.strip()} حذف شد")
        else: await client.send_message(chat_id, "❌ پیدا نشد")
    elif step == "warn":
        user = await get_user(text.strip())
        if not user: return await client.send_message(chat_id, "❌ پیدا نشد")
        banned, warns = await add_warning(text.strip())
        await client.send_message(chat_id, f"⚠️ اخطار {warns}/{MAX_WARNINGS}" + (" 🚫 بن شد" if banned else ""))
    elif step == "ban":
        await set_ban(text.strip(), True); await client.send_message(chat_id, f"🚫 {text.strip()} بن شد")
    elif step == "unban":
        await set_ban(text.strip(), False); await client.send_message(chat_id, f"✅ {text.strip()} آنبن شد")
    elif step == "add_admin":
        await add_admin(text.strip()); await client.send_message(chat_id, f"👤 {text.strip()} ادمین شد")
    elif step == "unmute":
        await unmute_user(text.strip()); await client.send_message(chat_id, f"🔇 محدودیت {text.strip()} برداشته شد")
    admin_state.pop(sid, None)

# ========== اجرا ==========
async def main():
    await init_db()
    print("🚀 ربات بیانیه روبیکا روشن شد!")
    print(f"⏳ هر {COOLDOWN_MINUTES} دقیقه یک بیانیه | 🔇 کلمات ممنوعه = {MUTE_HOURS} ساعت محرومیت")
    await app.run()

if __name__ == "__main__":
    asyncio.run(main())
