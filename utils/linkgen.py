"""
utils/linkgen.py — single shared "generate a live link" function.

Pehle ye logic sirf main.py ke /api/generate route ke andar tha (website
form ke liye). Ab Telegram bot (bot.py) ko bhi EXACT same cheez karni hai
(Mongo upsert + watch_gen bump + background recorder watcher start) — isliye
ye function yaha nikaal diya taaki dono jagah (website + bot) se hamesha
same, single code-path chale — kabhi bhi behavior mismatch na ho.

main.py ka /api/generate route ab sirf HTTP layer (request parsing +
jsonify) handle karta hai, asli kaam yehi function karta hai.
"""
from datetime import datetime
import base64
import os

from utils.text import sanitize_name, display_title
from recorder import start_recording


class LinkGenError(Exception):
    """User-facing validation error (bad URL / bad name)."""


def generate_live_link(lectures_col, original_url: str, desired_name: str, public_base_url: str) -> dict:
    """
    original_url : original m3u8 link (already validated as http/https by caller)
    desired_name  : raw class-name/title text (unsanitized)
    Returns: {"name", "public_link", "status", "title"}
    Raises: LinkGenError agar name khaali/invalid nikle sanitize hone ke baad.
    """
    original_url = (original_url or "").strip()
    if not original_url:
        raise LinkGenError("Original m3u8 link required")
    if not original_url.startswith(("http://", "https://")):
        raise LinkGenError("Invalid link — valid http(s) URL do")

    name = sanitize_name(desired_name)
    if not name:
        raise LinkGenError("Invalid class name — sirf letters, numbers aur hyphen(-) allowed hai.")

    now = datetime.utcnow()
    token = base64.urlsafe_b64encode(os.urandom(12)).decode().rstrip("=")
    lectures_col.update_one(
        {"_id": name},
        {
            "$set": {
                "original_url": original_url,
                "status": "LIVE",
                "title": display_title(name),
                "updated_at": now,
            },
            "$setOnInsert": {
                "created_at": now,
                "token": token,
                "telegram_file_id": None,
                "duration": None,
            },
            # Har naye/re-generate hone par watch_gen bump — purana watcher
            # (agar isi naam ke liye chal raha ho) khud supersede ho jaata hai.
            "$inc": {"watch_gen": 1},
        },
        upsert=True,
    )
    doc = lectures_col.find_one({"_id": name})

    # Live end hote hi automatic download + Telegram upload ke liye
    # background watcher — generate hote hi khud shuru ho jaata hai.
    start_recording(name, original_url, lectures_col)

    public_link = f"{public_base_url}/{name}"
    return {
        "name": name,
        "public_link": public_link,
        "status": doc.get("status", "LIVE"),
        "title": display_title(name),
    }
