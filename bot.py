"""
bot.py — "PW Live Link Generator" Telegram bot.

Isi repo ke Flask app (main.py) ke SAME process/container ke andar, ek
background thread me chalta hai — bilkul waise hi jaise recorder.py ke
watcher threads chalte hain. Koi alag Render service deploy nahi karna.

FLOW (jaisa maanga gaya):
  /start   → welcome image + caption (user mention) + Join Channel /
             Contact Us buttons. Har user register ho jaata hai (broadcast
             list ke liye), chahe wo channel join kare ya na kare.

  /Live    → 1) force-subscribe check (channel join zaroori)
             2) paid-auth check (/Addauth se milta hai)
             3) VIP key maango → verify
             4) index.m3u8 URL maango → format verify
             5) video titel maango → sanitize (Hindi/English/numbers,
                spaces→hyphen, special chars silently space ban jaate hain,
                NO length limit)
             6) Confirmation Required (Confirm ✅ / Back ❌)
             7) Confirm → utils.linkgen.generate_live_link() (WEBSITE
                wala EXACT same function — no duplicate logic) → success
                card with Copy Titel / Copy Link buttons.

  Owner-only: /Addauth, /rmauth, /User, /Broadcast
  Any user:   /MyPlan

ENV VARS needed (naye, existing website env vars ko chhua nahi hai):
  TELEGRAM_API_ID       — my.telegram.org se
  TELEGRAM_API_HASH     — my.telegram.org se
  LIVE_BOT_TOKEN        — is bot ka token (BotFather se) — file-store bot
                           (TELEGRAM_BOT_TOKEN, recorder.py) se ALAG hai
  TELEGRAM_OWNER_ID     — owner ka numeric Telegram user id (owner-only
                           commands isi se match hote hain)
  FORCE_SUB_CHANNEL     — default "PW_SENSEI" (bina @ ke)
Agar TELEGRAM_API_ID / TELEGRAM_API_HASH / LIVE_BOT_TOKEN set nahi hai to
bot chupchaap start nahi hota — website par koi asar nahi padta.
"""
import os
import re
import socket
import threading
import time
import traceback
import uuid
from datetime import datetime, timedelta, timezone

from utils.config import PUBLIC_BASE_URL, VIP_KEYS
from utils.linkgen import generate_live_link, LinkGenError
from utils.subscription import (
    add_auth, remove_auth, get_auth, is_authorised, list_auth,
    register_user, list_user_ids, parse_duration, fmt_ist,
    DurationParseError,
)
from utils.db import get_db

API_ID = os.environ.get("TELEGRAM_API_ID", "22518279").strip()
API_HASH = os.environ.get("TELEGRAM_API_HASH", "61e5cc94bc5e6318643707054e54caf4").strip()
BOT_TOKEN = os.environ.get("LIVE_BOT_TOKEN", "").strip()
OWNER_ID = os.environ.get("TELEGRAM_OWNER_ID", "8909902924").strip()
FORCE_SUB_CHANNEL = os.environ.get("FORCE_SUB_CHANNEL", "PW_SENSEI").strip().lstrip("@")

START_IMAGE_URL = "https://graph.org/file/96f7e50b37c6bd4dc5071-5eadeaf54110b8c34a.jpg"
JOIN_CHANNEL_URL = f"https://t.me/{FORCE_SUB_CHANNEL}"
CONTACT_ADMIN_URL = "https://t.me/SmartBoy_ApnaMS"

M3U8_NAME_RE = re.compile(r"^(index(_\d+|\d+_)?|master)\.m3u8$", re.IGNORECASE)

# user_id -> {"step": str, "data": {...}}  (in-memory /Live conversation state)
_pending = {}
_pending_lock = threading.Lock()

# Guard so start_bot_in_background() can never spin up a second bot thread
# WITHIN THE SAME PROCESS (belt-and-suspenders — the real cross-process
# protection is the Mongo leader-lock below, LEADER_LOCK_* constants).
_bot_thread_lock = threading.Lock()
_bot_thread_started = False

# ── Cross-process leader lock (Mongo-backed) ────────────────────────────
# THE REAL FIX for "every command answered N times": if the hosting
# platform (Render etc.) ever runs more than ONE process for this app
# (multiple gunicorn workers, a rolling-deploy overlap, a stuck old
# instance, etc.), EACH process would otherwise open its OWN MTProto
# session on the SAME LIVE_BOT_TOKEN — and Telegram delivers every update
# to ALL active sessions of a bot token independently, so every command
# gets answered once per running session. A lock inside one process can
# never prevent that; only a lock that ALL processes check (i.e. stored in
# the shared MongoDB) can. Exactly one process "wins" the lock and runs
# the Telegram client; every other process politely stays silent and just
# keeps checking whether the leader has died (stale heartbeat) so it can
# take over automatically if it does.
LEADER_LOCK_COLLECTION = "bot_leader_lock"
LEADER_LOCK_ID = "live_link_bot"
LEADER_LOCK_STALE_SECONDS = 45      # leader maana jaata hai "mar gaya" agar itni der heartbeat na aaye
LEADER_LOCK_HEARTBEAT_SECONDS = 15  # leader har itni der mein apna heartbeat update karta hai


def _is_owner(user_id: int) -> bool:
    try:
        return OWNER_ID and int(OWNER_ID) == int(user_id)
    except (TypeError, ValueError):
        return False


def _looks_like_m3u8(url: str) -> bool:
    if not url or not url.startswith(("http://", "https://")):
        return False
    from urllib.parse import urlparse
    path = urlparse(url).path
    filename = path.rsplit("/", 1)[-1]
    return bool(M3U8_NAME_RE.match(filename))


# ── Duplicate-update guard ──────────────────────────────────────────────
# Second layer of protection against the "every command answered N times"
# bug: even if (for whatever reason — a leaked extra session, Telegram
# redelivering an update after a reconnect, etc.) the SAME message/callback
# gets delivered to us more than once, we only ever act on it once. This is
# process-wide (not tied to a single Client instance) on purpose.
_seen_updates = set()
_seen_lock = threading.Lock()
_SEEN_MAX = 4000


def _mark_seen(key) -> bool:
    """True if this update was already handled (caller should skip it)."""
    with _seen_lock:
        if key in _seen_updates:
            return True
        _seen_updates.add(key)
        if len(_seen_updates) > _SEEN_MAX:
            _seen_updates.clear()
        return False


def _dedupe_message(func):
    async def wrapper(client, message, *args, **kwargs):
        if _mark_seen(("m", message.chat.id, message.id)):
            return
        return await func(client, message, *args, **kwargs)
    wrapper.__name__ = getattr(func, "__name__", "wrapper")
    return wrapper


def _dedupe_callback(func):
    async def wrapper(client, cq, *args, **kwargs):
        if _mark_seen(("c", cq.id)):
            return
        return await func(client, cq, *args, **kwargs)
    wrapper.__name__ = getattr(func, "__name__", "wrapper")
    return wrapper


def _try_acquire_leader_lock(db, holder_id: str) -> bool:
    """Atomic — True agar YE process ab leader ban gaya (ya pehle se hai),
    False agar koi doosra process already fresh leader hai."""
    from pymongo import ReturnDocument
    from pymongo.errors import DuplicateKeyError

    col = db[LEADER_LOCK_COLLECTION]
    now = datetime.now(timezone.utc)
    stale_before = now - timedelta(seconds=LEADER_LOCK_STALE_SECONDS)
    try:
        result = col.find_one_and_update(
            {
                "_id": LEADER_LOCK_ID,
                "$or": [
                    {"heartbeat": {"$lt": stale_before}},
                    {"heartbeat": {"$exists": False}},
                ],
            },
            {"$set": {"holder": holder_id, "heartbeat": now}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        return bool(result) and result.get("holder") == holder_id
    except DuplicateKeyError:
        # Race: kisi aur process ne isi waqt fresh lock le liya — hum haare.
        return False
    except Exception as e:
        # Mongo hi down ho gaya ho to bot ko hamesha ke liye chup mat karo —
        # best-effort fail-open (single-process deployments ke liye ye hi
        # sahi/safe default hai; multi-worker mein rare duplicate se better
        # hai ki bot chalta rahe).
        print(f"[bot] ⚠️ leader-lock check errored ({e}) — proceeding without lock (fail-open).")
        return True


def _leader_heartbeat_loop(db, holder_id: str, stop_event: threading.Event):
    col = db[LEADER_LOCK_COLLECTION]
    while not stop_event.is_set():
        try:
            col.update_one(
                {"_id": LEADER_LOCK_ID, "holder": holder_id},
                {"$set": {"heartbeat": datetime.now(timezone.utc)}},
            )
        except Exception:
            pass
        stop_event.wait(LEADER_LOCK_HEARTBEAT_SECONDS)


def _compute_retry_backoff(e: Exception, attempt: int) -> int:
    """Kitni der wait karke retry karna hai — FloodWait ho to Telegram ne
    jitna bola EXACTLY utna hi (na kam, na zyada baar-baar bomb-baazi karo,
    warna Telegram flood-wait aur badha deta hai — yehi asli wajah thi jab
    bot bilkul chup ho gaya tha: pehle 60s cap tha jo har baar Telegram ke
    flood-wait ko dobara violate karke aur lamba bana raha tha)."""
    err_name = type(e).__name__
    if err_name == "FloodWait":
        wait = int(getattr(e, "value", None) or getattr(e, "x", None) or 30)
        print(f"[bot] ⏳ Telegram FloodWait — bilkul {wait}s wait karenge (jaisa Telegram ne bola), "
              f"jaldi retry NAHI karenge (warna wait aur badh jaata hai).")
        return wait

    hint = ""
    if err_name in ("AccessTokenInvalid", "AccessTokenExpired"):
        hint = " → LIVE_BOT_TOKEN galat/expired hai, BotFather se dobara check karo."
    elif err_name == "ApiIdInvalid":
        hint = " → TELEGRAM_API_ID / TELEGRAM_API_HASH galat hai, my.telegram.org se dobara check karo."
    elif err_name == "ApiIdPublishedFlood":
        hint = " → ye API_ID/HASH public leak ho chuka hai, my.telegram.org se naya generate karo."
    print(f"[bot] ❌ attempt #{attempt} failed: {err_name}: {e}{hint}")
    if err_name not in ("AccessTokenInvalid", "AccessTokenExpired", "ApiIdInvalid", "ApiIdPublishedFlood"):
        traceback.print_exc()  # unrecognised error — poora traceback Render logs mein daalo
    wait = min(300, 10 * attempt)
    print(f"[bot] retrying in {wait}s…")
    return wait


def start_bot_in_background(lectures_col):
    """main.py isko call karta hai app boot hote hi."""
    global _bot_thread_started
    with _bot_thread_lock:
        if _bot_thread_started:
            print("[bot] ⚠️ start_bot_in_background() called again — ignoring "
                  "(bot thread already running, isse hi duplicate-reply bug hota tha).")
            return
        _bot_thread_started = True

    missing = []
    if not API_ID: missing.append("TELEGRAM_API_ID")
    if not API_HASH: missing.append("TELEGRAM_API_HASH")
    if not BOT_TOKEN: missing.append("LIVE_BOT_TOKEN")
    if missing:
        print(f"[bot] ❌ NOT STARTED — missing env var(s): {', '.join(missing)}. "
              f"Add these in Render → Environment, then redeploy. Website is unaffected.")
        return
    if not OWNER_ID:
        print("[bot] ⚠️ TELEGRAM_OWNER_ID not set — /Addauth, /rmauth, /User, /Broadcast "
              "will reply 'Only the owner can use this command' for EVERYONE until this is set.")

    def _run():
        import asyncio
        # Naya thread hai — is thread ke liye explicitly ek asyncio event
        # loop set karna zaroori hai (Python background threads me by
        # default koi current loop nahi hota, aur Pyrogram internally
        # asyncio.get_event_loop() use karta hai).
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        stop_heartbeat = None
        try:
            db = get_db()
            holder_id = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"

            waited_log = False
            while not _try_acquire_leader_lock(db, holder_id):
                if not waited_log:
                    print("[bot] ⏸️  another process/worker already holds the Live Link Bot "
                          "leader-lock — this process will stay silent (checking again every "
                          "20s in case it needs to take over).")
                    waited_log = True
                time.sleep(20)

            print(f"[bot] 🔑 leader-lock acquired (holder={holder_id}) — this process runs the bot.")
            stop_heartbeat = threading.Event()
            threading.Thread(
                target=_leader_heartbeat_loop, args=(db, holder_id, stop_heartbeat), daemon=True
            ).start()
        except Exception as e:
            print(f"[bot] ⚠️ leader-lock setup failed ({e}) — proceeding without it (fail-open).")

        # Retry loop — koi bhi startup/connection error ho (bad token,
        # temporary network issue, Telegram side hiccup, FloodWait) to bot
        # HAMESHA ke liye chup nahi ho jaata. Har attempt clearly logged
        # hoti hai taaki Render logs dekh kar exact wajah pata chal jaaye.
        attempt = 0
        while True:
            attempt += 1
            try:
                _run_bot(lectures_col)
                # app.run()/idle() sirf graceful shutdown (SIGINT/SIGTERM)
                # par hi return karta hai — is line tak aana normal hi hai.
                print("[bot] stopped (shutdown signal received).")
                break
            except Exception as e:
                wait = _compute_retry_backoff(e, attempt)
                time.sleep(wait)

        if stop_heartbeat is not None:
            stop_heartbeat.set()

    threading.Thread(target=_run, daemon=True).start()


def _run_bot(lectures_col):
    from pyrogram import Client, filters, enums
    from pyrogram.types import (
        InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, Message,
    )
    from pyrogram.errors import UserNotParticipant, RPCError

    # Bot ka apna Mongo db handle (lectures_col upar se aata hai, auth/users
    # naye collections hain).
    db = get_db()
    auth_col = db["bot_auth"]
    users_col = db["bot_users"]

    app = Client(
        "pw_live_bot",
        api_id=int(API_ID),
        api_hash=API_HASH,
        bot_token=BOT_TOKEN,
        in_memory=True,
    )

    # ── Optional native "copy to clipboard" button (Bot API 7.1+). Agar
    # installed pyrogram version isko support nahi karta to hum niche
    # gracefully callback-based fallback pe chale jaate hain. ──
    try:
        from pyrogram.types import CopyTextButton
        _HAS_COPY_BUTTON = True
    except ImportError:
        CopyTextButton = None
        _HAS_COPY_BUTTON = False

    def _copy_button(label: str, value: str, fallback_cb: str):
        if _HAS_COPY_BUTTON:
            try:
                return InlineKeyboardButton(label, copy_text=CopyTextButton(text=value))
            except Exception:
                pass
        return InlineKeyboardButton(label, callback_data=fallback_cb)

    def _clear_pending(user_id):
        with _pending_lock:
            _pending.pop(user_id, None)

    def _set_pending(user_id, step, data=None):
        with _pending_lock:
            entry = _pending.get(user_id, {"data": {}})
            entry["step"] = step
            if data:
                entry["data"].update(data)
            _pending[user_id] = entry

    def _get_pending(user_id):
        with _pending_lock:
            return _pending.get(user_id)

    async def _is_channel_member(user_id: int) -> bool:
        try:
            member = await app.get_chat_member(FORCE_SUB_CHANNEL, user_id)
            return member.status not in (
                enums.ChatMemberStatus.LEFT, enums.ChatMemberStatus.BANNED,
            )
        except UserNotParticipant:
            return False
        except RPCError as e:
            print(f"[bot] force-sub check error: {e}")
            # Channel misconfigured / bot not admin — fail-open so the bot
            # doesn't become totally unusable due to a setup mistake.
            return True

    def _join_channel_kb():
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("📢 Join Channel", url=JOIN_CHANNEL_URL),
            InlineKeyboardButton("💬 Contact US", url=CONTACT_ADMIN_URL),
        ]])

    def _contact_admin_kb():
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("💬 Contact Admin", url=CONTACT_ADMIN_URL),
        ]])

    # ═══════════════════════════════════ /start ═══════════════════════════
    @app.on_message(filters.command("start") & filters.private)
    @_dedupe_message
    async def cmd_start(client, message: Message):
        u = message.from_user
        register_user(users_col, u.id, u.first_name or "", u.username or "")
        _clear_pending(u.id)

        mention = u.mention(u.first_name or "there")
        caption = (
            f"👋 Welcome, {mention}!\n\n"
            "This is the **PW Live Link Generator Bot**.\n"
            "Join our channel to get access, then send /Live to generate "
            "your live class link. 🎥"
        )
        try:
            await message.reply_photo(
                START_IMAGE_URL, caption=caption, reply_markup=_join_channel_kb()
            )
        except Exception:
            await message.reply_text(caption, reply_markup=_join_channel_kb())

    # ═══════════════════════════════════ /MyPlan ══════════════════════════
    @app.on_message(filters.command(["myplan", "MyPlan"], case_sensitive=False) & filters.private)
    @_dedupe_message
    async def cmd_myplan(client, message: Message):
        doc = get_auth(auth_col, message.from_user.id)
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
        exp = expires_at.replace(tzinfo=timezone.utc) if expires_at and expires_at.tzinfo is None else expires_at

        if exp and exp > now:
            remaining = exp - now
            days, rem = divmod(int(remaining.total_seconds()), 86400)
            hours = rem // 3600
            left_text = f"{days}d {hours}h" if days else f"{hours}h"
            status_line = f"Your subscription will be expired in **{left_text}**"
        else:
            since = now - exp if exp else None
            days = since.days if since else 0
            status_line = f"⚠️ Your subscription **expired** {days}d ago."

        text = (
            "**📋 Your Plan**\n\n"
            f"Subscription get: `{fmt_ist(granted_at)}`\n"
            f"Subscription expiring: `{fmt_ist(expires_at)}`\n\n"
            f"{status_line}\n\n"
            "Renew Your Subscription From Our Admin"
        )
        await message.reply_text(text, reply_markup=_contact_admin_kb())

    # ═══════════════════════════════════ /Addauth ═════════════════════════
    @app.on_message(filters.command(["addauth", "Addauth"], case_sensitive=False) & filters.private)
    @_dedupe_message
    async def cmd_addauth(client, message: Message):
        if not _is_owner(message.from_user.id):
            return await message.reply_text("⛔ Only the owner can use this command.")

        args = message.text.split(None, 1)[1] if len(message.text.split(None, 1)) > 1 else ""
        m = re.match(r'^\s*(\d+)\s+"?([^"]+?)"?\s*$', args)
        if not m:
            return await message.reply_text(
                'Usage: `/Addauth user_id "1 Week"` (units: Day/Week/Month/Year)'
            )
        target_id, duration_text = int(m.group(1)), m.group(2).strip()

        try:
            result = add_auth(auth_col, target_id, duration_text)
        except DurationParseError as e:
            return await message.reply_text(f"❌ {e}")

        await message.reply_text(
            f"✅ Auth granted to `{target_id}` for **{duration_text}**.\n"
            f"Expires: `{fmt_ist(result['expires_at'])}`"
        )
        try:
            await client.send_message(
                target_id,
                f"🎉 You've been granted a subscription for **{duration_text}**!\n"
                f"Expires: `{fmt_ist(result['expires_at'])}`\n\nSend /Live to generate your link.",
            )
        except Exception:
            pass

    # ═══════════════════════════════════ /rmauth ══════════════════════════
    @app.on_message(filters.command(["rmauth", "Rmauth", "RmAuth"], case_sensitive=False) & filters.private)
    @_dedupe_message
    async def cmd_rmauth(client, message: Message):
        if not _is_owner(message.from_user.id):
            return await message.reply_text("⛔ Only the owner can use this command.")
        parts = message.text.split()
        if len(parts) < 2 or not parts[1].isdigit():
            return await message.reply_text("Usage: `/rmauth user_id`")
        target_id = int(parts[1])
        removed = remove_auth(auth_col, target_id)
        await message.reply_text(
            f"✅ Removed `{target_id}` from authorised list." if removed
            else f"ℹ️ `{target_id}` wasn't authorised."
        )

    # ═══════════════════════════════════ /User ════════════════════════════
    @app.on_message(filters.command(["user", "User"], case_sensitive=False) & filters.private)
    @_dedupe_message
    async def cmd_user(client, message: Message):
        if not _is_owner(message.from_user.id):
            return await message.reply_text("⛔ Only the owner can use this command.")
        docs = list_auth(auth_col)
        if not docs:
            return await message.reply_text("No authorised users yet.")
        now = datetime.now(timezone.utc)
        lines = ["**📋 Authorised Users**\n"]
        for d in docs:
            exp = d.get("expires_at")
            exp_aware = exp.replace(tzinfo=timezone.utc) if exp and exp.tzinfo is None else exp
            tag = "✅ Active" if exp_aware and exp_aware > now else "❌ Expired"
            lines.append(f"`{d['_id']}` — {tag} — till `{fmt_ist(exp)}`")
        await message.reply_text("\n".join(lines))

    # ═══════════════════════════════════ /Broadcast ═══════════════════════
    @app.on_message(filters.command(["broadcast", "Broadcast"], case_sensitive=False) & filters.private)
    @_dedupe_message
    async def cmd_broadcast(client, message: Message):
        if not _is_owner(message.from_user.id):
            return await message.reply_text("⛔ Only the owner can use this command.")
        if not message.reply_to_message:
            return await message.reply_text(
                "Reply to any text/photo/video/sticker/file with /Broadcast to send it to all users."
            )
        ids = list_user_ids(users_col)
        status = await message.reply_text(f"📢 Broadcasting to {len(ids)} users…")
        sent, failed = 0, 0
        for uid in ids:
            try:
                await message.reply_to_message.copy(uid)
                sent += 1
            except Exception:
                failed += 1
            time.sleep(0.05)  # rate-limit friendly
        await status.edit_text(f"✅ Broadcast done. Sent: {sent}, Failed: {failed}")

    # ═══════════════════════════════════ /Live ═════════════════════════════
    @app.on_message(filters.command(["live", "Live"], case_sensitive=False) & filters.private)
    @_dedupe_message
    async def cmd_live(client, message: Message):
        uid = message.from_user.id

        if not await _is_channel_member(uid):
            return await message.reply_text(
                "🚫 You must join our channel first to use this bot.",
                reply_markup=_join_channel_kb(),
            )

        if not is_authorised(auth_col, uid):
            return await message.reply_text(
                "**Sorry Dude 😎**\nYou are Not Subscribe me\n\n"
                "Get an Subscription from our Team Admin",
                reply_markup=_contact_admin_kb(),
            )

        _set_pending(uid, "await_key", {})
        await message.reply_text(
            "**Going to creating Live Link**\n\nSend me VIP Key for Verification."
        )

    # ═══════════════════════════════════ Conversation flow (text replies) ══
    @app.on_message(filters.text & filters.private & ~filters.via_bot & filters.regex(r"^(?!/)"))
    @_dedupe_message
    async def flow_handler(client, message: Message):
        uid = message.from_user.id
        # Always keep the broadcast/user list fresh, even for users who
        # never ran /start explicitly (e.g. shared bot link mid-flow).
        register_user(users_col, uid, message.from_user.first_name or "", message.from_user.username or "")

        state = _get_pending(uid)
        if not state:
            return  # koi active /Live flow nahi — chup raho

        step = state["step"]

        if step == "await_key":
            key = message.text.strip()
            if key not in VIP_KEYS:
                return await message.reply_text(
                    "**invalid Key**\n\nPlease send me any of Working key 🗝️"
                )
            try:
                await message.delete()
            except Exception:
                pass
            _set_pending(uid, "await_url")
            await message.reply_text(
                "**Need Base URL**\n\nSend me your video **index.m3u8** to creating a Live Link"
            )
            return

        if step == "await_url":
            url = message.text.strip()
            if not _looks_like_m3u8(url):
                return await message.reply_text(
                    "**invalid URL Format**\n\nPlease send only m3u8 Url formats 🧐"
                )
            _set_pending(uid, "await_title", {"url": url})
            await message.reply_text(
                "**Need Video Titel**\n\n"
                "Now send me your video Titel(including any of this हिंदी , English and Numbers)"
            )
            return

        if step == "await_title":
            raw_title = message.text.strip()
            if not raw_title:
                return await message.reply_text("Please send a valid title.")
            _set_pending(uid, "await_confirm", {"raw_title": raw_title})
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Confirm", callback_data="live_confirm"),
                InlineKeyboardButton("❌ Back", callback_data="live_back"),
            ]])
            await message.reply_text(
                "**Confirmation Required**\n\nAre you Sure to Generate Live Link?",
                reply_markup=kb,
            )
            return

    # ═══════════════════════════════════ Confirm / Back / Copy buttons ═════
    @app.on_callback_query(filters.regex("^live_confirm$"))
    @_dedupe_callback
    async def cb_confirm(client, cq: CallbackQuery):
        uid = cq.from_user.id
        state = _get_pending(uid)
        if not state or state.get("step") != "await_confirm":
            return await cq.answer("This request expired, send /Live again.", show_alert=True)

        data = state["data"]
        await cq.answer()
        try:
            result = generate_live_link(lectures_col, data["url"], data["raw_title"], PUBLIC_BASE_URL)
        except LinkGenError as e:
            _clear_pending(uid)
            return await cq.message.edit_text(f"❌ Failed to generate link: {e}")
        except Exception as e:
            _clear_pending(uid)
            return await cq.message.edit_text(f"❌ Unexpected error: {e}")

        _clear_pending(uid)
        kb = InlineKeyboardMarkup([[
            _copy_button("📋 Copy Titel", result["title"], f"copytitle:{result['name']}"),
            _copy_button("🔗 Copy Link", result["public_link"], f"copylink:{result['name']}"),
        ]])
        await cq.message.edit_text(
            "**LINK GENERATED SUCCESSFULLY**\n\n"
            f"titel : {result['title']}\n\n"
            "Link: Can't Write here just Copy it from Below Button.",
            reply_markup=kb,
        )

    @app.on_callback_query(filters.regex("^live_back$"))
    @_dedupe_callback
    async def cb_back(client, cq: CallbackQuery):
        _clear_pending(cq.from_user.id)
        await cq.answer("Cancelled.")
        try:
            await cq.message.delete()
        except Exception:
            pass

    @app.on_callback_query(filters.regex(r"^copytitle:"))
    @_dedupe_callback
    async def cb_copytitle(client, cq: CallbackQuery):
        # Fallback path (older pyrogram without native copy_text button).
        m = re.match(r"^copytitle:(.+)$", cq.data)
        name = m.group(1) if m else ""
        from utils.text import display_title
        await cq.answer(display_title(name), show_alert=True)

    @app.on_callback_query(filters.regex(r"^copylink:"))
    @_dedupe_callback
    async def cb_copylink(client, cq: CallbackQuery):
        m = re.match(r"^copylink:(.+)$", cq.data)
        name = m.group(1) if m else ""
        await cq.answer(f"{PUBLIC_BASE_URL}/{name}", show_alert=True)

    print("[bot] connecting to Telegram…")
    import asyncio

    async def _main():
        await app.start()
        me = await app.get_me()
        print(f"[bot] ✅ CONNECTED as @{me.username} (id={me.id}) — listening for /start, /Live now.")
        try:
            from pyrogram import idle
            await idle()
        except ImportError:
            # Bahut purana pyrogram version jisme idle() nahi hai — bas
            # process alive rakho jab tak thread khatam na ho.
            while True:
                await asyncio.sleep(3600)
        finally:
            # CRITICAL: chahe idle() normal shutdown se return ho ya beech
            # mein koi exception se bahar nikle (network blip, Telegram
            # side hiccup, etc.) — app.stop() HAMESHA chalna chahiye.
            # Pehle ye sirf "clean exit" path par hi chalta tha, isliye
            # ek exception ke baad retry-loop ek NAYA Client bana deta tha
            # jabki purana (same LIVE_BOT_TOKEN wala) session abhi bhi
            # Telegram se connected rehta tha — dono independently har
            # update receive/reply karte the, isi wajah se ek hi command
            # ka jawab kai baar (leaked sessions ki ginti jitni baar) aata
            # tha. Ab koi bhi exit path ho, purana session pehle poori
            # tarah band hota hai, phir hi retry loop dobara try karta hai.
            try:
                await app.stop()
            except Exception:
                pass
            print("[bot] disconnected.")

    asyncio.get_event_loop().run_until_complete(_main())
