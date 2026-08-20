"""
bot.py — "PW Live Link Generator" Telegram bot.

Isi repo ke Flask app (main.py) ke SAME process/container ke andar, ek
background thread me chalta hai — bilkul waise hi jaise recorder.py ke
watcher threads chalte hain. Koi alag Render service deploy nahi karna.

IMPORTANT:
- Bot ko same process/container me sirf EK baar start kiya jayega.
- Multiple Flask/Gunicorn workers same container me bot start nahi karenge.
- Render par ideally website ko 1 worker / 1 instance ke saath run karo.
- TELEGRAM_API_ID / TELEGRAM_API_HASH hard-code nahi kiye gaye hain.
"""

import os
import re
import threading
import time
from datetime import datetime, timezone

from utils.config import PUBLIC_BASE_URL, VIP_KEYS
from utils.linkgen import generate_live_link, LinkGenError
from utils.subscription import (
    add_auth,
    remove_auth,
    get_auth,
    is_authorised,
    list_auth,
    register_user,
    list_user_ids,
    parse_duration,
    fmt_ist,
    DurationParseError,
)
from utils.db import get_db


# ═══════════════════════════════════════════════════════════════════════════
# ENVIRONMENT VARIABLES
# ═══════════════════════════════════════════════════════════════════════════

API_ID = os.environ.get("TELEGRAM_API_ID", "").strip()
API_HASH = os.environ.get("TELEGRAM_API_HASH", "").strip()
BOT_TOKEN = os.environ.get("LIVE_BOT_TOKEN", "").strip()
OWNER_ID = os.environ.get("TELEGRAM_OWNER_ID", "").strip()

FORCE_SUB_CHANNEL = (
    os.environ.get("FORCE_SUB_CHANNEL", "PW_SENSEI")
    .strip()
    .lstrip("@")
)


START_IMAGE_URL = (
    "https://graph.org/file/96f7e50b37c6bd4dc5071-5eadeaf54110b8c34a.jpg"
)

JOIN_CHANNEL_URL = f"https://t.me/{FORCE_SUB_CHANNEL}"
CONTACT_ADMIN_URL = "https://t.me/SmartBoy_ApnaMS"


# ═══════════════════════════════════════════════════════════════════════════
# REGEX
# ═══════════════════════════════════════════════════════════════════════════

M3U8_NAME_RE = re.compile(
    r"^(index(_\d+|\d+_)?|master)\.m3u8$",
    re.IGNORECASE,
)


# ═══════════════════════════════════════════════════════════════════════════
# LIVE CONVERSATION STATE
# ═══════════════════════════════════════════════════════════════════════════

# user_id -> {"step": str, "data": {...}}
_pending = {}

_pending_lock = threading.Lock()


# ═══════════════════════════════════════════════════════════════════════════
# BOT START GUARDS
# ═══════════════════════════════════════════════════════════════════════════

# Same Python process me start_bot_in_background() agar multiple baar
# call ho jaye to sirf first call bot start karegi.
_BOT_STARTED = False

_BOT_START_LOCK = threading.Lock()

# Thread reference ko alive rakhne ke liye.
_BOT_THREAD = None


# Linux/Render par multiple Gunicorn worker PROCESSES ke case me bhi
# same container ke andar sirf ek process bot start kare.
#
# NOTE:
# Ye same container ke processes ko protect karta hai.
# Agar Render par multiple separate instances chal rahe hain, unhe bhi
# 1 instance par rakhna zaroori hai.
_PROCESS_LOCK_FILE = None


# ═══════════════════════════════════════════════════════════════════════════
# OWNER CHECK
# ═══════════════════════════════════════════════════════════════════════════

def _is_owner(user_id: int) -> bool:
    try:
        return bool(OWNER_ID) and int(OWNER_ID) == int(user_id)
    except (TypeError, ValueError):
        return False


# ═══════════════════════════════════════════════════════════════════════════
# M3U8 VALIDATION
# ═══════════════════════════════════════════════════════════════════════════

def _looks_like_m3u8(url: str) -> bool:
    if not url:
        return False

    if not url.startswith(("http://", "https://")):
        return False

    from urllib.parse import urlparse

    path = urlparse(url).path
    filename = path.rsplit("/", 1)[-1]

    return bool(M3U8_NAME_RE.match(filename))


# ═══════════════════════════════════════════════════════════════════════════
# PROCESS LOCK
# ═══════════════════════════════════════════════════════════════════════════

def _acquire_process_lock():
    """
    Linux/Render par multiple Gunicorn workers same container me chal rahe
    hon to sirf ek process ko Telegram bot start karne deta hai.

    Lock file ka handle global variable me rakha jata hai taaki process
    lifetime tak lock release na ho.
    """

    global _PROCESS_LOCK_FILE

    try:
        import fcntl
    except ImportError:
        # Non-Linux environment me process lock available nahi hai.
        print(
            "[bot] ⚠️ fcntl unavailable — process-level bot lock disabled."
        )
        return True

    lock_path = "/tmp/pw_live_bot.lock"

    try:
        lock_file = open(lock_path, "w")

        try:
            fcntl.flock(
                lock_file.fileno(),
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except BlockingIOError:
            lock_file.close()

            print(
                "[bot] ⚠️ Another process is already running the Telegram "
                "bot in this container. Skipping duplicate bot startup."
            )

            return False

        _PROCESS_LOCK_FILE = lock_file

        print(
            "[bot] 🔒 Process-level Telegram bot lock acquired."
        )

        return True

    except Exception as e:
        print(
            f"[bot] ⚠️ Could not acquire process lock: "
            f"{type(e).__name__}: {e}"
        )

        # Lock failure par bot ko completely disable nahi kar rahe.
        # Same-process guard phir bhi active rahega.
        return True


# ═══════════════════════════════════════════════════════════════════════════
# START BOT IN BACKGROUND
# ═══════════════════════════════════════════════════════════════════════════

def start_bot_in_background(lectures_col):
    """
    main.py isko app boot hote hi call karta hai.

    IMPORTANT:
    Agar main.py / Flask startup is function ko multiple baar call kare,
    bot sirf EK baar start hoga.
    """

    global _BOT_STARTED
    global _BOT_THREAD

    # ───────────────────────────────────────────────────────────────────────
    # Same-process startup guard
    # ───────────────────────────────────────────────────────────────────────

    with _BOT_START_LOCK:

        if _BOT_STARTED:
            print(
                "[bot] ⚠️ Bot already started in this process — "
                "skipping duplicate startup."
            )
            return

        # Pehle hi mark kar dete hain taaki race condition me do threads
        # ek saath bot start na kar dein.
        _BOT_STARTED = True

    # ───────────────────────────────────────────────────────────────────────
    # Environment validation
    # ───────────────────────────────────────────────────────────────────────

    missing = []

    if not API_ID:
        missing.append("TELEGRAM_API_ID")

    if not API_HASH:
        missing.append("TELEGRAM_API_HASH")

    if not BOT_TOKEN:
        missing.append("LIVE_BOT_TOKEN")

    if missing:
        print(
            "[bot] ❌ NOT STARTED — missing env var(s): "
            f"{', '.join(missing)}. "
            "Add these in Render → Environment, then redeploy. "
            "Website is unaffected."
        )

        # Startup guard reset kar do taaki env fix ke baad same process me
        # retry possible ho.
        with _BOT_START_LOCK:
            _BOT_STARTED = False

        return

    if not OWNER_ID:
        print(
            "[bot] ⚠️ TELEGRAM_OWNER_ID not set — "
            "/Addauth, /rmauth, /User, /Broadcast "
            "will reply 'Only the owner can use this command' "
            "for EVERYONE until this is set."
        )

    # ───────────────────────────────────────────────────────────────────────
    # Process-level lock
    # ───────────────────────────────────────────────────────────────────────

    if not _acquire_process_lock():

        with _BOT_START_LOCK:
            _BOT_STARTED = False

        return

    # ───────────────────────────────────────────────────────────────────────
    # Background thread
    # ───────────────────────────────────────────────────────────────────────

    def _run():

        import asyncio

        # Background thread ke liye dedicated asyncio event loop.
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        attempt = 0

        while True:

            attempt += 1

            try:

                print(
                    f"[bot] Starting Telegram bot "
                    f"(attempt #{attempt})..."
                )

                _run_bot(lectures_col)

                # Normal graceful shutdown.
                print(
                    "[bot] stopped "
                    "(shutdown signal received)."
                )

                break

            except Exception as e:

                err_name = type(e).__name__
                hint = ""

                if err_name in (
                    "AccessTokenInvalid",
                    "AccessTokenExpired",
                ):
                    hint = (
                        " → LIVE_BOT_TOKEN galat/expired hai, "
                        "BotFather se dobara check karo."
                    )

                elif err_name in (
                    "ApiIdInvalid",
                ):
                    hint = (
                        " → TELEGRAM_API_ID / TELEGRAM_API_HASH "
                        "galat hai, my.telegram.org se dobara check karo."
                    )

                elif err_name in (
                    "ApiIdPublishedFlood",
                ):
                    hint = (
                        " → API_ID/API_HASH public leak ho chuka hai. "
                        "my.telegram.org se naya generate karo."
                    )

                print(
                    f"[bot] ❌ attempt #{attempt} failed: "
                    f"{err_name}: {e}{hint}"
                )

                wait = min(60, 5 * attempt)

                print(
                    f"[bot] retrying in {wait}s..."
                )

                time.sleep(wait)

    _BOT_THREAD = threading.Thread(
        target=_run,
        daemon=True,
        name="pw-live-telegram-bot",
    )

    _BOT_THREAD.start()

    print(
        "[bot] ✅ Background Telegram bot thread started."
    )


# ═══════════════════════════════════════════════════════════════════════════
# MAIN PYROGRAM BOT
# ═══════════════════════════════════════════════════════════════════════════

def _run_bot(lectures_col):

    from pyrogram import Client, filters, enums

    from pyrogram.types import (
        InlineKeyboardMarkup,
        InlineKeyboardButton,
        CallbackQuery,
        Message,
    )

    from pyrogram.errors import (
        UserNotParticipant,
        RPCError,
    )

    # ───────────────────────────────────────────────────────────────────────
    # MongoDB collections
    # ───────────────────────────────────────────────────────────────────────

    db = get_db()

    auth_col = db["bot_auth"]
    users_col = db["bot_users"]

    # ───────────────────────────────────────────────────────────────────────
    # Pyrogram client
    # ───────────────────────────────────────────────────────────────────────

    app = Client(
        "pw_live_bot",
        api_id=int(API_ID),
        api_hash=API_HASH,
        bot_token=BOT_TOKEN,
        in_memory=True,
    )

    # ───────────────────────────────────────────────────────────────────────
    # Native CopyTextButton support
    # ───────────────────────────────────────────────────────────────────────

    try:

        from pyrogram.types import CopyTextButton

        _HAS_COPY_BUTTON = True

    except ImportError:

        CopyTextButton = None
        _HAS_COPY_BUTTON = False

    # ───────────────────────────────────────────────────────────────────────
    # Copy button helper
    # ───────────────────────────────────────────────────────────────────────

    def _copy_button(
        label: str,
        value: str,
        fallback_cb: str,
    ):

        if _HAS_COPY_BUTTON:

            try:

                return InlineKeyboardButton(
                    label,
                    copy_text=CopyTextButton(
                        text=value
                    ),
                )

            except Exception:
                pass

        return InlineKeyboardButton(
            label,
            callback_data=fallback_cb,
        )

    # ═══════════════════════════════════════════════════════════════════════
    # PENDING STATE HELPERS
    # ═══════════════════════════════════════════════════════════════════════

    def _clear_pending(user_id):

        with _pending_lock:
            _pending.pop(user_id, None)

    def _set_pending(
        user_id,
        step,
        data=None,
    ):

        with _pending_lock:

            entry = _pending.get(
                user_id,
                {"data": {}},
            )

            entry["step"] = step

            if data:
                entry["data"].update(data)

            _pending[user_id] = entry

    def _get_pending(user_id):

        with _pending_lock:
            return _pending.get(user_id)

    # ═══════════════════════════════════════════════════════════════════════
    # FORCE SUB CHECK
    # ═══════════════════════════════════════════════════════════════════════

    async def _is_channel_member(user_id: int) -> bool:

        try:

            member = await app.get_chat_member(
                FORCE_SUB_CHANNEL,
                user_id,
            )

            return member.status not in (
                enums.ChatMemberStatus.LEFT,
                enums.ChatMemberStatus.BANNED,
            )

        except UserNotParticipant:

            return False

        except RPCError as e:

            print(
                f"[bot] force-sub check error: {e}"
            )

            # Channel misconfigured / bot not admin.
            # Fail-open so bot completely unusable na ho.
            return True

    # ═══════════════════════════════════════════════════════════════════════
    # KEYBOARDS
    # ═══════════════════════════════════════════════════════════════════════

    def _join_channel_kb():

        return InlineKeyboardMarkup(
            [[
                InlineKeyboardButton(
                    "📢 Join Channel",
                    url=JOIN_CHANNEL_URL,
                ),
                InlineKeyboardButton(
                    "💬 Contact US",
                    url=CONTACT_ADMIN_URL,
                ),
            ]]
        )

    def _contact_admin_kb():

        return InlineKeyboardMarkup(
            [[
                InlineKeyboardButton(
                    "💬 Contact Admin",
                    url=CONTACT_ADMIN_URL,
                ),
            ]]
        )

    # ═══════════════════════════════════════════════════════════════════════
    # /START
    # ═══════════════════════════════════════════════════════════════════════

    @app.on_message(
        filters.command("start") & filters.private
    )
    async def cmd_start(
        client,
        message: Message,
    ):

        u = message.from_user

        register_user(
            users_col,
            u.id,
            u.first_name or "",
            u.username or "",
        )

        _clear_pending(u.id)

        mention = u.mention(
            u.first_name or "there"
        )

        caption = (
            f"👋 Welcome, {mention}!\n\n"
            "This is the **PW Live Link Generator Bot**.\n"
            "Join our channel to get access, then send /Live "
            "to generate your live class link. 🎥"
        )

        try:

            await message.reply_photo(
                START_IMAGE_URL,
                caption=caption,
                reply_markup=_join_channel_kb(),
            )

        except Exception:

            await message.reply_text(
                caption,
                reply_markup=_join_channel_kb(),
            )

    # ═══════════════════════════════════════════════════════════════════════
    # /MYPLAN
    # ═══════════════════════════════════════════════════════════════════════

    @app.on_message(
        filters.command(
            ["myplan", "MyPlan"],
            case_sensitive=False,
        )
        & filters.private
    )
    async def cmd_myplan(
        client,
        message: Message,
    ):

        doc = get_auth(
            auth_col,
            message.from_user.id,
        )

        if not doc:

            await message.reply_text(
                "😔 You don't have any subscription yet.\n\n"
                "Renew Your Subscription From Our Admin",
                reply_markup=_contact_admin_kb(),
            )

            return

        granted_at = doc.get("granted_at")
        expires_at = doc.get("expires_at")

        now = datetime.now(timezone.utc)

        exp = (
            expires_at.replace(tzinfo=timezone.utc)
            if expires_at and expires_at.tzinfo is None
            else expires_at
        )

        if exp and exp > now:

            remaining = exp - now

            days, rem = divmod(
                int(remaining.total_seconds()),
                86400,
            )

            hours = rem // 3600

            left_text = (
                f"{days}d {hours}h"
                if days
                else f"{hours}h"
            )

            status_line = (
                f"Your subscription will be expired in "
                f"**{left_text}**"
            )

        else:

            since = (
                now - exp
                if exp
                else None
            )

            days = (
                since.days
                if since
                else 0
            )

            status_line = (
                f"⚠️ Your subscription **expired** "
                f"{days}d ago."
            )

        text = (
            "**📋 Your Plan**\n\n"
            f"Subscription get: `{fmt_ist(granted_at)}`\n"
            f"Subscription expiring: `{fmt_ist(expires_at)}`\n\n"
            f"{status_line}\n\n"
            "Renew Your Subscription From Our Admin"
        )

        await message.reply_text(
            text,
            reply_markup=_contact_admin_kb(),
        )

    # ═══════════════════════════════════════════════════════════════════════
    # /ADDAUTH
    # ═══════════════════════════════════════════════════════════════════════

    @app.on_message(
        filters.command(
            ["addauth", "Addauth"],
            case_sensitive=False,
        )
        & filters.private
    )
    async def cmd_addauth(
        client,
        message: Message,
    ):

        if not _is_owner(
            message.from_user.id
        ):

            return await message.reply_text(
                "⛔ Only the owner can use this command."
            )

        parts = message.text.split(
            None,
            1,
        )

        args = (
            parts[1]
            if len(parts) > 1
            else ""
        )

        m = re.match(
            r'^\s*(\d+)\s+"?([^"]+?)"?\s*$',
            args,
        )

        if not m:

            return await message.reply_text(
                'Usage: `/Addauth user_id "1 Week"` '
                '(units: Day/Week/Month/Year)'
            )

        target_id = int(
            m.group(1)
        )

        duration_text = m.group(2).strip()

        try:

            result = add_auth(
                auth_col,
                target_id,
                duration_text,
            )

        except DurationParseError as e:

            return await message.reply_text(
                f"❌ {e}"
            )

        await message.reply_text(
            f"✅ Auth granted to `{target_id}` "
            f"for **{duration_text}**.\n"
            f"Expires: `{fmt_ist(result['expires_at'])}`"
        )

        try:

            await client.send_message(
                target_id,
                f"🎉 You've been granted a subscription "
                f"for **{duration_text}**!\n"
                f"Expires: `{fmt_ist(result['expires_at'])}`\n\n"
                "Send /Live to generate your link.",
            )

        except Exception:

            pass

    # ═══════════════════════════════════════════════════════════════════════
    # /RMAUTH
    # ═══════════════════════════════════════════════════════════════════════

    @app.on_message(
        filters.command(
            ["rmauth", "Rmauth", "RmAuth"],
            case_sensitive=False,
        )
        & filters.private
    )
    async def cmd_rmauth(
        client,
        message: Message,
    ):

        if not _is_owner(
            message.from_user.id
        ):

            return await message.reply_text(
                "⛔ Only the owner can use this command."
            )

        parts = message.text.split()

        if (
            len(parts) < 2
            or not parts[1].isdigit()
        ):

            return await message.reply_text(
                "Usage: `/rmauth user_id`"
            )

        target_id = int(
            parts[1]
        )

        removed = remove_auth(
            auth_col,
            target_id,
        )

        await message.reply_text(
            (
                f"✅ Removed `{target_id}` "
                "from authorised list."
            )
            if removed
            else (
                f"ℹ️ `{target_id}` wasn't authorised."
            )
        )

    # ═══════════════════════════════════════════════════════════════════════
    # /USER
    # ═══════════════════════════════════════════════════════════════════════

    @app.on_message(
        filters.command(
            ["user", "User"],
            case_sensitive=False,
        )
        & filters.private
    )
    async def cmd_user(
        client,
        message: Message,
    ):

        if not _is_owner(
            message.from_user.id
        ):

            return await message.reply_text(
                "⛔ Only the owner can use this command."
            )

        docs = list_auth(
            auth_col
        )

        if not docs:

            return await message.reply_text(
                "No authorised users yet."
            )

        now = datetime.now(
            timezone.utc
        )

        lines = [
            "**📋 Authorised Users**\n"
        ]

        for d in docs:

            exp = d.get(
                "expires_at"
            )

            exp_aware = (
                exp.replace(
                    tzinfo=timezone.utc
                )
                if exp and exp.tzinfo is None
                else exp
            )

            tag = (
                "✅ Active"
                if exp_aware and exp_aware > now
                else "❌ Expired"
            )

            lines.append(
                f"`{d['_id']}` — {tag} — "
                f"till `{fmt_ist(exp)}`"
            )

        await message.reply_text(
            "\n".join(lines)
        )

    # ═══════════════════════════════════════════════════════════════════════
    # /BROADCAST
    # ═══════════════════════════════════════════════════════════════════════

    @app.on_message(
        filters.command(
            ["broadcast", "Broadcast"],
            case_sensitive=False,
        )
        & filters.private
    )
    async def cmd_broadcast(
        client,
        message: Message,
    ):

        if not _is_owner(
            message.from_user.id
        ):

            return await message.reply_text(
                "⛔ Only the owner can use this command."
            )

        if not message.reply_to_message:

            return await message.reply_text(
                "Reply to any text/photo/video/sticker/file "
                "with /Broadcast to send it to all users."
            )

        ids = list_user_ids(
            users_col
        )

        status = await message.reply_text(
            f"📢 Broadcasting to {len(ids)} users…"
        )

        sent = 0
        failed = 0

        for uid in ids:

            try:

                await message.reply_to_message.copy(
                    uid
                )

                sent += 1

            except Exception:

                failed += 1

            # Async sleep use karo, taaki Telegram bot ka event loop
            # unnecessarily block na ho.
            await asyncio.sleep(0.05)

        await status.edit_text(
            f"✅ Broadcast done. "
            f"Sent: {sent}, Failed: {failed}"
        )

    # ═══════════════════════════════════════════════════════════════════════
    # /LIVE
    # ═══════════════════════════════════════════════════════════════════════

    @app.on_message(
        filters.command(
            ["live", "Live"],
            case_sensitive=False,
        )
        & filters.private
    )
    async def cmd_live(
        client,
        message: Message,
    ):

        uid = message.from_user.id

        # ───────────────────────────────────────────────────────────────────
        # Force subscription
        # ───────────────────────────────────────────────────────────────────

        if not await _is_channel_member(uid):

            return await message.reply_text(
                "🚫 You must join our channel first "
                "to use this bot.",
                reply_markup=_join_channel_kb(),
            )

        # ───────────────────────────────────────────────────────────────────
        # Paid auth
        # ───────────────────────────────────────────────────────────────────

        if not is_authorised(
            auth_col,
            uid,
        ):

            return await message.reply_text(
                "**Sorry Dude 😎**\n"
                "You are Not Subscribe me\n\n"
                "Get an Subscription from our Team Admin",
                reply_markup=_contact_admin_kb(),
            )

        # ───────────────────────────────────────────────────────────────────
        # Start conversation
        # ───────────────────────────────────────────────────────────────────

        _set_pending(
            uid,
            "await_key",
            {},
        )

        await message.reply_text(
            "**Going to creating Live Link**\n\n"
            "Send me VIP Key for Verification."
        )

    # ═══════════════════════════════════════════════════════════════════════
    # LIVE CONVERSATION FLOW
    # ═══════════════════════════════════════════════════════════════════════

    @app.on_message(
        filters.text
        & filters.private
        & ~filters.via_bot
        & filters.regex(r"^(?!/)")
    )
    async def flow_handler(
        client,
        message: Message,
    ):

        uid = message.from_user.id

        # User list fresh rakho.
        register_user(
            users_col,
            uid,
            message.from_user.first_name or "",
            message.from_user.username or "",
        )

        state = _get_pending(
            uid
        )

        if not state:
            return

        step = state["step"]

        # ═══════════════════════════════════════════════════════════════════
        # STEP 1 — VIP KEY
        # ═══════════════════════════════════════════════════════════════════

        if step == "await_key":

            key = message.text.strip()

            if key not in VIP_KEYS:

                return await message.reply_text(
                    "**invalid Key**\n\n"
                    "Please send me any of Working key 🗝️"
                )

            try:

                await message.delete()

            except Exception:

                pass

            _set_pending(
                uid,
                "await_url",
            )

            await message.reply_text(
                "**Need Base URL**\n\n"
                "Send me your video **index.m3u8** "
                "to creating a Live Link"
            )

            return

        # ═══════════════════════════════════════════════════════════════════
        # STEP 2 — M3U8 URL
        # ═══════════════════════════════════════════════════════════════════

        if step == "await_url":

            url = message.text.strip()

            if not _looks_like_m3u8(url):

                return await message.reply_text(
                    "**invalid URL Format**\n\n"
                    "Please send only m3u8 Url formats 🧐"
                )

            _set_pending(
                uid,
                "await_title",
                {
                    "url": url
                },
            )

            await message.reply_text(
                "**Need Video Titel**\n\n"
                "Now send me your video Titel "
                "(including any of this हिंदी , English and Numbers)"
            )

            return

        # ═══════════════════════════════════════════════════════════════════
        # STEP 3 — TITLE
        # ═══════════════════════════════════════════════════════════════════

        if step == "await_title":

            raw_title = message.text.strip()

            if not raw_title:

                return await message.reply_text(
                    "Please send a valid title."
                )

            _set_pending(
                uid,
                "await_confirm",
                {
                    "raw_title": raw_title
                },
            )

            kb = InlineKeyboardMarkup(
                [[
                    InlineKeyboardButton(
                        "✅ Confirm",
                        callback_data="live_confirm",
                    ),
                    InlineKeyboardButton(
                        "❌ Back",
                        callback_data="live_back",
                    ),
                ]]
            )

            await message.reply_text(
                "**Confirmation Required**\n\n"
                "Are you Sure to Generate Live Link?",
                reply_markup=kb,
            )

            return

    # ═══════════════════════════════════════════════════════════════════════
    # CONFIRM LIVE LINK
    # ═══════════════════════════════════════════════════════════════════════

    @app.on_callback_query(
        filters.regex("^live_confirm$")
    )
    async def cb_confirm(
        client,
        cq: CallbackQuery,
    ):

        uid = cq.from_user.id

        state = _get_pending(
            uid
        )

        if (
            not state
            or state.get("step") != "await_confirm"
        ):

            return await cq.answer(
                "This request expired, send /Live again.",
                show_alert=True,
            )

        data = state["data"]

        await cq.answer()

        try:

            result = generate_live_link(
                lectures_col,
                data["url"],
                data["raw_title"],
                PUBLIC_BASE_URL,
            )

        except LinkGenError as e:

            _clear_pending(uid)

            return await cq.message.edit_text(
                f"❌ Failed to generate link: {e}"
            )

        except Exception as e:

            _clear_pending(uid)

            return await cq.message.edit_text(
                f"❌ Unexpected error: {e}"
            )

        _clear_pending(uid)

        kb = InlineKeyboardMarkup(
            [[
                _copy_button(
                    "📋 Copy Titel",
                    result["title"],
                    f"copytitle:{result['name']}",
                ),
                _copy_button(
                    "🔗 Copy Link",
                    result["public_link"],
                    f"copylink:{result['name']}",
                ),
            ]]
        )

        await cq.message.edit_text(
            "**LINK GENERATED SUCCESSFULLY**\n\n"
            f"titel : {result['title']}\n\n"
            "Link: Can't Write here just Copy it from Below Button.",
            reply_markup=kb,
        )

    # ═══════════════════════════════════════════════════════════════════════
    # BACK / CANCEL
    # ═══════════════════════════════════════════════════════════════════════

    @app.on_callback_query(
        filters.regex("^live_back$")
    )
    async def cb_back(
        client,
        cq: CallbackQuery,
    ):

        _clear_pending(
            cq.from_user.id
        )

        await cq.answer(
            "Cancelled."
        )

        try:

            await cq.message.delete()

        except Exception:

            pass

    # ═══════════════════════════════════════════════════════════════════════
    # COPY TITLE FALLBACK
    # ═══════════════════════════════════════════════════════════════════════

    @app.on_callback_query(
        filters.regex(r"^copytitle:")
    )
    async def cb_copytitle(
        client,
        cq: CallbackQuery,
    ):

        m = re.match(
            r"^copytitle:(.+)$",
            cq.data,
        )

        name = (
            m.group(1)
            if m
            else ""
        )

        from utils.text import display_title

        await cq.answer(
            display_title(name),
            show_alert=True,
        )

    # ═══════════════════════════════════════════════════════════════════════
    # COPY LINK FALLBACK
    # ═══════════════════════════════════════════════════════════════════════

    @app.on_callback_query(
        filters.regex(r"^copylink:")
    )
    async def cb_copylink(
        client,
        cq: CallbackQuery,
    ):

        m = re.match(
            r"^copylink:(.+)$",
            cq.data,
        )

        name = (
            m.group(1)
            if m
            else ""
        )

        await cq.answer(
            f"{PUBLIC_BASE_URL}/{name}",
            show_alert=True,
        )

    # ═══════════════════════════════════════════════════════════════════════
    # START TELEGRAM CLIENT
    # ═══════════════════════════════════════════════════════════════════════

    print(
        "[bot] connecting to Telegram..."
    )

    import asyncio

    async def _main():

        await app.start()

        me = await app.get_me()

        print(
            f"[bot] ✅ CONNECTED as "
            f"@{me.username} "
            f"(id={me.id}) — "
            f"listening for /start, /Live now."
        )

        try:

            from pyrogram import idle

            await idle()

        except ImportError:

            # Older Pyrogram fallback.
            while True:

                await asyncio.sleep(
                    3600
                )

        finally:

            try:

                await app.stop()

            except Exception:

                pass

            print(
                "[bot] disconnected."
            )

    # IMPORTANT:
    # _run() ne already is thread ke liye event loop create kiya hai.
    # Usi loop ko yahan use karna hai.
    loop = asyncio.get_event_loop()

    loop.run_until_complete(
        _main()
    )
