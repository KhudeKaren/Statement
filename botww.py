"""
بات بیانیه‌رسان روبیکا — نسخه‌ی بروز شده (v3)
==============================================

قابلیت‌های جدید:
  - هر کاربر فقط هر ۵ دقیقه یک بار می‌تونه بیانیه بفرسته
  - فیلتر کلمات ممنوعه (خامنه‌ای، مادرجنده، ...)
  - در صورت استفاده از کلمات ممنوعه: ۶ ساعت محرومیت + ارسال به ادمین
  - ادمین می‌تونه محدودیت رو برداره
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

import aiosqlite
from rubpy.bot import BotClient, filters

# ───────────────────────── لاگینگ ─────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("announcement_bot")

# ───────────────────────── تنظیمات ─────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN ست نشده.")

DB_PATH = Path(__file__).resolve().parent / "announcement_bot.db"

MIN_WORDS = 30
MAX_WARNINGS = 5
COOLDOWN_MINUTES = 5
MUTE_HOURS = 6

# ========== لیست کلمات ممنوعه ==========
FORBIDDEN_WORDS = [
    "خامنه ای", "خامنه‌ای", "خامنه ای", "خامنه‌ای",
    "مرگ بر خامنه ای", "مرگ بر خامنه‌ای",
    "کسننه", "کسننت", "مادرجنده", "مادر جنده",
    "کیر تو خامنه ای", "کیر تو خامنه‌ای",
    "کیر تو ننه خامنه ای", "کیر تو ننه خامنه‌ای",
    "مادرکونی", "خایه مال", "ننه جنده", "ننه جنده"
]

app = BotClient(BOT_TOKEN)
admin_state: dict[str, dict] = {}

# ───────────────────────── دیتابیس ─────────────────────────
async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                sender_id TEXT PRIMARY KEY,
                country TEXT NOT NULL,
                warnings INTEGER NOT NULL DEFAULT 0,
                banned INTEGER NOT NULL DEFAULT 0,
                last_statement_at TEXT,
                muted_until TEXT,
                registered_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS admins (
                sender_id TEXT PRIMARY KEY,
                added_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS violations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender_id TEXT,
                text TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        await db.commit()
    log.info("Database ready: %s", DB_PATH)

# ───────────────────────── توابع دیتابیس ─────────────────────────
async def any_admin_exists() -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT 1 FROM admins LIMIT 1")
        return (await cur.fetchone()) is not None

async def is_admin(sender_id: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT 1 FROM admins WHERE sender_id=?", (str(sender_id),))
        return (await cur.fetchone()) is not None

async def add_admin(sender_id: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO admins (sender_id) VALUES (?)", (str(sender_id),))
        await db.commit()

async def get_setting(key: str) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = await cur.fetchone()
        return row[0] if row else None

async def set_setting(key: str, value: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        await db.commit()

async def get_channel_id() -> str | None:
    return await get_setting("channel_id")

async def set_channel_id(chat_id: str) -> None:
    await set_setting("channel_id", str(chat_id))

async def get_user(sender_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM users WHERE sender_id=?", (str(sender_id),))
        return await cur.fetchone()

async def register_user(sender_id: str, country: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO users (sender_id, country) VALUES (?, ?) ON CONFLICT(sender_id) DO UPDATE SET country=excluded.country", (str(sender_id), country))
        await db.commit()

async def remove_user(sender_id: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("DELETE FROM users WHERE sender_id=?", (str(sender_id),))
        await db.commit()
        return cur.rowcount > 0

async def add_warning(sender_id: str) -> tuple[bool, int]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("UPDATE users SET warnings = warnings + 1 WHERE sender_id=? RETURNING warnings", (str(sender_id),))
        row = await cur.fetchone()
        if not row:
            await db.commit()
            return False, 0
        warnings = row["warnings"]
        newly_banned = False
        if warnings >= MAX_WARNINGS:
            await db.execute("UPDATE users SET banned=1 WHERE sender_id=?", (str(sender_id),))
            newly_banned = True
        await db.commit()
        return newly_banned, warnings

async def set_ban(sender_id: str, banned: bool) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("UPDATE users SET banned=? WHERE sender_id=?", (1 if banned else 0, str(sender_id)))
        await db.commit()
        return cur.rowcount > 0

async def set_mute(sender_id: str, hours: int) -> None:
    muted_until = (datetime.now() + timedelta(hours=hours)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET muted_until=? WHERE sender_id=?", (muted_until, str(sender_id)))
        await db.commit()

async def unmute_user(sender_id: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET muted_until=NULL WHERE sender_id=?", (str(sender_id),))
        await db.commit()

async def is_muted(sender_id: str) -> tuple[bool, str | None]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT muted_until FROM users WHERE sender_id=?", (str(sender_id),))
        row = await cur.fetchone()
        if row and row["muted_until"]:
            muted_until = datetime.fromisoformat(row["muted_until"])
            if muted_until > datetime.now():
                return True, row["muted_until"]
        return False, None

async def can_send_statement(sender_id: str) -> tuple[bool, str]:
    """بررسی محدودیت ۵ دقیقه‌ای"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT last_statement_at FROM users WHERE sender_id=?", (str(sender_id),))
        row = await cur.fetchone()
        if row and row["last_statement_at"]:
            last_time = datetime.fromisoformat(row["last_statement_at"])
            if (datetime.now() - last_time) < timedelta(minutes=COOLDOWN_MINUTES):
                remaining = COOLDOWN_MINUTES - int((datetime.now() - last_time).seconds / 60)
                return False, f"⏳ صبر کن! هر {COOLDOWN_MINUTES} دقیقه فقط یک بیانیه می‌تونی بفرستی. {remaining} دقیقه دیگه."
    return True, ""

async def touch_last_statement(sender_id: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET last_statement_at=datetime('now') WHERE sender_id=?", (str(sender_id),))
        await db.commit()

async def list_users() -> list[sqlite3.Row]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM users ORDER BY registered_at DESC")
        return await cur.fetchall()

async def log_violation(sender_id: str, text: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO violations (sender_id, text) VALUES (?, ?)", (str(sender_id), text[:500]))
        await db.commit()

# ───────────────────────── ابزارهای کمکی ─────────────────────────
def word_count(text: str) -> int:
    return len([w for w in text.split() if w.strip()])

def contains_forbidden_words(text: str) -> list[str]:
    found = []
    for word in FORBIDDEN_WORDS:
        if word in text:
            found.append(word)
    return found

def format_statement(text: str, country: str) -> str:
    return f"【 𝗥𝗲𝘂𝘁𝗲𝗿𝘀 | 𝗢𝗳𝗳𝗶𝗰𝗶𝗮𝗹 𝗡𝗲𝘄𝘀 】\n\n📢- بیانیه رسمی مقام کشور {country}\n\n{text}"

def safe_sender_id(msg) -> str:
    sid = getattr(msg, "sender_id", None)
    return str(sid) if sid is not None else ""

ADMIN_MENU_TEXT = (
    "🇺🇳 پنل ادمین — بیانیه‌رسان\n\n"
    "1. ➕ ثبت کاربر جدید\n"
    "2. ➖ حذف کاربر\n"
    "3. ⚠️ اخطار به کاربر\n"
    "4. 🚫 بن دستی\n"
    "5. ✅ آنبن\n"
    "6. 📋 لیست کاربران\n"
    "7. 👤 افزودن ادمین جدید\n"
    "8. 🔇 رفع محدودیت ۶ ساعته\n"
    "0. بستن منو\n\n"
    "🔢 عدد مورد نظر رو بفرست."
)

# ───────────────────────── middleware ─────────────────────────
@app.middleware()
async def log_updates(bot, update, call_next):
    try:
        nm = getattr(update, "new_message", None)
        if nm:
            text = (getattr(nm, "text", None) or "")[:80]
            log.info("UPDATE sender=%s text=%r", getattr(nm, "sender_id", None), text)
    except Exception:
        pass
    await call_next()

# ───────────────────────── هندلر عکس ─────────────────────────
@app.on_update(filters.private & filters.file)
async def on_photo(client: BotClient, message):
    try:
        msg = getattr(message, "new_message", None)
        if not msg:
            await client.send_message(message.chat_id, "❌ فایلی دریافت نشد.")
            return

        sender_id = safe_sender_id(msg)
        chat_id = message.chat_id
        caption = (getattr(msg, "text", None) or "").strip()
        file_obj = getattr(msg, "file", None)

        if file_obj is None:
            await client.send_message(chat_id, "❌ فایلی دریافت نشد.")
            return

        user = await get_user(sender_id)
        if not user:
            await client.send_message(chat_id, f"❌ شما ثبت‌نام نشدید.\nشناسه: `{sender_id}`")
            return
        
        if user["banned"]:
            await client.send_message(chat_id, "🚫 شما بن شدید.")
            return

        # بررسی محدودیت ۶ ساعته (کلمات ممنوعه)
        muted, until = await is_muted(sender_id)
        if muted:
            await client.send_message(chat_id, f"🔇 شما تا {until} محروم هستید.")
            return

        # بررسی ۵ دقیقه
        can_send, msg = await can_send_statement(sender_id)
        if not can_send:
            await client.send_message(chat_id, msg)
            return

        if not caption:
            await client.send_message(chat_id, "❌ متن بیانیه رو در کپشن عکس بنویسید.")
            return

        if word_count(caption) < MIN_WORDS:
            await client.send_message(chat_id, f"❌ بیانیه باید حداقل {MIN_WORDS} کلمه داشته باشه.")
            return

        # ========== بررسی کلمات ممنوعه ==========
        forbidden = contains_forbidden_words(caption)
        if forbidden:
            # ۶ ساعت محرومیت
            await set_mute(sender_id, MUTE_HOURS)
            await log_violation(sender_id, caption)
            
            # ارسال به ادمین
            admins = []
            async with aiosqlite.connect(DB_PATH) as db:
                cur = await db.execute("SELECT sender_id FROM admins")
                admins = [row[0] for row in await cur.fetchall()]
            
            for admin_id in admins:
                try:
                    await client.send_message(
                        admin_id,
                        f"🚨 **بیانیه مشکل‌دار**\n\n"
                        f"👤 کاربر: `{sender_id}`\n"
                        f"🚫 کلمات ممنوعه: {', '.join(forbidden)}\n"
                        f"🔇 محروم شد تا ۶ ساعت\n\n"
                        f"📝 متن:\n{caption[:500]}"
                    )
                except Exception as e:
                    log.error(f"ارسال به ادمین {admin_id} ناموفق: {e}")
            
            await client.send_message(
                chat_id,
                f"🚫 بیانیه شما شامل کلمات ممنوعه است:\n{', '.join(forbidden)}\n\n"
                f"🔇 شما به مدت ۶ ساعت از ارسال بیانیه محروم شدید."
            )
            return

        final_text = format_statement(caption, user["country"])
        channel_id = await get_channel_id()
        if not channel_id:
            await client.send_message(chat_id, "❌ کانالی برای انتشار ثبت نشده.")
            return

        file_id = getattr(file_obj, "file_id", None)
        if not file_id:
            await client.send_message(chat_id, "❌ شناسه فایل نامعتبر است.")
            return

        ok = await _send_to_channel(client, channel_id, file_id, final_text)
        if not ok:
            await client.send_message(chat_id, "❌ ارسال به کانال ناموفق بود.")
            return

        await touch_last_statement(sender_id)
        await client.send_message(chat_id, "✅ بیانیه با موفقیت در کانال منتشر شد.")
        
    except Exception as e:
        log.exception("on_photo error")
        await client.send_message(message.chat_id, "⚠️ خطای داخلی رخ داد.")

# ───────────────────────── هندلر متنی ─────────────────────────
@app.on_update(filters.text)
async def on_text_any(client: BotClient, message):
    try:
        msg = getattr(message, "new_message", None)
        if not msg:
            return

        if getattr(msg, "file", None):
            return

        sender_id = safe_sender_id(msg)
        text = (getattr(msg, "text", None) or "").strip()
        text_norm = text.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
        chat_id = message.chat_id
        is_private = str(chat_id).startswith("b0")

        # ثبت کانال
        if text == "ثبت کانال":
            existing = await get_channel_id()
            admin_ok = await is_admin(sender_id)
            if existing is not None and not admin_ok:
                await client.send_message(chat_id, "❌ کانال از قبل ثبت شده.")
                return
            await set_channel_id(chat_id)
            await client.send_message(chat_id, f"✅ کانال ثبت شد. شناسه: {chat_id}")
            return

        if not is_private:
            return

        # ثبت ادمین
        if text == "ثبت ادمین":
            if await any_admin_exists():
                if await is_admin(sender_id):
                    await client.send_message(chat_id, "شما از قبل ادمین هستید.")
                else:
                    await client.send_message(chat_id, "❌ سیستم از قبل ادمین داره.")
                return
            await add_admin(sender_id)
            await client.send_message(chat_id, "✅ شما اولین ادمین شدید.\nبرای منو: مدیریت")
            return

        # منوی ادمین
        if text in ("مدیریت", "ادمین", "/admin") and await is_admin(sender_id):
            admin_state.pop(sender_id, None)
            await client.send_message(chat_id, ADMIN_MENU_TEXT)
            return

        # انتخاب از منو
        if sender_id not in admin_state and await is_admin(sender_id) and text_norm in {"1", "2", "3", "4", "5", "6", "7", "8", "0"}:
            await handle_admin_menu_choice(client, chat_id, sender_id, text_norm)
            return

        # فلوی ادمین
        if sender_id in admin_state:
            await handle_admin_flow(client, chat_id, sender_id, text)
            return

        # کاربر عادی
        user = await get_user(sender_id)
        if not user:
            await client.send_message(chat_id, f"❌ شما ثبت‌نام نشدید.\nشناسه: `{sender_id}`")
            return

        await client.send_message(chat_id, f"📝 برای ارسال بیانیه، یه عکس با کپشن بفرست.\nحداقل {MIN_WORDS} کلمه.")

    except Exception as e:
        log.exception("on_text_any error")
        await client.send_message(message.chat_id, "⚠️ خطای داخلی رخ داد.")

# ───────────────────────── منوی ادمین ─────────────────────────
async def handle_admin_menu_choice(client: BotClient, chat_id: str, sender_id: str, choice: str):
    if choice == "0":
        await client.send_message(chat_id, "✅ منو بسته شد.")
        return

    if choice == "1":
        admin_state[sender_id] = {"step": "register"}
        await client.send_message(chat_id, "📝 شناسه کاربر و کشور رو بفرست:\nمثال: `12345678 آلمان`")
        return

    if choice == "2":
        admin_state[sender_id] = {"step": "remove"}
        await client.send_message(chat_id, "🗑️ شناسه کاربر رو بفرست:")
        return

    if choice == "3":
        admin_state[sender_id] = {"step": "warn"}
        await client.send_message(chat_id, "⚠️ شناسه کاربر رو بفرست:")
        return

    if choice == "4":
        admin_state[sender_id] = {"step": "ban"}
        await client.send_message(chat_id, "🚫 شناسه کاربر رو بفرست:")
        return

    if choice == "5":
        admin_state[sender_id] = {"step": "unban"}
        await client.send_message(chat_id, "✅ شناسه کاربر رو بفرست:")
        return

    if choice == "6":
        users = await list_users()
        if not users:
            await client.send_message(chat_id, "📋 هیچ کاربری ثبت نشده.")
            return
        msg = "📋 **لیست کاربران:**\n\n"
        for u in users:
            muted, _ = await is_muted(u["sender_id"])
            mute_status = "🔇" if muted else "✅"
            ban_status = "🚫" if u["banned"] else "✅"
            msg += f"🆔 `{u['sender_id']}` → {u['country']}\n"
            msg += f"   ⚠️ اخطار: {u['warnings']}/{MAX_WARNINGS} | بن: {ban_status} | محرومیت: {mute_status}\n\n"
        await client.send_message(chat_id, msg)
        return

    if choice == "7":
        admin_state[sender_id] = {"step": "add_admin"}
        await client.send_message(chat_id, "👤 شناسه کاربر جدید رو بفرست تا ادمین بشه:")
        return

    if choice == "8":
        admin_state[sender_id] = {"step": "unmute"}
        await client.send_message(chat_id, "🔇 شناسه کاربر رو بفرست تا محدودیت ۶ ساعته‌اش برداشته بشه:")
        return

# ───────────────────────── فلوی ادمین ─────────────────────────
async def handle_admin_flow(client: BotClient, chat_id: str, sender_id: str, text: str):
    state = admin_state.get(sender_id)
    if not state:
        return

    step = state.get("step")

    if step == "register":
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            await client.send_message(chat_id, "❌ فرمت: `user_id country`")
            return
        uid, country = parts[0], parts[1]
        await register_user(uid, country)
        await client.send_message(chat_id, f"✅ کاربر {uid} با کشور {country} ثبت شد.")
        admin_state.pop(sender_id, None)

    elif step == "remove":
        uid = text.strip()
        if not uid:
            await client.send_message(chat_id, "❌ شناسه نامعتبر.")
            return
        if await remove_user(uid):
            await client.send_message(chat_id, f"✅ کاربر {uid} حذف شد.")
        else:
            await client.send_message(chat_id, f"❌ کاربر {uid} پیدا نشد.")
        admin_state.pop(sender_id, None)

    elif step == "warn":
        uid = text.strip()
        if not uid:
            await client.send_message(chat_id, "❌ شناسه نامعتبر.")
            return
        user = await get_user(uid)
        if not user:
            await client.send_message(chat_id, f"❌ کاربر {uid} پیدا نشد.")
                    admin_state.pop(sender_id, None)  # <--- اصلاح شد (None بود)

    elif step == "ban":
        uid = text.strip()
        if not uid:
            await client.send_message(chat_id, "❌ شناسه نامعتبر.")
            return
        if await set_ban(uid, True):
            await client.send_message(chat_id, f"🚫 کاربر {uid} بن شد.")
        else:
            await client.send_message(chat_id, f"❌ کاربر {uid} پیدا نشد.")
        admin_state.pop(sender_id, None)

    elif step == "unban":
        uid = text.strip()
        if not uid:
            await client.send_message(chat_id, "❌ شناسه نامعتبر.")
            return
        if await set_ban(uid, False):
            await client.send_message(chat_id, f"✅ کاربر {uid} آنبن شد.")
        else:
            await client.send_message(chat_id, f"❌ کاربر {uid} پیدا نشد.")
        admin_state.pop(sender_id, None)

    elif step == "add_admin":
        uid = text.strip()
        if not uid:
            await client.send_message(chat_id, "❌ شناسه نامعتبر.")
            return
        await add_admin(uid)
        await client.send_message(chat_id, f"👤 کاربر {uid} ادمین شد.")
        admin_state.pop(sender_id, None)

    elif step == "unmute":
        uid = text.strip()
        if not uid:
            await client.send_message(chat_id, "❌ شناسه نامعتبر.")
            return
        user = await get_user(uid)
        if not user:
            await client.send_message(chat_id, f"❌ کاربر {uid} پیدا نشد.")
            admin_state.pop(sender_id, None)
            return
        await unmute_user(uid)
        await client.send_message(chat_id, f"🔇 محدودیت ۶ ساعته کاربر {uid} برداشته شد.")
        admin_state.pop(sender_id, None)

# ───────────────────────── ارسال به کانال ─────────────────────────
def _writable_tmp_dir() -> Path:
    import tempfile
    d = Path(__file__).resolve().parent / "bot_tmp"
    d.mkdir(parents=True, exist_ok=True)
    return d

async def _send_to_channel(client: BotClient, channel_id: str, file_id: str, text: str) -> bool:
    tmp_dir = _writable_tmp_dir()
    safe_name = "".join(c if c.isalnum() else "_" for c in str(file_id))[-40:]
    tmp_path = str(tmp_dir / f"announce_{safe_name}.jpg")

    try:
        saved = await client.download_file(file_id, save_as=tmp_path)
        path_to_send = saved if isinstance(saved, str) else tmp_path
        if not path_to_send or not Path(path_to_send).is_file():
            log.error("Download failed")
            return False

        await client.send_file(channel_id, path_to_send, caption=text)
        return True
    except Exception as e:
        log.exception("_send_to_channel error")
        return False

# ───────────────────────── اجرا ─────────────────────────
async def main():
    await init_db()
    print("🚀 ربات بیانیه روبیکا با محدودیت‌های جدید روشن شد!")
    print(f"⏳ هر کاربر هر {COOLDOWN_MINUTES} دقیقه یک بیانیه")
    print(f"🔇 کلمات ممنوعه = ۶ ساعت محرومیت")
    await app.run()

if __name__ == "__main__":
    asyncio.run(main())
