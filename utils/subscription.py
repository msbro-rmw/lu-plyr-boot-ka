"""
utils/subscription.py — Telegram bot ke liye subscription/auth + user store.

Do collections use hote hain (dono naye, existing `lectures` collection ko
haath nahi lagaya):
  - bot_auth  : { _id: user_id(int), granted_at: datetime(UTC),
                  expires_at: datetime(UTC), duration_text: str }
  - bot_users : { _id: user_id(int), first_name, username, joined_at }
                (broadcast ke liye — HAR user jo kabhi bhi bot se mila,
                 chahe authorised ho ya na ho)

Restart-safety: Mongo hi source of truth hai, lekin har write ke saath
local JSON file (bot_data.json) bhi turant update hoti hai, taaki agar
Mongo connection kabhi thodi der ke liye fail ho jaaye to bhi data
disk par safe rahe aur startup par recover ho sake.
"""
import json
import os
import threading
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))

_JSON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bot_data.json")
_JSON_PATH = os.path.abspath(_JSON_PATH)
_json_lock = threading.Lock()

_DURATION_UNITS = {
    "day": 1, "days": 1,
    "week": 7, "weeks": 7,
    "month": 30, "months": 30,
    "year": 365, "years": 365,
}


class DurationParseError(Exception):
    pass


def parse_duration(text: str) -> timedelta:
    """'1 Week' / '2 Week' / '36 Day' / '2 Day' / '2 Year' -> timedelta.
    Case-insensitive, quotes optional, extra spaces okay."""
    if not text:
        raise DurationParseError("Duration missing — e.g. \"1 Week\", \"2 Day\", \"2 Year\"")
    t = text.strip().strip('"').strip("'").strip()
    parts = t.split()
    if len(parts) != 2:
        raise DurationParseError(f"Invalid duration format: {text!r} — e.g. \"1 Week\"")
    num_str, unit = parts[0], parts[1].lower()
    if not num_str.isdigit():
        raise DurationParseError(f"Invalid number in duration: {num_str!r}")
    num = int(num_str)
    if num <= 0:
        raise DurationParseError("Duration number must be positive")
    if unit not in _DURATION_UNITS:
        raise DurationParseError(f"Unknown unit {unit!r} — use Day/Week/Month/Year")
    return timedelta(days=num * _DURATION_UNITS[unit])


def fmt_ist(dt: datetime) -> str:
    if dt is None:
        return "-"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST).strftime("%d %b %Y, %I:%M %p IST")


# ─── JSON mirror (best-effort backup cache) ────────────────────────────────

def _load_json() -> dict:
    if not os.path.exists(_JSON_PATH):
        return {"auth": {}, "users": {}}
    try:
        with open(_JSON_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"auth": {}, "users": {}}


def _save_json(data: dict):
    with _json_lock:
        try:
            tmp = _JSON_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2, default=str)
            os.replace(tmp, _JSON_PATH)
        except Exception as e:
            print(f"[subscription] JSON persist failed (non-fatal, Mongo is source of truth): {e}")


def _mirror_auth(user_id: int, granted_at, expires_at, duration_text):
    data = _load_json()
    data.setdefault("auth", {})[str(user_id)] = {
        "granted_at": granted_at.isoformat() if granted_at else None,
        "expires_at": expires_at.isoformat() if expires_at else None,
        "duration_text": duration_text,
    }
    _save_json(data)


def _mirror_remove_auth(user_id: int):
    data = _load_json()
    data.setdefault("auth", {}).pop(str(user_id), None)
    _save_json(data)


def _mirror_user(user_id: int, first_name, username):
    data = _load_json()
    data.setdefault("users", {})[str(user_id)] = {
        "first_name": first_name,
        "username": username,
        "joined_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_json(data)


# ─── Mongo-backed operations ────────────────────────────────────────────────

def add_auth(auth_col, user_id: int, duration_text: str) -> dict:
    """Grant/extend access. Agar user already active hai to expiry usi se
    aage extend hoti hai (remaining time lose nahi hota); expire ho chuka
    ho ya naya user ho to ab (now) se shuru hoti hai."""
    delta = parse_duration(duration_text)
    now = datetime.now(timezone.utc)

    existing = auth_col.find_one({"_id": user_id})
    base = now
    if existing and existing.get("expires_at"):
        exp = existing["expires_at"]
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp > now:
            base = exp

    expires_at = base + delta
    granted_at = now

    auth_col.update_one(
        {"_id": user_id},
        {"$set": {
            "granted_at": granted_at,
            "expires_at": expires_at,
            "duration_text": duration_text,
        }},
        upsert=True,
    )
    _mirror_auth(user_id, granted_at, expires_at, duration_text)
    return {"granted_at": granted_at, "expires_at": expires_at}


def remove_auth(auth_col, user_id: int) -> bool:
    res = auth_col.delete_one({"_id": user_id})
    _mirror_remove_auth(user_id)
    return res.deleted_count > 0


def get_auth(auth_col, user_id: int):
    return auth_col.find_one({"_id": user_id})


def is_authorised(auth_col, user_id: int) -> bool:
    doc = get_auth(auth_col, user_id)
    if not doc or not doc.get("expires_at"):
        return False
    exp = doc["expires_at"]
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return exp > datetime.now(timezone.utc)


def list_auth(auth_col):
    return list(auth_col.find({}).sort("expires_at", 1))


# ─── Bot users (for /Broadcast) ────────────────────────────────────────────

def register_user(users_col, user_id: int, first_name: str, username: str):
    users_col.update_one(
        {"_id": user_id},
        {
            "$set": {"first_name": first_name, "username": username},
            "$setOnInsert": {"joined_at": datetime.now(timezone.utc)},
        },
        upsert=True,
    )
    _mirror_user(user_id, first_name, username)


def list_user_ids(users_col):
    return [d["_id"] for d in users_col.find({}, {"_id": 1})]
