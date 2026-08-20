require("dotenv").config(); // Optional: if using a .env file

// NOTE: Secrets (BOT_TOKEN, MONGO_URI) must ONLY come from environment
// variables now — no hardcoded fallback values in source code.
// Reason: a bot token / DB URI hardcoded here and pushed to a public repo
// gets scanned and auto-revoked by Telegram/GitHub secret scanning, which
// is exactly what was causing the repeated "ETELEGRAM: 401 Unauthorized"
// polling_error spam in the logs — the token stopped being valid.
// Set these in your Render dashboard (or a local .env file) instead:
//   BOT_TOKEN=...
//   MONGO_URI=...
//   OWNER_ID=...
//   START_IMAGE_URL=... (optional)
const config = {
  BOT_TOKEN: process.env.BOT_TOKEN || "",
  MONGO_URI: process.env.MONGO_URI || "mongodb+srv://carrombro47_db_user:St7FJBRs0pPYYmt3@cluster0.fp3wrat.mongodb.net/?appName=Cluster0",
  OWNER_ID: Number(process.env.OWNER_ID || "8909902924"),
  START_IMAGE_URL:
    process.env.START_IMAGE_URL ||
    "https://graph.org/file/dabc3b293f0ab07a49eab-f3d1061ff5994e7b50.jpg",
};

// validation — fail fast instead of letting the bot start with a bad/missing
// token and spam "polling_error" every second forever.
const missing = [];
if (!config.BOT_TOKEN) missing.push("BOT_TOKEN");
if (!config.MONGO_URI) missing.push("MONGO_URI");
if (!config.OWNER_ID) missing.push("OWNER_ID");

if (missing.length > 0) {
  console.error(
    `❌ Missing required environment variable(s): ${missing.join(
      ", "
    )}.\nSet them in your Render service's Environment tab (or a local .env file) and restart.`
  );
  process.exit(1);
}

module.exports = config;
