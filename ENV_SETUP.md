# Env Vars Setup — Merged Repo (Live Link Bot + Website + File Store Bot)

Ab is EK repo/service se **do alag Telegram bots** simultaneously chalte hain
(`start.sh` dekho):

1. **Live Link Bot** (`bot.py`, Pyrogram, Python) — website (`main.py`) ke
   SAME process/thread ke andar.
2. **File Store Bot** (`filestore_bot/bot.js`, Node.js) — alag background
   process, `start.sh` isko launch karta hai.

Dono ke env vars **completely separate** hain — kabhi mix mat karo. Dono
**SAME MongoDB** use karte hain (`lectures` collection share hoti hai), isliye
`MONGO_URI` / `MONGO_DB_NAME` dono jagah SAME set karo.

---

## Shared (both sides must use the SAME value)

| Var | Notes |
|---|---|
| `MONGO_URI` | SAME Mongo connection string, dono side. |
| `MONGO_DB_NAME` | SAME DB name, dono side (default `pw_live_system` on Python side). |

## Python side — Website + Live Link Bot (`bot.py`)

| Var | Purpose |
|---|---|
| `PUBLIC_BASE_URL` | Public domain (already existing). |
| `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` | my.telegram.org — Live Link Bot ke liye. |
| `LIVE_BOT_TOKEN` | Live Link Bot ka apna token (BotFather) — **alag** hai File Store bot ke token se. |
| `TELEGRAM_OWNER_ID` | Live Link Bot ka owner numeric id. |
| `FORCE_SUB_CHANNEL` | default `PW_SENSEI`. |
| `TELEGRAM_BOT_TOKEN` | recorder.py — File Store bot ke token se **same** value do (recorder isi token se video sendVideo karta hai). |
| `TELEGRAM_CHAT_ID` | recorder.py upload target chat/channel. |
| `TELEGRAM_BOT_USERNAME` | File Store bot ka `@username` (default `PWSENSEI_FileStoreBot`) — deep-link (`t.me/<username>?start=...`) banane ke liye. |
| `TELEGRAM_LOCAL_API_URL` | optional — local Bot API server (2GB upload limit). |
| `END_LIVE_CONFIRM_KEY` | Owner Dashboard ke "END LIVE NOW" button ki confirmation key. |

## Node side — File Store Bot (`filestore_bot/`)

| Var | Purpose |
|---|---|
| `BOT_TOKEN` | File Store bot ka apna token — **alag** hai `LIVE_BOT_TOKEN` se. |
| `OWNER_ID` | File Store bot ka owner numeric id. |
| `START_IMAGE_URL` | optional. |
| `FILESTORE_PORT` | optional, default `3000` — internal Express port, container ke andar hi use hota hai. |

---

## Kyu dono bots connected hain

`filestore_bot/bot.js` mein already `LectureModel` (collection: `lectures`)
save hai — jab Live Link Bot / website ka `recorder.py` live class end hone
ke baad video ek baar Telegram par upload karta hai, wahi `telegram_file_id`
+ `token` + `status` isi shared `lectures` collection mein save ho jaate hain.
File Store Bot `/start <token>` par isi collection se turant video deliver
kar deta hai — dobara koi upload nahi hota. Isiliye `MONGO_URI` /
`MONGO_DB_NAME` dono side same hona ZAROORI hai.
