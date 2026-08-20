# profixed.zip — kya karna hai

Ye sirf **naye + updated** files hain, `lu-plyr-boot-ka` repo ke root mein
inhi paths par extract/overwrite karo (jo files pehle se hain unko replace,
jo naye hain unko add):

```
main.py                                   (updated)
recorder.py                               (updated)
Dockerfile                                (updated)
start.sh                                  (NEW)
ENV_SETUP.md                              (NEW — env vars guide, zaroor padho)
utils/config.py                           (updated)
templates/player.html                     (updated)
templates/owner_lecture_dashboard.html    (NEW)
static/css/style.css                      (updated)
static/js/owner_lecture_dashboard.js      (NEW)
filestore_bot/                            (NEW — puura File Store bot isi ke andar)
```

Baaki koi file (bot.py, utils/db.py, utils/text.py, utils/linkgen.py,
utils/subscription.py, templates/admin.html, templates/generated.html,
static/js/admin.js, static/js/generated.js, requirements.txt) **unchanged**
hai — is zip mein nahi hai, unhe touch mat karna.

## Deploy se pehle

1. `ENV_SETUP.md` padh ke Render → Environment mein saare naye vars set karo
   (khaaskar `filestore_bot` ke `BOT_TOKEN` / `OWNER_ID`, aur `MONGO_URI` +
   `MONGO_DB_NAME` dono side same).
2. `END_LIVE_CONFIRM_KEY` chaho to apna khud ka set kar do (default hardcoded
   value hai, production ke liye change karna better hai).
3. Redeploy — `start.sh` khud dono bots (Node File Store bot + Python
   website/Live Link bot) ek saath launch kar dega.

## Naya kya add hua

- **Player page** (`/<name>`) par bottom-right ek chhota blue 📐 icon —
  tap karo → "Are you Sure to open?" popup → **NO** dabao to secret Owner
  Dashboard khulta hai (**YES** sirf popup band karta hai, kuch nahi hota —
  ye jaanbujh ke ulta rakha hai taaki students ko doubt na ho).
- Owner Dashboard (`/owner/<name>`) — usi admin login session se protected
  hai jo `/login` se banta hai. Isme dikhta hai: class titel, live-elapsed
  timelabel, status, Live Link + Original Link (copy buttons ke saath), aur
  ek bada **"END LIVE NOW"** button — confirmation key maangta hai, phir
  turant (bina URL delete/expire kiye) us waqt tak ke elapsed duration ka
  hi video download → 480p convert → Telegram File Store bot par upload
  karke `READY` bana deta hai.
- Player ka "Watch Online" mode ab green **RECORDED** dot dikhata hai
  (pehle red LIVE dot tha) — ±10s seek, 0.5x–2x speed already the.
- File Store bot ab isi repo ke andar `filestore_bot/` mein hai, alag
  `BOT_TOKEN`/`OWNER_ID` ke saath, lekin website/Live-Link-bot ke SAME
  MongoDB (`lectures` collection) use karke connected hai.
