# dashboarddd.zip — kya karna hai

Sirf ye 4 files hain — inhi paths par apne repo mein overwrite karo:

```
bot.py                 (updated — duplicate-reply bug ka REAL fix)
main.py                (updated — naya /api/dashboard-key/verify endpoint)
utils/config.py        (updated — naya DASHBOARD_ACCESS_KEY)
templates/player.html  (updated — 📐 icon ka NO ab Dashboard Key maangta hai)
```

Baaki koi file is zip mein nahi hai — unhe touch mat karna.

## 1) "Bilkul response nahi aa raha" — asli wajah + fix

Pehle wale fix (`app.stop()` sirf clean-shutdown par) se leak to ruk gaya
tha, lekin uske pehle jo excessive session-churn hua tha usne bahut mumkin
hai ki Telegram se **FloodWait** trigger kar diya ho — aur purana retry-loop
sirf max 60s wait karke dobara try karta tha, jo Telegram ke flood-wait ko
**baar-baar violate karke aur lamba** kar deta tha (isiliye "pehle multiple
reply aate the, ab bilkul nahi" — dono ek hi cheez ke do stages hain).

Ab fix kiya:

- **FloodWait ko sahi se handle karta hai** — Telegram jitna wait bolta hai
  (`FloodWait.value` seconds), utna hi EXACT wait karta hai, dobara-dobara
  violate nahi karta. Baaki errors ke liye backoff cap bhi 60s se badha ke
  300s kar diya (gentler retries).
- **Cross-process leader-lock (MongoDB-backed)** — agar Render kabhi bhi
  is app ke >1 process/worker chalaye (jo asli root-cause tha "8-12x
  duplicate reply" ka — same `LIVE_BOT_TOKEN` ke multiple MTProto sessions,
  Telegram har session ko INDEPENDENTLY updates deliver karta hai), to ab
  sirf EK process hi Telegram se connect hota hai; baaki process khud
  chup rehte hain aur har 20s check karte hain (agar leader crash ho jaaye
  to automatically dusra process leader ban jaata hai — heartbeat-based).
- **Better logs** — koi bhi na-pehchana error ab poora traceback Render
  logs mein print karta hai, taaki future mein turant pata chal jaaye.
- Pehle wala duplicate-update guard (dedupe) bhi as-is rakha hai — extra
  safety layer.

**Deploy ke baad zaroor karo:** ek dum fresh redeploy (existing container
ko poora restart), taaki purana koi bhi hanging session clear ho jaaye.
Agar abhi bhi FloodWait active hai (Telegram side se), to logs mein
`⏳ Telegram FloodWait — bilkul Xs wait karenge` dikhega — bas utna wait
karo, khud thik ho jaayega (retry loop apne aap sahi time pe try karega).

## 2) 📐 icon → "NO" → ab Dashboard Key bhi maangta hai

Flow ab aisa hai:

1. 📐 icon tap → **"Are you Sure to open?"** → YES/NO.
2. **YES** = kuch nahi hota (popup band, wahi ka wahi).
3. **NO** = naya popup: **"Sure i will redirect you to owner dashboard —
   Please enter Dashboard key"** + input field + 2 buttons:
   - 🔴 **BACK** (red) — popup band, wapas player page par.
   - 🔵 **GO** (blue) — backend ko key verify karne bhejta hai.
4. Key **sahi** → `/owner/<name>` (Owner Dashboard) par redirect.
5. Key **galat** → bas BACK jaisa hi ho jaata hai (koi hint/error nahi
   dikhta — stealth wahi rehta hai).

Key kahin bhi JS/HTML mein hardcoded NAHI hai — `templates/player.html` sirf
`/api/dashboard-key/verify` (naya endpoint, `main.py`) ko call karta hai,
jo key ko `utils/config.py` ke `DASHBOARD_ACCESS_KEY` se backend par compare
karta hai. Default key: `ToXic-Dash#ViMSPR8m!57QxL7` (chaho to
`DASHBOARD_ACCESS_KEY` env var se override kar sakte ho).

Bonus: is endpoint par basic brute-force throttle bhi hai (8 galat attempts
ke baad wahi IP 5 minute ke liye locked ho jaata hai).
