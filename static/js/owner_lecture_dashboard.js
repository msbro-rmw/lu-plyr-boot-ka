const toast = document.getElementById("toast");
function showToast(message, duration = 2500) {
  toast.textContent = message;
  toast.classList.remove("hidden");
  setTimeout(() => toast.classList.add("hidden"), duration);
}

function fmtElapsed(totalSeconds) {
  const s = Math.max(0, Math.floor(totalSeconds || 0));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const pad = (n) => String(n).padStart(2, "0");
  return (h ? h + ":" : "") + pad(m) + ":" + pad(sec);
}

const STATUS_LABELS = {
  LIVE: "🔴 LIVE",
  PROCESSING: "⏳ Processing (Downloading & Uploading)",
  READY: "✅ Ready (Watch Online / Download available)",
  ERROR: "❌ Error",
};

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const ta = document.createElement("textarea");
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    document.body.removeChild(ta);
  }
  showToast("Copied ✅!");
}

document.getElementById("backBtn").addEventListener("click", () => {
  window.location.href = "/" + encodeURIComponent(LECTURE_NAME);
});

const infoTime = document.getElementById("infoTime");
const infoStatus = document.getElementById("infoStatus");
const liveLinkText = document.getElementById("liveLinkText");
const originalLinkText = document.getElementById("originalLinkText");
const endLiveBtn = document.getElementById("endLiveBtn");
const alreadyEndedNote = document.getElementById("alreadyEndedNote");

let currentLiveLink = "";
let currentOriginalLink = "";
let currentStatus = "";
let poller = null;

document.getElementById("copyLiveLinkBtn").addEventListener("click", () => {
  if (currentLiveLink) copyText(currentLiveLink);
});
document.getElementById("copyOriginalLinkBtn").addEventListener("click", () => {
  if (currentOriginalLink) copyText(currentOriginalLink);
});

function applyInfo(data) {
  infoTime.textContent = fmtElapsed(data.elapsed_seconds);
  infoStatus.textContent = STATUS_LABELS[data.status] || data.status;
  currentLiveLink = data.live_link || "";
  currentOriginalLink = data.original_link || "";
  liveLinkText.textContent = currentLiveLink || "—";
  originalLinkText.textContent = currentOriginalLink || "—";
  currentStatus = data.status;

  if (data.status === "LIVE") {
    endLiveBtn.classList.remove("hidden");
    endLiveBtn.disabled = false;
    alreadyEndedNote.classList.add("hidden");
  } else {
    endLiveBtn.classList.add("hidden");
    alreadyEndedNote.classList.remove("hidden");
    if (data.status === "READY") {
      alreadyEndedNote.textContent = "This class has already ended and is ready (Watch Online / Download).";
    } else if (data.status === "PROCESSING") {
      alreadyEndedNote.textContent = "This class has already ended — video is being processed right now.";
    } else if (data.status === "ERROR") {
      alreadyEndedNote.textContent = "Processing failed: " + (data.error || "Unknown error.");
    }
  }
}

async function refreshInfo() {
  try {
    const res = await fetch("/api/owner/" + encodeURIComponent(LECTURE_NAME) + "/info");
    if (res.status === 401) {
      window.location.href = "/";
      return;
    }
    const data = await res.json();
    if (data.ok) applyInfo(data);
  } catch (e) {
    /* silent — next poll retries */
  }
}

function startPolling() {
  refreshInfo();
  if (poller) clearInterval(poller);
  poller = setInterval(refreshInfo, 4000);
}
startPolling();

// ── End Live Now: confirmation-key modal ────────────────────────────────
const endLiveKeyModal = document.getElementById("endLiveKeyModal");
const endLiveKeyInput = document.getElementById("endLiveKeyInput");
const endLiveKeyError = document.getElementById("endLiveKeyError");

endLiveBtn.addEventListener("click", () => {
  endLiveKeyInput.value = "";
  endLiveKeyError.classList.add("hidden");
  endLiveKeyModal.classList.remove("hidden");
});

document.getElementById("endLiveBackBtn").addEventListener("click", () => {
  endLiveKeyModal.classList.add("hidden");
});

document.getElementById("endLiveConfirmBtn").addEventListener("click", async () => {
  const val = endLiveKeyInput.value.trim();
  if (!val) {
    endLiveKeyError.textContent = "Confirmation Key likhna zaroori hai.";
    endLiveKeyError.classList.remove("hidden");
    return;
  }
  try {
    const res = await fetch("/api/owner/" + encodeURIComponent(LECTURE_NAME) + "/end-live", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirm_key: val }),
    });
    const data = await res.json();
    if (!data.ok) {
      endLiveKeyError.textContent = "❌ " + (data.error || "Invalid Confirmation Key");
      endLiveKeyError.classList.remove("hidden");
      return;
    }
    endLiveKeyModal.classList.add("hidden");
    showToast("Live Ended ✅ — processing started");
    refreshInfo();
  } catch (e) {
    endLiveKeyError.textContent = "❌ Something went wrong: " + e.message;
    endLiveKeyError.classList.remove("hidden");
  }
});
