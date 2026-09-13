// background.js — Universal Video Grabber
// Responsibilities:
//   1. Sniff network traffic for direct video URLs (mp4/HLS/DASH/etc).
//   2. Keep a per-tab list of detected candidates (used by the popup).
//   3. Relay "download this" requests from the in-page overlay (content.js)
//      to the local desktop app, attaching cookies/referer/UA so yt-dlp
//      can authenticate the same way the browser did.

const BACKEND_BASE = "http://127.0.0.1:5757";

const MEDIA_EXT_REGEX = /\.(mp4|m4v|mov|webm|mkv|m3u8|mpd)(\?|#|$)/i;
const MEDIA_CT_REGEX = /^(video\/|application\/vnd\.apple\.mpegurl|application\/x-mpegurl|application\/dash\+xml|audio\/mpegurl)/i;

// tabId -> Map(url -> item)
const videoMap = new Map();

function addItem(tabId, item) {
  if (tabId === undefined || tabId < 0) return;
  if (!videoMap.has(tabId)) videoMap.set(tabId, new Map());
  const m = videoMap.get(tabId);
  if (!m.has(item.url)) {
    item.timestamp = Date.now();
    m.set(item.url, item);
    chrome.action.setBadgeText({ tabId, text: String(m.size) });
    chrome.action.setBadgeBackgroundColor({ tabId, color: "#2e7d32" });
  }
}

chrome.webRequest.onBeforeRequest.addListener(
  (details) => {
    if (MEDIA_EXT_REGEX.test(details.url)) {
      addItem(details.tabId, { url: details.url, source: "url-pattern", contentType: null });
    }
  },
  { urls: ["<all_urls>"], types: ["media", "xmlhttprequest", "other"] }
);

chrome.webRequest.onHeadersReceived.addListener(
  (details) => {
    const ct = (details.responseHeaders || []).find(
      (h) => h.name.toLowerCase() === "content-type"
    );
    if (ct && MEDIA_CT_REGEX.test(ct.value)) {
      addItem(details.tabId, { url: details.url, source: "content-type", contentType: ct.value });
    }
  },
  { urls: ["<all_urls>"], types: ["media", "xmlhttprequest", "other"] },
  ["responseHeaders", "extraHeaders"]
);

chrome.webNavigation.onBeforeNavigate.addListener((details) => {
  if (details.frameId === 0) {
    videoMap.delete(details.tabId);
    chrome.action.setBadgeText({ tabId: details.tabId, text: "" });
  }
});

chrome.tabs.onRemoved.addListener((tabId) => videoMap.delete(tabId));

async function cookieHeaderFor(url) {
  try {
    const cookies = await chrome.cookies.getAll({ url });
    return cookies.map((c) => `${c.name}=${c.value}`).join("; ");
  } catch (e) {
    return "";
  }
}

async function checkBackend() {
  try {
    const res = await fetch(`${BACKEND_BASE}/health`, { method: "GET" });
    return res.ok;
  } catch (e) {
    return false;
  }
}

async function sendToBackend({ url, pageUrl, filename }) {
  const alive = await checkBackend();
  if (!alive) {
    return { ok: false, error: "Video Grabber app isn't running. Launch the desktop app, then try again." };
  }
  const cookie = await cookieHeaderFor(url).catch(() => "");
  const pageCookie = await cookieHeaderFor(pageUrl).catch(() => "");
  try {
    const res = await fetch(`${BACKEND_BASE}/download`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url,
        referer: pageUrl,
        cookie: cookie || pageCookie,
        user_agent: navigator.userAgent,
        filename: filename || null,
      }),
    });
    const data = await res.json();
    return { ok: res.ok, job_id: data.job_id, error: data.error };
  } catch (e) {
    return { ok: false, error: String(e) };
  }
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === "GET_VIDEOS") {
    const m = videoMap.get(msg.tabId);
    sendResponse({ videos: m ? Array.from(m.values()) : [] });
    return false;
  }

  if (msg.type === "RELAY_DOWNLOAD") {
    // From content.js overlay OR popup.js
    const tabId = sender.tab ? sender.tab.id : msg.tabId;
    addItem(tabId, { url: msg.url, source: "overlay", contentType: null });
    sendToBackend({ url: msg.url, pageUrl: msg.pageUrl, filename: msg.filename }).then(sendResponse);
    return true; // async
  }

  if (msg.type === "SAVE_RECORDING") {
    // content.js captured a blob it couldn't stream to disk itself (rare) —
    // normally content.js just triggers a normal <a download> click for blobs
    // since it already has DOM access; this path is a fallback only.
    sendResponse({ ok: true });
    return false;
  }
});
