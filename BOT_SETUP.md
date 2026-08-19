# Telegram Live-Link Generator Bot — Setup

Ye bot isi Flask app ke SAME process/Render service ke andar ek background
thread mein chalta hai (jaise `recorder.py` ke watcher threads chalte hain).
Koi alag service deploy nahi karni.

## Naye Environment Variables (Render → Environment)

| Variable | Description |
|---|---|
| `TELEGRAM_API_ID` | my.telegram.org se apna `api_id` |
| `TELEGRAM_API_HASH` | my.telegram.org se apna `api_hash` |
| `LIVE_BOT_TOKEN` | @BotFather se banaye is naye bot ka token (file-store bot wale `TELEGRAM_BOT_TOKEN` se ALAG hai) |
| `TELEGRAM_OWNER_ID` | Aapki apni numeric Telegram user id (owner-only commands: `/Addauth`, `/rmauth`, `/User`, `/Broadcast`) — apna id `@userinfobot` se nikaal sakte ho |
| `FORCE_SUB_CHANNEL` | (optional) default `PW_SENSEI` — bina `@` ke channel username, jisme join karna users ke liye mandatory hai |

Agar `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` / `LIVE_BOT_TOKEN` set nahi hain,
bot silently start nahi hota — website bilkul normal chalta rehta hai, kuchh
tootega nahi.

**Zaroori:** Bot ko force-subscribe channel (`FORCE_SUB_CHANNEL`) me **admin**
banana zaroori hai, warna membership check fail ho sakta hai.

## Commands

- `/start` — welcome message + Join Channel / Contact Us buttons
- `/Live` — live link generate karne ka poora flow (VIP key → m3u8 URL → titel → confirm)
- `/MyPlan` — apni subscription ki details (kab mili, kab expire hogi)
- `/Addauth <user_id> "<N> <Unit>"` — (owner only) user ko access do. Units: `Day`, `Week`, `Month`, `Year` — e.g. `/Addauth 123456789 "1 Week"`
- `/rmauth <user_id>` — (owner only) access hatao
- `/User` — (owner only) sabhi authorised users ki list + expiry
- `/Broadcast` — (owner only) kisi bhi message/media par reply karke sabhi bot users ko bhejo

## VIP Keys

`/Live` flow ke VIP-key verification step mein wahi teeno keys chalti hain jo
website login ke `VIP KEY` field mein chalti hain (`utils/config.py` mein
`VIP_KEYS` — dono jagah se ek hi jagah maintain hoti hain).

## Data storage

- `bot_auth` (MongoDB) — kisko kab tak access hai
- `bot_users` (MongoDB) — broadcast list (sabhi jo kabhi bot se mile)
- `bot_data.json` (local file, same server) — Mongo ka mirror/backup, taaki
  restart par kuchh na ho (agar Mongo temporarily unreachable ho jaaye to
  bhi data disk par safe rehta hai)
