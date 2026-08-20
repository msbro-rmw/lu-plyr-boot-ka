#!/bin/sh
# start.sh — SAME Render service/container se DONO bots ek saath chalao:
#   1) File Store Bot (Node.js, filestore_bot/bot.js)         — background
#   2) Live Link Bot + Website (Python, gunicorn -> main:flask_app) — foreground
#
# Dono bots ke apne-apne ALAG env vars honge (BOT_TOKEN/OWNER_ID file-store
# ke liye, LIVE_BOT_TOKEN/TELEGRAM_OWNER_ID live-link bot ke liye — dekho
# ENV_SETUP.md), lekin SAME MongoDB use karte hain (MONGO_URI + MONGO_DB_NAME
# dono jagah same set karo) taaki "lectures" collection share ho aur file
# store bot recorded lecture ka file_id turant deliver kar sake.
#
# Agar filestore_bot ke required env vars (BOT_TOKEN/MONGO_URI/OWNER_ID)
# missing hain to Node process khud fail-fast ho ke exit(1) karega (config.js
# dekho) — is script mein isko background mein chalate hain isliye us case
# mein bhi Python website normal chalta rehta hai, sirf File Store bot side
# disabled rehta hai (koi crash-loop poore container ko nahi girata).
set -e

echo "[start.sh] launching File Store Bot (Node.js) in background…"
( cd filestore_bot && node bot.js >&2 ) &

echo "[start.sh] launching Live Link Bot + Website (gunicorn) in foreground…"
exec gunicorn main:flask_app --bind 0.0.0.0:"${PORT:-8000}" --workers 1 --worker-class gthread --threads 32 --timeout 120
