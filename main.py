import base64
import os
import re
import threading
import time
import functools
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse, parse_qsl, urlencode, urlunparse, quote

import requests
from flask import (
    Flask, render_template, request, jsonify, redirect, url_for,
    session, Response, send_from_directory,
)

from utils.db import get_db
from utils.text import display_title
from utils.linkgen import generate_live_link, LinkGenError
from utils.config import (
    PUBLIC_BASE_URL, OWNER_NAME, ADMIN_KEYS, VIP_KEYS,
    END_LIVE_CONFIRM_KEY, DASHBOARD_ACCESS_KEY,
)
from recorder import start_recording, resume_pending, force_end_live

RECORDINGS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recordings")
os.makedirs(RECORDINGS_DIR, exist_ok=True)

# ─── Flask app ──────────────────────────────────────────────────────────────
flask_app = Flask(__name__)
flask_app.secret_key = os.environ.get(
    "SECRET_KEY",
    "c7c8d55d9d8b4a3c2f71b1f5f79c8ea84e8d2c7c3a4b51d70b91ef0fdad5f2f6f13e9a7b8c6d1e24f4a8e9c0b5d3a7f6d8e2c1b9a4f7d5e8c3a6b1d0f9e2c7",
)
flask_app.config["SESSION_COOKIE_HTTPONLY"] = True
flask_app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
flask_app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=12)

db = get_db()
lectures_col = db["lectures"]


# ═══════════════════════════════════════════════════════════════════════════
#  HLS PROXY (stream.js logic, ported to Python)
#  - Full CORS on EVERY response (success + error + preflight)
#  - Case-insensitive m3u8 content-type detection
#  - CloudFront signed-URL auth params inherited onto segments
#  - Original URL NEVER reaches the browser (base64 opaque tokens)
# ═══════════════════════════════════════════════════════════════════════════

UPSTREAM_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.pw.live/",
    "Origin": "https://www.pw.live",
    # sec-ch-ua / client-hints — kuch CDN edge nodes bina in headers ke bhi
    # requests ko "non-browser" maan ke drop/slow kar dete hain.
    "sec-ch-ua": '"Chromium";v="126", "Not_A Brand";v="8"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}

AUTH_PARAMS = {"signature", "policy", "key-pair-id", "expires", "start", "session-id"}
UPSTREAM_TIMEOUT = 15
UPSTREAM_MAX_RETRIES = 2  # transient CDN edge hiccups ke liye


@flask_app.after_request
def add_cors_headers(resp):
    """CORS on every response — success ho ya error."""
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, HEAD, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "*"
    resp.headers["Access-Control-Expose-Headers"] = "*"
    resp.headers["Access-Control-Max-Age"] = "86400"
    return resp


def _b64e(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def _b64d(s: str) -> str:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad).decode()


def _inherit_auth_params(seg_url: str, playlist_url: str) -> str:
    """Signed CloudFront playlist ke auth params same-host segments pe copy karo."""
    try:
        seg = urlparse(seg_url)
        pl = urlparse(playlist_url)
        if seg.netloc != pl.netloc:
            return seg_url
        seg_q = dict(parse_qsl(seg.query, keep_blank_values=True))
        seg_lower = {k.lower() for k in seg_q}
        for k, v in parse_qsl(pl.query, keep_blank_values=True):
            if k.lower() in AUTH_PARAMS and k.lower() not in seg_lower:
                seg_q[k] = v
        return urlunparse(seg._replace(query=urlencode(seg_q)))
    except Exception:
        return seg_url


def _rewrite_m3u8(body: str, playlist_url: str, name: str) -> str:
    """Playlist ke saare URLs ko proxy tokens se replace karo."""
    base = request.host_url.rstrip("/")

    def tok(raw: str) -> str:
        absolute = urljoin(playlist_url, raw.strip())
        absolute = _inherit_auth_params(absolute, playlist_url)
        return f"{base}/api/live/{quote(name)}/seg?u={_b64e(absolute)}"

    out_lines = []
    for line in body.splitlines():
        t = line.strip()
        if not t:
            out_lines.append(line)
            continue
        if t.startswith("#"):
            if "URI=" in t:
                line = re.sub(
                    r'URI="([^"]+)"',
                    lambda m: f'URI="{tok(m.group(1))}"',
                    line,
                    flags=re.IGNORECASE,
                )
            out_lines.append(line)
            continue
        out_lines.append(tok(t))
    return "\n".join(out_lines) + "\n"


def _fetch_upstream(url: str):
    """
    Upstream fetch with retry + backoff — ported from the reference
    stream.js proxy logic:
      - 2xx aur 4xx dono FINAL maane jaate hain (4xx retry karne se theek
        nahi hoga — e.g. expired signed URL — retry sirf time waste karta
        hai aur player ko zyada der "loading" pe atka deta hai).
      - Sirf 5xx / connection-level errors (timeout, DNS, reset — transient
        CDN edge hiccups) retry hote hain, chhoti backoff ke saath.
    Pehle sirf EK attempt tha (koi retry nahi) — isliye ek chhota transient
    upstream glitch turant hi player ko fatal error de deta tha, jo live
    stream ke case me bahut common hai. Ye hi "live nahi chal raha" ke
    symptoms ka ek bada part tha.
    """
    headers = dict(UPSTREAM_HEADERS)
    if request.headers.get("Range"):
        headers["Range"] = request.headers["Range"]

    last_exc = None
    for attempt in range(UPSTREAM_MAX_RETRIES + 1):
        try:
            r = requests.get(
                url, headers=headers, timeout=UPSTREAM_TIMEOUT, allow_redirects=True
            )
            if r.ok or (400 <= r.status_code < 500):
                return r  # final — 2xx ya 4xx, retry se koi fayda nahi
            last_exc = requests.RequestException(f"Upstream {r.status_code}")
        except requests.RequestException as e:
            last_exc = e
        if attempt < UPSTREAM_MAX_RETRIES:
            time.sleep(0.3 * (attempt + 1))
    raise last_exc


@flask_app.route("/api/live/<name>/playlist")
def live_playlist(name):
    """Master/media playlist — original URL DB se aati hai, browser kabhi nahi dekhta."""
    doc = lectures_col.find_one({"_id": name})
    if not doc:
        return jsonify({"error": "Stream not found"}), 404
    try:
        r = _fetch_upstream(doc["original_url"])
    except requests.RequestException as e:
        return jsonify({"error": f"Upstream error: {e}"}), 502
    if not r.ok:
        return jsonify({"error": f"Upstream failed: {r.status_code}"}), r.status_code

    body = _rewrite_m3u8(r.text, doc["original_url"], name)
    return Response(
        body,
        200,
        content_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@flask_app.route("/api/live/<name>/seg")
def live_segment(name):
    """Binary segments / nested playlists — opaque base64 token se fetch."""
    token = request.args.get("u")
    if not token:
        return jsonify({"error": "Missing segment token"}), 400
    try:
        url = _b64d(token)
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise ValueError("bad scheme")
    except Exception:
        return jsonify({"error": "Invalid segment token"}), 400

    try:
        r = _fetch_upstream(url)
    except requests.RequestException as e:
        return jsonify({"error": f"Upstream error: {e}"}), 502
    if not r.ok:
        return jsonify({"error": f"Upstream failed: {r.status_code}"}), r.status_code

    ctype = (r.headers.get("Content-Type") or "").lower()
    if "mpegurl" in ctype or "m3u8" in ctype or parsed.path.lower().endswith(".m3u8"):
        # nested playlist — usko bhi rewrite karo
        doc = lectures_col.find_one({"_id": name}, {"original_url": 1})
        playlist_base = doc["original_url"] if doc else url
        body = _rewrite_m3u8(r.text, url, name)
        return Response(body, 200, content_type="application/vnd.apple.mpegurl")

    headers = {
        "Cache-Control": "public, max-age=30",
        "Accept-Ranges": "bytes",
    }
    if r.headers.get("Content-Range"):
        headers["Content-Range"] = r.headers["Content-Range"]
    return Response(
        r.content,
        206 if r.status_code == 206 else 200,
        content_type=r.headers.get("Content-Type") or "video/mp2t",
        headers=headers,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  AUTH + ADMIN (Luctyebro jaisa strict login portal — as it is)
# ═══════════════════════════════════════════════════════════════════════════

def admin_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("is_admin"):
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "error": "Login required"}), 401
            return redirect(url_for("index"))
        return view(*args, **kwargs)
    return wrapped


@flask_app.route("/login", methods=["POST"])
def login():
    data = request.get_json(silent=True) or {}
    if (
        data.get("owner_name") == OWNER_NAME
        and data.get("admin_key") in ADMIN_KEYS
        and data.get("vip_key") in VIP_KEYS
    ):
        session.permanent = True
        session["is_admin"] = True
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "Invalid Name / Admin Key / VIP Key."}), 401


@flask_app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"ok": True})


@flask_app.route("/")
def index():
    return render_template("admin.html")


@flask_app.route("/api/generate", methods=["POST"])
@admin_required
def api_generate():
    data = request.get_json(silent=True) or {}
    original_url = (data.get("original_url") or "").strip()
    desired_name = (data.get("name") or "").strip()

    try:
        result = generate_live_link(lectures_col, original_url, desired_name, PUBLIC_BASE_URL)
    except LinkGenError as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    return jsonify({
        "ok": True,
        "name": result["name"],
        "public_link": result["public_link"],
        "status": result["status"],
    })


@flask_app.route("/api/record/<name>", methods=["POST"])
@admin_required
def api_record(name):
    """Manual override/kick — agar kisi wajah se background watcher active
    nahi hai (e.g. race condition) to ise idempotently (re)start karo.
    Normal flow mein iski zaroorat nahi padti — generate hote hi automatic
    watcher already chal raha hota hai."""
    doc = lectures_col.find_one({"_id": name})
    if not doc:
        return jsonify({"ok": False, "error": "Stream not found"}), 404
    status = doc.get("status")
    if status == "READY":
        return jsonify({"ok": False, "error": "Already READY"}), 409
    started = start_recording(name, doc["original_url"], lectures_col)
    if not started:
        return jsonify({"ok": True, "status": status, "note": "Watcher already running"})
    return jsonify({"ok": True, "status": doc.get("status", "LIVE")})


@flask_app.route("/api/status/<name>")
def api_status(name):
    """Student page isko poll karta hai — LIVE / PROCESSING / READY."""
    doc = lectures_col.find_one({"_id": name})
    if not doc:
        return jsonify({"ok": False, "error": "Not found"}), 404

    status = doc.get("status", "LIVE")
    resp = {"ok": True, "status": status, "title": display_title(name)}

    if status == "READY":
        bot_username = os.environ.get("TELEGRAM_BOT_USERNAME", "PWSENSEI_FileStoreBot")
        resp["watch_url"] = f"{PUBLIC_BASE_URL}/recordings/{name}-480p.mp4"
        resp["download_url"] = f"https://t.me/{bot_username}?start={doc['token']}"
        resp["duration"] = doc.get("duration")
    elif status == "ERROR":
        resp["error"] = doc.get("error", "Processing failed")
    return jsonify(resp)


@flask_app.route("/recordings/<path:filename>")
def recordings(filename):
    # conditional=True → Range support (Watch Online seek ke liye)
    return send_from_directory(
        RECORDINGS_DIR, filename, conditional=True, mimetype="video/mp4"
    )


@flask_app.route("/generated/<name>")
def generated(name):
    doc = lectures_col.find_one({"_id": name}, {"_id": 1, "status": 1})
    if not doc:
        return redirect(url_for("index"))
    public_link = f"{PUBLIC_BASE_URL}/{name}"
    return render_template(
        "generated.html", name=name, public_link=public_link, status=doc.get("status")
    )


# ═══════════════════════════════════════════════════════════════════════════
#  OWNER DASHBOARD (per-lecture) — secret 📐 icon (player.html) se access hota
#  hai. Page-level access wahi admin session use karta hai jo /login se
#  banta hai (admin_required) — koi alag nickname/key login nahi chahiye,
#  isliye student ke liye ye page bina admin session ke sirf redirect karta
#  hai, kuch bhi leak nahi hota.
# ═══════════════════════════════════════════════════════════════════════════

@flask_app.route("/owner/<name>")
@admin_required
def owner_lecture_dashboard(name):
    doc = lectures_col.find_one({"_id": name}, {"_id": 1})
    if not doc:
        return redirect(url_for("index"))
    return render_template(
        "owner_lecture_dashboard.html", name=name, title=display_title(name)
    )


@flask_app.route("/api/owner/<name>/info")
@admin_required
def api_owner_info(name):
    doc = lectures_col.find_one({"_id": name})
    if not doc:
        return jsonify({"ok": False, "error": "Not found"}), 404

    status = doc.get("status", "LIVE")
    created_at = doc.get("created_at")

    if status == "READY" and doc.get("duration"):
        elapsed = int(doc["duration"])
    elif created_at:
        elapsed = int((datetime.utcnow() - created_at).total_seconds())
    else:
        elapsed = 0

    resp = {
        "ok": True,
        "title": display_title(name),
        "status": status,
        "elapsed_seconds": max(0, elapsed),
        "live_link": f"{PUBLIC_BASE_URL}/{name}",
        "original_link": doc.get("original_url", ""),
    }
    if status == "READY":
        bot_username = os.environ.get("TELEGRAM_BOT_USERNAME", "PWSENSEI_FileStoreBot")
        resp["watch_url"] = f"{PUBLIC_BASE_URL}/recordings/{name}-480p.mp4"
        resp["download_url"] = f"https://t.me/{bot_username}?start={doc.get('token')}"
    elif status == "ERROR":
        resp["error"] = doc.get("error", "Processing failed")
    return jsonify(resp)


@flask_app.route("/api/owner/<name>/end-live", methods=["POST"])
@admin_required
def api_owner_end_live(name):
    data = request.get_json(silent=True) or {}
    confirm_key = (data.get("confirm_key") or "").strip()
    if confirm_key != END_LIVE_CONFIRM_KEY:
        return jsonify({"ok": False, "error": "Invalid Confirmation Key"}), 401

    doc = lectures_col.find_one({"_id": name})
    if not doc:
        return jsonify({"ok": False, "error": "Not found"}), 404

    status = doc.get("status", "LIVE")
    if status != "LIVE":
        return jsonify(
            {"ok": False, "error": f"Class is already '{status}' — can't End Live again."}
        ), 409

    started = force_end_live(name, lectures_col)
    if not started:
        return jsonify({"ok": False, "error": "Could not end live — original link missing."}), 500

    return jsonify({"ok": True, "status": "PROCESSING"})


# ═══════════════════════════════════════════════════════════════════════════
#  Secret 📐 icon (player.html) → "NO" → Dashboard Key gate.
#  Key sirf yahin (.py, backend) mein hai — kabhi JS/HTML mein nahi. Player
#  page sirf is endpoint ko call karta hai; sahi key par hi /owner/<name>
#  par redirect karta hai (jo khud admin_required session se bhi protected
#  hai — do layers).
# ═══════════════════════════════════════════════════════════════════════════

_dash_key_attempts = {}  # ip -> [failed_count, locked_until_epoch]
_dash_key_attempts_lock = threading.Lock()
_DASH_KEY_MAX_ATTEMPTS = 8
_DASH_KEY_LOCKOUT_SECONDS = 300


@flask_app.route("/api/dashboard-key/verify", methods=["POST"])
def api_dashboard_key_verify():
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "unknown").split(",")[0].strip()
    now = time.time()

    with _dash_key_attempts_lock:
        failed, locked_until = _dash_key_attempts.get(ip, [0, 0])
        if now < locked_until:
            return jsonify({"ok": False, "error": "Too many attempts, try again later."}), 429

    data = request.get_json(silent=True) or {}
    key = (data.get("key") or "").strip()

    if key and key == DASHBOARD_ACCESS_KEY:
        with _dash_key_attempts_lock:
            _dash_key_attempts.pop(ip, None)
        return jsonify({"ok": True})

    with _dash_key_attempts_lock:
        failed, locked_until = _dash_key_attempts.get(ip, [0, 0])
        failed += 1
        if failed >= _DASH_KEY_MAX_ATTEMPTS:
            locked_until = now + _DASH_KEY_LOCKOUT_SECONDS
            failed = 0
        _dash_key_attempts[ip] = [failed, locked_until]

    return jsonify({"ok": False}), 401


@flask_app.route("/health")
def health():
    return jsonify({"status": "ok"})


@flask_app.route("/<name>")
def play(name):
    doc = lectures_col.find_one({"_id": name})
    if not doc:
        return "Link galat hai ya Class expire ho gayi. 😔", 404
    return render_template(
        "player.html",
        name=name,
        title=display_title(name),
        status=doc.get("status", "LIVE"),
    )


# ── Startup recovery ─────────────────────────────────────────────────────
# App start/redeploy hote hi jo lectures LIVE/RECORDING/PROCESSING atki hui
# thi unke background watchers dobara chalu karo, taaki koi bhi live class
# jiska recording pending tha wo aage bhi khud-ba-khud process ho jaaye.
threading.Thread(target=resume_pending, args=(lectures_col,), daemon=True).start()

# ── Telegram "Live Link Generator" bot ───────────────────────────────────
# Same process/thread ke andar bot bhi chalta hai (jaise recorder watchers
# chalte hain) — koi alag Render service nahi chahiye. Agar TELEGRAM_API_ID /
# TELEGRAM_API_HASH / LIVE_BOT_TOKEN set nahi hai to bot silently skip ho
# jaata hai (website normal chalta rehta hai, kuchh nahi tootega).
try:
    from bot import start_bot_in_background
    start_bot_in_background(lectures_col)
except Exception as e:
    print(f"[main] Telegram bot start skipped/failed: {e}")


def run_flask():
    port = int(os.environ.get("PORT", 8000))
    flask_app.run(host="0.0.0.0", port=port, threaded=True)


if __name__ == "__main__":
    run_flask()
