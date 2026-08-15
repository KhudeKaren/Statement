"""
بات بیانیه‌رسان روبیکا — نسخه‌ی اصلاح‌شده (v2)
==============================================

علت اصلی مشکل «ثبت ادمین جواب نمی‌داد»:
  در rubpy.BotClient فقط **اولین** هندلری که فیلترش match شود اجرا می‌شود
  و بعد return می‌کند. هندلر debug_all بدون فیلتر همیشه اول match می‌شد
  و بقیه هندلرها (از جمله on_text) هرگز اجرا نمی‌شدند.

اصلاحات این نسخه:
  - حذف هندلر catch-all که بقیه را می‌خورد
  - لاگ از طریق middleware (قبل از dispatch، بدون مصرف آپدیت)
  - ترتیب هندلرها: خاص‌ترها اول
  - try/except + لاگ شفاف

قبل از اجرا:
    pip install rubpy aiosqlite --break-system-packages
    export BOT_TOKEN="توکن_واقعی_بات"
    python3 announcement_bot.py

    بعدش:
      1) از پیوی بات، «ثبت ادمین» بفرست → ادمین اول می‌شی.
      2) بات رو عضوِ کانال مقصد کن (با دسترسی ارسال پیام/پست).
      3) توی خودِ اون کانال، «ثبت کانال» بفرست (از حساب ادمین).
      4) از منوی ادمین («مدیریت» یا «ادمین») کاربرها رو ثبت کن.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time
from pathlib import Path

import aiosqlite
from rubpy.bot import BotClient, filters

# زمان استارت بات — پیام‌های قدیمی‌تر از این پاسخ داده نمی‌شوند
BOT_STARTED_AT: float = 0.0

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
    raise SystemExit(
        "BOT_TOKEN ست نشده.\n"
        "مثال:\n"
        '  export BOT_TOKEN="توکن_بات"\n'
        "  python3 announcement_bot.py"
    )

DB_PATH = Path(__file__).resolve().parent / "announcement_bot.db"

DEFAULT_PREFIX = "【 𝗥𝗲𝘂𝘁𝗲𝗿𝘀 | 𝗢𝗳𝗳𝗶𝗰𝗶𝗮𝗹 𝗡𝗲𝘄𝘀 】"
MIN_WORDS = 30
MAX_WARNINGS = 5

app = BotClient(BOT_TOKEN)

# state موقتِ چندمرحله‌ایِ منوی ادمین — فقط در حافظه
admin_state: dict[str, dict] = {}


# ───────────────────────── دیتابیس ─────────────────────────

async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                sender_id TEXT PRIMARY KEY,
                country TEXT NOT NULL,
                warnings INTEGER NOT NULL DEFAULT 0,
                banned INTEGER NOT NULL DEFAULT 0,
                last_statement_at TEXT,
                registered_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS admins (
                sender_id TEXT PRIMARY KEY,
                added_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )
        await db.commit()
    log.info("Database ready: %s", DB_PATH)


async def any_admin_exists() -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT 1 FROM admins LIMIT 1")
        return (await cur.fetchone()) is not None


async def is_admin(sender_id: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT 1 FROM admins WHERE sender_id=?", (str(sender_id),)
        )
        return (await cur.fetchone()) is not None


async def add_admin(sender_id: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO admins (sender_id) VALUES (?)",
            (str(sender_id),),
        )
        await db.commit()
    log.info("Admin added: %s", sender_id)


async def get_setting(key: str) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = await cur.fetchone()
        return row[0] if row else None


async def set_setting(key: str, value: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (key, value),
        )
        await db.commit()


async def get_channel_id() -> str | None:
    return await get_setting("channel_id")


async def set_channel_id(chat_id: str) -> None:
    await set_setting("channel_id", str(chat_id))
    log.info("Channel registered: %s", chat_id)


async def get_user(sender_id: str) -> sqlite3.Row | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM users WHERE sender_id=?", (str(sender_id),)
        )
        return await cur.fetchone()


async def register_user(sender_id: str, country: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO users (sender_id, country) VALUES (?, ?)
            ON CONFLICT(sender_id) DO UPDATE SET country=excluded.country
            """,
            (str(sender_id), country),
        )
        await db.commit()
    log.info("User registered: %s / %s", sender_id, country)


async def remove_user(sender_id: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM users WHERE sender_id=?", (str(sender_id),)
        )
        await db.commit()
        return cur.rowcount > 0


async def add_warning(sender_id: str) -> tuple[bool, int]:
    """(آیا حالا بن شد؟, تعداد اخطار فعلی)"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "UPDATE users SET warnings = warnings + 1 WHERE sender_id=? RETURNING warnings",
            (str(sender_id),),
        )
        row = await cur.fetchone()
        if not row:
            await db.commit()
            return False, 0
        warnings = row["warnings"]
        newly_banned = False
        if warnings >= MAX_WARNINGS:
            await db.execute(
                "UPDATE users SET banned=1 WHERE sender_id=?", (str(sender_id),)
            )
            newly_banned = True
        await db.commit()
        return newly_banned, warnings


async def set_ban(sender_id: str, banned: bool) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "UPDATE users SET banned=? WHERE sender_id=?",
            (1 if banned else 0, str(sender_id)),
        )
        await db.commit()
        return cur.rowcount > 0


async def touch_last_statement(sender_id: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET last_statement_at=datetime('now') WHERE sender_id=?",
            (str(sender_id),),
        )
        await db.commit()


async def list_users() -> list[sqlite3.Row]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM users ORDER BY registered_at DESC")
        return await cur.fetchall()


# ───────────────────────── ابزارهای کمکی ─────────────────────────

def word_count(text: str) -> int:
    return len([w for w in text.split() if w.strip()])


def format_statement(text: str, country: str) -> str:
    return (
        f"{DEFAULT_PREFIX}\n\n"
        f"📢- بیانیه رسمی مقام کشور {country}\n\n"
        f"{text}"
    )


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
    "0. بستن منو\n\n"
    "🔢 عدد مورد نظر رو بفرست."
)


# ───────────────────────── نادیده گرفتن پیام‌های قبلی (قبل از استارت بات) ─────────────────────────

@app.middleware()
async def ignore_old_messages(bot, update, call_next):
    """پیام‌هایی که قبل از روشن شدن بات ارسال شده‌اند را پردازش نکن."""
    if BOT_STARTED_AT <= 0:
        await call_next()
        return
    nm = getattr(update, "new_message", None) or getattr(update, "updated_message", None)
    if nm is not None:
        raw = getattr(nm, "time", None)
        if raw is not None:
            try:
                msg_ts = float(raw)
                if msg_ts > 1e12:  # میلی‌ثانیه
                    msg_ts /= 1000.0
                # ۲ ثانیه حاشیه برای اختلاف ساعت
                if msg_ts < BOT_STARTED_AT - 2:
                    log.info("skip old message (time=%s)", raw)
                    return  # بدون call_next → هیچ پاسخی داده نمی‌شود
            except (TypeError, ValueError):
                pass
    await call_next()


# ───────────────────────── middleware لاگ (آپدیت را مصرف نمی‌کند) ─────────────────────────

@app.middleware()
async def log_updates(bot, update, call_next):
    try:
        chat_id = getattr(update, "chat_id", None)
        utype = getattr(update, "type", None)
        nm = getattr(update, "new_message", None)
        text_preview = None
        sender = None
        has_file = False
        if nm:
            text_preview = (getattr(nm, "text", None) or "")[:80]
            sender = getattr(nm, "sender_id", None)
            has_file = bool(getattr(nm, "file", None))
        log.info(
            "UPDATE chat=%s type=%s sender=%s file=%s text=%r",
            chat_id,
            utype,
            sender,
            has_file,
            text_preview,
        )
    except Exception:
        log.exception("log middleware failed")
    await call_next()


# مهم: در rubpy فقط **اولین** هندلرِ match‌شده اجرا می‌شود.
# پس هندلر فایل باید قبل از هندلر متن ثبت شود تا عکس+کپشن
# به‌اشتباه توسط filters.text بلعیده نشود.

# ───────────────────────── هندلر عکس / فایل (پیوی) — اول ثبت می‌شود ─────────────────────────

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

        log.info(
            "on_photo | sender=%s caption_len=%s has_file=%s",
            sender_id,
            len(caption),
            bool(file_obj),
        )

        if file_obj is None:
            await client.send_message(chat_id, "❌ فایلی دریافت نشد.")
            return

        user = await get_user(sender_id)
        if not user:
            await client.send_message(
                chat_id,
                "❌ شما هنوز ثبت‌نام نشدید.\n\n"
                f"شناسه‌ی شما: `{sender_id}`\n\n"
                "این شناسه رو برای مدیر بفرستید تا ثبت‌نامتون انجام بشه.",
            )
            return
        if user["banned"]:
            await client.send_message(
                chat_id, "🚫 شما بن شدید و نمی‌تونید بیانیه بفرستید."
            )
            return
        if not caption:
            await client.send_message(
                chat_id, "❌ باید متنِ بیانیه رو در کپشنِ عکس بنویسید."
            )
            return
        if word_count(caption) < MIN_WORDS:
            await client.send_message(
                chat_id, f"❌ بیانیه باید حداقل {MIN_WORDS} کلمه داشته باشه."
            )
            return

        final_text = format_statement(caption, user["country"])

        channel_id = await get_channel_id()
        if not channel_id:
            await client.send_message(
                chat_id,
                "❌ هنوز هیچ کانالی برای انتشار بیانیه ثبت نشده؛ با ادمین هماهنگ کنید.",
            )
            return

        file_id = getattr(file_obj, "file_id", None)
        if not file_id:
            await client.send_message(chat_id, "❌ شناسه فایل نامعتبر است.")
            return

        ok = await _send_to_channel(client, channel_id, file_id, final_text)
        if not ok:
            await client.send_message(
                chat_id,
                "❌ ارسال به کانال ناموفق بود؛ دوباره امتحان کنید یا با ادمین هماهنگ کنید.",
            )
            return

        await touch_last_statement(sender_id)
        await client.send_message(chat_id, "✅ بیانیه با موفقیت در کانال منتشر شد.")
    except Exception:
        log.exception("on_photo error")
        try:
            await client.send_message(
                message.chat_id,
                "⚠️ خطای داخلی هنگام پردازش بیانیه رخ داد.",
            )
        except Exception:
            pass


# ───────────────────────── هندلر متنی یکپارچه (پیوی + ثبت کانال) ─────────────────────────

@app.on_update(filters.text)
async def on_text_any(client: BotClient, message):
    try:
        msg = getattr(message, "new_message", None)
        if not msg:
            return

        # اگر فایل هم دارد، نباید اینجا پردازش شود (on_photo مسئول است)
        # ولی اگر به هر دلیلی به اینجا رسید، رد شو.
        if getattr(msg, "file", None):
            log.debug("on_text_any: message has file, skip (should be handled by on_photo)")
            return

        sender_id = safe_sender_id(msg)
        text = (getattr(msg, "text", None) or "").strip()
        # ارقام فارسی/عربی → انگلیسی تا انتخاب منو با «۱» هم کار کند
        text_norm = text.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
        chat_id = message.chat_id
        is_private = str(chat_id).startswith("b0")

        log.info(
            "on_text_any | private=%s sender=%s chat=%s text=%r",
            is_private,
            sender_id,
            chat_id,
            text,
        )

        # ── ثبت کانال ──
        # مثل «ثبت ادمین»: اگر هنوز هیچ کانالی ثبت نشده، اولین «ثبت کانال» قبول می‌شود.
        # بعد از آن فقط ادمین می‌تواند کانال را عوض کند.
        if text == "ثبت کانال":
            existing = await get_channel_id()
            admin_ok = await is_admin(sender_id)
            log.info(
                "ثبت کانال | sender=%r is_admin=%s existing=%s chat=%s",
                sender_id,
                admin_ok,
                existing,
                chat_id,
            )
            if existing is not None and not admin_ok:
                await client.send_message(
                    chat_id,
                    "❌ کانال از قبل ثبت شده؛ فقط ادمین می‌تونه عوضش کنه.\n\n"
                    f"شناسهٔ فرستنده: `{sender_id or 'نامشخص'}`\n"
                    f"کانال فعلی: `{existing}`",
                )
                return
            await set_channel_id(chat_id)
            note = " (اولین ثبت)" if existing is None else ""
            await client.send_message(
                chat_id,
                f"✅ این چت به‌عنوان کانالِ مقصدِ بیانیه‌ها ثبت شد{note}.\n"
                f"شناسه: {chat_id}",
            )
            return

        # از اینجا به بعد فقط پیوی
        if not is_private:
            return

        # ── ثبت ادمین ──
        if text == "ثبت ادمین":
            if await any_admin_exists():
                if await is_admin(sender_id):
                    await client.send_message(chat_id, "شما از قبل ادمین هستید.")
                else:
                    await client.send_message(
                        chat_id,
                        "❌ سیستم از قبل ادمین داره. از یه ادمینِ موجود بخواید "
                        "شما رو از منو («7. افزودن ادمین جدید») اضافه کنه.",
                    )
                return
            await add_admin(sender_id)
            await client.send_message(
                chat_id,
                "✅ شما به‌عنوان اولین ادمینِ سیستم ثبت شدید.\n"
                "برای دیدن منو بنویس: مدیریت",
            )
            return

        # ── دیباگ: لیست ادمین‌ها ──
        if text in ("کی ادمینه", "لیست ادمین", "ادمین‌ها") and await is_admin(sender_id):
            async with aiosqlite.connect(DB_PATH) as db:
                cur = await db.execute("SELECT sender_id, added_at FROM admins ORDER BY added_at")
                rows = await cur.fetchall()
            if not rows:
                await client.send_message(chat_id, "هیچ ادمینی ثبت نشده.")
            else:
                lines = ["👤 ادمین‌های ثبت‌شده:\n"]
                for sid, added in rows:
                    mark = " (شما)" if sid == sender_id else ""
                    lines.append(f"• `{sid}`{mark}\n  ثبت: {added}")
                await client.send_message(chat_id, "\n".join(lines))
            return

        # ── منوی ادمین (بدون قفل کردن state روی step=menu) ──
        if text in ("مدیریت", "ادمین", "/admin") and await is_admin(sender_id):
            # اگر وسط فلو بود، لغو شود تا منو تمیز باز شود
            admin_state.pop(sender_id, None)
            await client.send_message(chat_id, ADMIN_MENU_TEXT)
            return

        # ── انتخاب عدد از منو (فقط وقتی فلو چندمرحله‌ای فعال نیست) ──
        if (
            sender_id not in admin_state
            and await is_admin(sender_id)
            and text_norm in {"1", "2", "3", "4", "5", "6", "7", "0"}
        ):
            await handle_admin_menu_choice(client, chat_id, sender_id, text_norm)
            return

        # ── فلوی چندمرحله‌ای ادمین (ثبت کاربر، بن، …) ──
        if sender_id in admin_state:
            await handle_admin_flow(client, chat_id, sender_id, text)
            return

        # ── کاربر عادی ──
        user = await get_user(sender_id)
        if not user:
            await client.send_message(
                chat_id,
                "❌ شما هنوز ثبت‌نام نشدید.\n\n"
                f"شناسه‌ی شما: `{sender_id}`\n\n"
                "این شناسه رو برای مدیر بفرستید تا ثبت‌نامتون انجام بشه.",
            )
            return

        await client.send_message(
            chat_id,
            "برای ارسال بیانیه، یه عکس همراه با متنِ بیانیه (در کپشن) بفرست.\n"
            f"حداقل {MIN_WORDS} کلمه لازمه.",
        )
    except Exception:
        log.exception("on_text_any error")
        try:
            await client.send_message(
                message.chat_id,
                "⚠️ خطای داخلی رخ داد. جزئیات در لاگ سرور ثبت شد.",
            )
        except Exception:
            pass


def _writable_tmp_dir() -> Path:
    """مسیر موقت قابل‌نوشتن (Termux-friendly)."""
    import tempfile

    candidates = [
        Path(tempfile.gettempdir()),
        Path(__file__).resolve().parent / "bot_tmp",
        Path.home() / ".cache" / "announcement_bot",
    ]
    for d in candidates:
        try:
            d.mkdir(parents=True, exist_ok=True)
            test = d / ".write_test"
            test.write_text("ok", encoding="utf-8")
            test.unlink(missing_ok=True)
            return d
        except OSError:
            continue
    # آخرین تلاش: کنار اسکریپت
    d = Path(__file__).resolve().parent / "bot_tmp"
    d.mkdir(parents=True, exist_ok=True)
    return d


async def _send_to_channel(
    client: BotClient, channel_id: str, file_id: str, text: str
) -> bool:
    """
    file_id دریافتی از پیوی برای ارسال مستقیم به کانال معمولاً
    INVALID_ACCESS می‌دهد؛ باید دانلود و دوباره آپلود شود.
    """
    tmp_dir = _writable_tmp_dir()
    safe_name = "".join(c if c.isalnum() else "_" for c in str(file_id))[-40:]
    tmp_path = str(tmp_dir / f"announce_{safe_name}.jpg")
    path_to_send = tmp_path

    try:
        log.info("download_file → %s", tmp_path)
        saved = await client.download_file(file_id, save_as=tmp_path)
        path_to_send = saved if isinstance(saved, str) else tmp_path
        if not path_to_send or not Path(path_to_send).is_file():
            log.error("download produced no file: saved=%r path=%r", saved, path_to_send)
            return False
        size = Path(path_to_send).stat().st_size
        log.info("downloaded %s bytes, sending to channel %s", size, channel_id)

        await client.send_file(
            chat_id=channel_id,
            file=path_to_send,
            text=text,
            type="Image",
        )
        log.info("send_file to channel OK")
        return True
    except Exception as e:
        log.exception("send to channel failed: %s", e)
        return False
    finally:
        try:
            if path_to_send and Path(path_to_send).is_file():
                os.remove(path_to_send)
        except OSError:
            pass


# ───────────────────────── منطق منوی ادمین ─────────────────────────

async def handle_admin_menu_choice(
    client: BotClient, chat_id: str, sender_id: str, choice: str
) -> None:
    if choice == "0":
        admin_state.pop(sender_id, None)
        await client.send_message(chat_id, "منو بسته شد.")
        return

    if choice == "1":
        admin_state[sender_id] = {"step": "register_ask_id"}
        await client.send_message(chat_id, "آیدی عددیِ کاربر رو بفرست:")
    elif choice == "2":
        admin_state[sender_id] = {"step": "remove_ask_id"}
        await client.send_message(
            chat_id, "آیدی عددیِ کاربری که می‌خوای حذف کنی رو بفرست:"
        )
    elif choice == "3":
        admin_state[sender_id] = {"step": "warn_ask_id"}
        await client.send_message(
            chat_id, "آیدی عددیِ کاربری که می‌خوای بهش اخطار بدی رو بفرست:"
        )
    elif choice == "4":
        admin_state[sender_id] = {"step": "ban_ask_id"}
        await client.send_message(
            chat_id, "آیدی عددیِ کاربری که می‌خوای بن کنی رو بفرست:"
        )
    elif choice == "5":
        admin_state[sender_id] = {"step": "unban_ask_id"}
        await client.send_message(
            chat_id, "آیدی عددیِ کاربری که می‌خوای آنبن کنی رو بفرست:"
        )
    elif choice == "6":
        admin_state.pop(sender_id, None)
        users = await list_users()
        if not users:
            await client.send_message(chat_id, "هیچ کاربری ثبت نشده.")
            return
        lines = ["📋 لیست کاربران:\n"]
        for u in users:
            status = "🚫 بن" if u["banned"] else "✅ فعال"
            last = u["last_statement_at"] or "—"
            lines.append(
                f"• {u['sender_id']} | {u['country']} | "
                f"اخطار: {u['warnings']}/{MAX_WARNINGS} | {status} | "
                f"آخرین بیانیه: {last}"
            )
        text = "\n".join(lines)
        if len(text) > 3500:
            chunk = ""
            for line in lines:
                if len(chunk) + len(line) + 1 > 3500:
                    await client.send_message(chat_id, chunk)
                    chunk = line + "\n"
                else:
                    chunk += line + "\n"
            if chunk.strip():
                await client.send_message(chat_id, chunk)
        else:
            await client.send_message(chat_id, text)
    elif choice == "7":
        admin_state[sender_id] = {"step": "add_admin_ask_id"}
        await client.send_message(chat_id, "آیدی عددیِ ادمینِ جدید رو بفرست:")


async def handle_admin_flow(
    client: BotClient, chat_id: str, sender_id: str, text: str
) -> None:
    state = admin_state.get(sender_id, {})
    step = state.get("step")

    if text == "0":
        admin_state.pop(sender_id, None)
        await client.send_message(chat_id, "لغو شد.")
        return

    if step == "register_ask_id":
        state["target_id"] = text.strip()
        state["step"] = "register_ask_country"
        await client.send_message(chat_id, "اسم کشور رو بفرست:")
        return

    if step == "register_ask_country":
        await register_user(state["target_id"], text.strip())
        admin_state.pop(sender_id, None)
        await client.send_message(
            chat_id,
            f"✅ کاربر {state['target_id']} با کشور «{text.strip()}» ثبت شد.",
        )
        return

    if step == "remove_ask_id":
        removed = await remove_user(text.strip())
        admin_state.pop(sender_id, None)
        await client.send_message(
            chat_id, "✅ کاربر حذف شد." if removed else "❌ همچین کاربری پیدا نشد."
        )
        return

    if step == "warn_ask_id":
        target = text.strip()
        user = await get_user(target)
        admin_state.pop(sender_id, None)
        if not user:
            await client.send_message(chat_id, "❌ همچین کاربری پیدا نشد.")
            return
        banned_now, count = await add_warning(target)
        msg = f"⚠️ اخطار ثبت شد ({count}/{MAX_WARNINGS})."
        if banned_now:
            msg += "\n🚫 کاربر با رسیدن به سقف اخطار، به‌صورت خودکار بن شد."
        await client.send_message(chat_id, msg)
        return

    if step == "ban_ask_id":
        ok = await set_ban(text.strip(), True)
        admin_state.pop(sender_id, None)
        await client.send_message(
            chat_id, "🚫 بن شد." if ok else "❌ همچین کاربری پیدا نشد."
        )
        return

    if step == "unban_ask_id":
        ok = await set_ban(text.strip(), False)
        admin_state.pop(sender_id, None)
        await client.send_message(
            chat_id, "✅ آنبن شد." if ok else "❌ همچین کاربری پیدا نشد."
        )
        return

    if step == "add_admin_ask_id":
        await add_admin(text.strip())
        admin_state.pop(sender_id, None)
        await client.send_message(
            chat_id, f"✅ کاربر {text.strip()} به‌عنوان ادمین اضافه شد."
        )
        return

    admin_state.pop(sender_id, None)
    log.warning("Unknown admin step for %s: %s", sender_id, step)


# ───────────────────────── اجرا ─────────────────────────

async def main() -> None:
    global BOT_STARTED_AT
    await init_db()
    BOT_STARTED_AT = time.time()
    log.info("بات بیانیه‌رسان در حال اجراست... (start_ts=%.0f)", BOT_STARTED_AT)
    log.info("برای ثبت ادمین اول، از پیوی بات بنویس: ثبت ادمین")
    await app.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Bot stopped by user.")
    except Exception:
        log.exception("Fatal error")
        raise
