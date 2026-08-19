"""
recorder.py — Live end hone ke baad automatic download + Telegram delivery
pipeline. Fully rewritten (advanced version).

──────────────────────────────────────────────────────────────────────────
PURANA PROBLEM (kyun "Download" option kabhi aata hi nahi tha):
  1. Recording sirf tab start hoti thi jab ADMIN khud "Start Recording"
     button dabata tha — agar bhool gaya to READY kabhi nahi banta.
  2. Real-time ffmpeg jo LIVE playlist ko tail karta tha, live HLS ka
     "sliding window" hota hai (sirf pichhle kuch minute ke segments hi
     playlist mein rehte hain) — isliye agar recording thodi der se start
     hui to shuruaat ke segments already CDN se expire ho chuke hote the
     aur video incomplete/corrupt milta tha.
  3. Telegram ka normal Bot API (api.telegram.org) sirf ~50MB tak hi bot
     se upload allow karta hai — ek 1-2hr 480p lecture usse bada hota hai,
     isliye upload silently fail ho jaata tha aur status kabhi READY nahi
     hota tha (hence "download button" kabhi nahi aata tha).

NAYA SYSTEM (advanced fix):
  1. Har lecture ke liye ek background "watcher" thread automatic start
     hota hai jaise hi admin link generate karta hai (koi manual click
     zaroori nahi). Ye thread live playlist ko har ~20s poll karta rehta
     hai jab tak live khatam na ho jaaye (#EXT-X-ENDLIST aa jaaye ya link
     hi expire/fail ho jaaye).
  2. Live khatam hote hi — REAL-TIME RECORDING KI ZAROORAT HI NAHI —
     seedha PRO TRICK use karte hain: live ka `index.m3u8` (ya jo bhi
     media playlist filename ho) usko `master.m3u8` se replace karke ek
     naya URL banate hain. Zyaadatar PW/CDN setups mein ye hi poori class
     (start se end tak) ka FULL ARCHIVED VOD playlist hota hai — sliding
     window ka koi issue nahi, poora lecture milta hai. Kaam na kare to
     automatically fallback chain try hoti hai.
  3. Us master playlist ko ek hi shot mein ffmpeg se mp4 mein download
     karte hain (ye already non-realtime hai kyunki playlist ended/finite
     hai — ffmpeg jitni fast ho utni fast segments khींch leta hai).
  4. 480p mein convert karke Telegram par upload karte hain — agar
     TELEGRAM_LOCAL_API_URL configured hai (local Bot API server, MTProto
     based) to usse (koi 50MB limit nahi, 2GB tak) — warna normal remote
     Bot API pe fallback (50MB limit warning ke saath).
  5. `telegram_file_id` + `duration` + `title` MongoDB mein save, status
     READY — File-Store bot isi se turant deliver karta hai, dobara kabhi
     upload nahi hota.

ENV VARS:
  TELEGRAM_BOT_TOKEN     — file store bot ka token
  TELEGRAM_CHAT_ID       — jis chat/channel me upload karna hai
  TELEGRAM_BOT_USERNAME  — deep link ke liye (default: PWSENSEI_FileStoreBot)
  TELEGRAM_LOCAL_API_URL — local Bot API server base (e.g. http://127.0.0.1:8081)
                            Not set => remote https://api.telegram.org use hota
                            hai (50MB upload limit — Telegram ki apni limit hai).
"""

import os
import subprocess
import threading
import time
from urllib.parse import urljoin, urlparse, urlunparse

import requests

from utils.text import display_title

RECORDINGS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recordings")
os.makedirs(RECORDINGS_DIR, exist_ok=True)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
BOT_USERNAME = os.environ.get("TELEGRAM_BOT_USERNAME", "Testbro786Bot")
LOCAL_API_URL = os.environ.get("TELEGRAM_LOCAL_API_URL", "")

# Same header set as main.py's UPSTREAM_HEADERS — kaafi CDN edge nodes
# generic/non-browser requests ko drop/403 kar dete hain.
UPSTREAM_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.pw.live/",
    "Origin": "https://www.pw.live",
}
FFMPEG_HEADERS = "".join(f"{k}: {v}\r\n" for k, v in UPSTREAM_HEADERS.items())

POLL_INTERVAL_SECONDS = 20          # live playlist ko kitni der mein check karein
CONSECUTIVE_FAILS_TO_END = 3        # itni baar lagatar fetch fail = live mar gayi maano
MASTER_RETRY_ATTEMPTS = 9           # master.m3u8 ready hone ka wait (CDN thoda time leta hai)
MASTER_RETRY_DELAY_SECONDS = 10     # ~90s total grace period
MAX_PIPELINE_RETRIES = 3            # pura download+upload pipeline kitni baar retry ho

_active = {}          # name -> {"thread": Thread, "gen": int}
_active_lock = threading.Lock()


# ═══════════════════════════════════════════════════════════════════════
#  Playlist helpers
# ═══════════════════════════════════════════════════════════════════════

def _get_text(url: str, timeout: int = 15) -> str:
    r = requests.get(url, headers=UPSTREAM_HEADERS, timeout=timeout)
    r.raise_for_status()
    return r.text


def _resolve_media_url(original_url: str) -> str:
    """Agar original_url ek MASTER/multivariant playlist hai (variants ki
    list), to pehla variant nikaal ke uska direct media playlist URL do.
    Warna original_url hi media playlist hai — as-is return karo."""
    try:
        body = _get_text(original_url)
    except Exception:
        return original_url
    if "#EXT-X-STREAM-INF" not in body:
        return original_url
    lines = body.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF"):
            for j in range(i + 1, len(lines)):
                nxt = lines[j].strip()
                if nxt and not nxt.startswith("#"):
                    return urljoin(original_url, nxt)
    return original_url


def _looks_ended(body: str) -> bool:
    return "#EXT-X-ENDLIST" in body


def _is_live_ended(media_url: str) -> bool:
    """True agar playlist mein ENDLIST aa gaya YA link hi fetch nahi ho
    raha (expire/close ho gaya — dono live khatam hone ke signal hain)."""
    fails = 0
    for _ in range(CONSECUTIVE_FAILS_TO_END):
        try:
            body = _get_text(media_url, timeout=10)
            if _looks_ended(body):
                return True
            return False  # abhi bhi live hai, ENDLIST nahi mila
        except Exception:
            fails += 1
            time.sleep(2)
    return fails >= CONSECUTIVE_FAILS_TO_END


def _looks_like_valid_m3u8(body: str) -> bool:
    return "#EXTM3U" in body and ("#EXTINF" in body or ".ts" in body or ".m4s" in body)


def _build_master_candidates(media_url: str):
    """PRO TRICK: live media playlist ka filename (jo aksar `index.m3u8`
    hota hai) ko `master.m3u8` se replace karo — zyaadatar CDN setups mein
    yahi poori class (start se end tak) ka full archived VOD playlist hota
    hai, live ke sliding-window wale chhote portion ke bajaye."""
    parsed = urlparse(media_url)
    path = parsed.path
    if "/" in path:
        dirpath, _filename = path.rsplit("/", 1)
    else:
        dirpath, _filename = "", path
    new_path = f"{dirpath}/master.m3u8" if dirpath else "/master.m3u8"

    with_query = urlunparse(parsed._replace(path=new_path))
    no_query = urlunparse(parsed._replace(path=new_path, query=""))

    candidates = [with_query]
    if no_query != with_query:
        candidates.append(no_query)
    return candidates


def _resolve_download_url(media_url: str) -> str:
    """Master VOD playlist dhoondo (with retries — CDN ko archive banane
    mein thoda time lagta hai). Na mile to fallback: original media_url
    (jo ab ENDLIST ke saath hai — jitna window bacha hai wo mil jayega)."""
    candidates = _build_master_candidates(media_url)

    for attempt in range(MASTER_RETRY_ATTEMPTS):
        for cand in candidates:
            try:
                body = _get_text(cand, timeout=12)
                if _looks_like_valid_m3u8(body):
                    print(f"[recorder] master VOD playlist mil gaya: {cand}")
                    return cand
            except Exception:
                pass
        if attempt < MASTER_RETRY_ATTEMPTS - 1:
            time.sleep(MASTER_RETRY_DELAY_SECONDS)

    print("[recorder] master.m3u8 nahi mila — fallback: live playlist ka bacha hua window use karenge")
    return media_url


# ═══════════════════════════════════════════════════════════════════════
#  ffmpeg pipeline
# ═══════════════════════════════════════════════════════════════════════

def _run_ffmpeg(args: list) -> bool:
    try:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", *args],
            timeout=None,
        )
        return proc.returncode == 0
    except Exception as e:
        print(f"[recorder] ffmpeg error: {e}")
        return False


def _probe_duration(path: str):
    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", path,
            ],
            capture_output=True, text=True, timeout=60,
        )
        return float(out.stdout.strip()) if out.stdout.strip() else None
    except Exception:
        return None


def _download_full_video(download_url: str, raw_path: str) -> bool:
    """Ek hi shot mein poora VOD download karo (non-realtime — playlist
    khud finite/ended hai isliye ffmpeg jitni fast ho utni fast kheenchta hai)."""
    return _run_ffmpeg([
        "-y",
        "-headers", FFMPEG_HEADERS,
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_on_network_error", "1",
        "-reconnect_delay_max", "5",
        "-i", download_url,
        "-c", "copy",
        "-movflags", "+faststart",
        raw_path,
    ])


def _make_480p(raw_path: str, out_path: str) -> bool:
    return _run_ffmpeg([
        "-y", "-i", raw_path,
        "-vf", "scale=-2:480",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
        "-c:a", "aac", "-b:a", "96k",
        "-movflags", "+faststart",
        out_path,
    ])


# ═══════════════════════════════════════════════════════════════════════
#  Telegram upload — local Bot API server (preferred) ya remote fallback
# ═══════════════════════════════════════════════════════════════════════

def _caption_for(title: str) -> str:
    clean = display_title(title)
    return f"📝 Titel: {clean}\n\n📥 Upload By♠: @SmartBoy_ApnaMS"


def _upload_to_telegram(path: str, title: str, duration):
    """Video Telegram pe upload karke file_id return karo. None on failure."""
    if not BOT_TOKEN or not CHAT_ID:
        print("[recorder] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing — upload skipped")
        return None

    caption = _caption_for(title)
    base_data = {
        "chat_id": CHAT_ID,
        "caption": caption,
        "supports_streaming": "true",
    }
    if duration:
        base_data["duration"] = int(duration)

    # Local Bot API server set hai to usi base URL pe bhejo (upload limit
    # 50MB ki jagah 2GB) — warna normal remote Bot API. Dono cases mein
    # NORMAL multipart upload use karte hain (files=), taaki ye chahe
    # pw-live-proxy ke SAME container mein chal raha ho ya ek ALAG Render
    # service ke roop mein — dono deployment tareeke se kaam kare
    # (`--local` mode ka local-filesystem-path shortcut sirf same-machine
    # setup mein hi valid hota hai, isliye us par depend nahi karte).
    base_url = LOCAL_API_URL if LOCAL_API_URL else "https://api.telegram.org"
    url = f"{base_url}/bot{BOT_TOKEN}/sendVideo"
    try:
        with open(path, "rb") as f:
            r = requests.post(
                url,
                data=base_data,
                files={"video": f},
                timeout=3600,
            )
        data = r.json()
        if data.get("ok"):
            return data["result"]["video"]["file_id"]
        print(f"[recorder] telegram upload failed ({'local' if LOCAL_API_URL else 'remote'} API): {data}")
    except Exception as e:
        print(f"[recorder] telegram upload error ({'local' if LOCAL_API_URL else 'remote'} API): {e}")
    return None


# ═══════════════════════════════════════════════════════════════════════
#  Pipeline orchestration
# ═══════════════════════════════════════════════════════════════════════

def _set(col, name, **fields):
    col.update_one({"_id": name}, {"$set": fields})


def _current_gen(col, name):
    doc = col.find_one({"_id": name}, {"watch_gen": 1})
    return (doc or {}).get("watch_gen", 0)


def _run_pipeline(name: str, media_url: str, col) -> bool:
    """Live already ended maan ke — download + convert + upload. True on success."""
    raw_path = os.path.join(RECORDINGS_DIR, f"{name}-raw.mp4")
    out_path = os.path.join(RECORDINGS_DIR, f"{name}-480p.mp4")

    _set(col, name, status="PROCESSING")

    download_url = _resolve_download_url(media_url)

    ok = _download_full_video(download_url, raw_path)
    if not ok or not os.path.exists(raw_path) or os.path.getsize(raw_path) == 0:
        print(f"[recorder] {name}: master VOD download failed, trying raw fallback")
        # Agar master trick fail ho gaya download ke time bhi, ek aakhri
        # koshish seedhe original media_url (jo abhi bhi ENDLIST wale
        # bache hue window ke saath hai) se karo.
        if download_url != media_url:
            ok = _download_full_video(media_url, raw_path)
        if not ok or not os.path.exists(raw_path) or os.path.getsize(raw_path) == 0:
            return False

    ok = _make_480p(raw_path, out_path)
    if not ok:
        os.replace(raw_path, out_path)  # fallback: original quality hi rakho

    duration = _probe_duration(out_path)
    file_id = _upload_to_telegram(out_path, name, duration)

    _set(
        col, name,
        status="READY",
        telegram_file_id=file_id,
        duration=duration,
        title=display_title(name),
    )

    if os.path.exists(raw_path) and raw_path != out_path:
        try:
            os.remove(raw_path)
        except OSError:
            pass

    print(f"[recorder] ✅ {name} READY (duration={duration}, file_id={'yes' if file_id else 'no'})")
    return True


def _watch_loop(name: str, original_url: str, my_gen: int, col):
    try:
        _set(col, name, status="LIVE")

        media_url = _resolve_media_url(original_url)

        # ── Poll until live ends (ya is watcher ko supersede kar diya gaya) ──
        while True:
            if _current_gen(col, name) != my_gen:
                return  # naye /api/generate ne isko supersede kar diya
            if _is_live_ended(media_url):
                break
            time.sleep(POLL_INTERVAL_SECONDS)

        if _current_gen(col, name) != my_gen:
            return

        # ── Live end ho chuki — download + upload pipeline (retries ke saath) ──
        for attempt in range(1, MAX_PIPELINE_RETRIES + 1):
            if _current_gen(col, name) != my_gen:
                return
            try:
                if _run_pipeline(name, media_url, col):
                    return
            except Exception as e:
                print(f"[recorder] pipeline error for {name} (attempt {attempt}): {e}")
            if attempt < MAX_PIPELINE_RETRIES:
                time.sleep(15)

        _set(col, name, status="ERROR", error="Download/upload pipeline failed after retries")
        print(f"[recorder] ❌ {name}: pipeline permanently failed")

    finally:
        with _active_lock:
            entry = _active.get(name)
            if entry and entry.get("gen") == my_gen:
                _active.pop(name, None)


def start_recording(name: str, original_url: str, col) -> bool:
    """Background watcher thread start karo (idempotent — agar already
    is naam ke liye active hai to naya nahi banega). Naye /api/generate
    call par main.py isse pehle `watch_gen` bump karega taaki purana
    watcher (agar chal raha ho) khud superseded ho jaaye."""
    with _active_lock:
        existing = _active.get(name)
        if existing and existing["thread"].is_alive():
            return False

        gen = _current_gen(col, name)
        t = threading.Thread(
            target=_watch_loop, args=(name, original_url, gen, col), daemon=True
        )
        _active[name] = {"thread": t, "gen": gen}
        t.start()
        return True


def resume_pending(col):
    """App restart/redeploy hone par jo lectures abhi bhi LIVE/RECORDING/
    PROCESSING status mein atki hui hain unke watcher dobara start karo
    (agar app crash/redeploy ho jaaye to bhi system apne aap continue kare)."""
    try:
        stuck = list(col.find(
            {"status": {"$in": ["LIVE", "RECORDING", "PROCESSING"]}},
            {"_id": 1, "original_url": 1, "status": 1},
        ))
    except Exception as e:
        print(f"[recorder] resume_pending query failed: {e}")
        return

    for doc in stuck:
        name = doc["_id"]
        original_url = doc.get("original_url")
        if not original_url:
            continue
        if doc.get("status") in ("RECORDING", "PROCESSING"):
            # Beech mein process ho raha tha, redeploy se interrupt ho gaya —
            # wapas LIVE maan ke fresh se watch/process karo.
            _set(col, name, status="LIVE")
        col.update_one({"_id": name}, {"$inc": {"watch_gen": 1}})
        start_recording(name, original_url, col)
        print(f"[recorder] resumed watcher for '{name}'")
