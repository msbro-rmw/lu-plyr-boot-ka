"""
utils/config.py — shared constants.

Web app (main.py) aur Telegram bot (bot.py) dono isi file se OWNER_NAME /
ADMIN_KEYS / VIP_KEYS / PUBLIC_BASE_URL uthaate hain — taaki keys sirf EK
jagah maintain karni padein, dono jagah kabhi out-of-sync na ho.

Yaha kuchh bhi change mat karo jab tak explicitly na bola jaaye — dono
system (website login + bot ka /Live VIP key verification) isi file par
depend karte hain.
"""
import os

# Public domain used in every generated link.
PUBLIC_BASE_URL = os.environ.get(
    "PUBLIC_BASE_URL", "https://live-system-final-test-by-ms-bro.onrender.com"
)

# ─── Server-side Admin Auth (keys never reach the browser) ────────────────
OWNER_NAME = os.environ.get("OWNER_NAME", "ViPvxMS10BRO")
ADMIN_KEYS = ["MS#Admin_R4!xQ8Lp7", "Core$MS_N6v!T2Zk9", "mS@Root_P8#Lm5Qx3"]
VIP_KEYS = ["ToXic#ViPR8m!4QxL7", "tOxic@VipN5v!9ZpK2", "ToXic$ViPX7#rT3Lm8"]

# ─── Per-lecture Owner Dashboard: "END LIVE NOW" confirmation key ─────────
# Player page ke secret 📐 icon se khulne wale Owner Dashboard par "END LIVE
# NOW" dabane par ye key maangi jaati hai (jaisa Live-quuisz repo ke "Delete
# Quiz" flow mein hota hai). Override karne ke liye env var set karo.
END_LIVE_CONFIRM_KEY = os.environ.get("END_LIVE_CONFIRM_KEY", "EndLive#PWSensei$9K2mX")
