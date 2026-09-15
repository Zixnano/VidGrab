// background.js — Universal Video Grabber
// Responsibilities:
//   1. Sniff network traffic for direct video URLs (mp4/HLS/DASH/etc).
//   2. Keep a per-tab list of detected candidates (used by the popup).
//   3. Relay "download this" requests from the in-page overlay (content.js)
//      to the local desktop app, attaching cookies/referer/UA so yt-dlp
//      can authenticate the same way the browser did.

const BACKEND_BASE = "http://127.0.0.1:5757";

const MEDIA_EXT_REGEX = /\.(mp4|m4v|mov|webm|mkv|avi|m3u8|mpd|mp3|m4a|aac|wav|flac|opus|ogg|jpg|jpeg|png|gif|webp|avif|svg|bmp|ico|pdf|zip|rar|7z|tar|gz|exe|msi|dmg|doc|docx|xls|xlsx|ppt|pptx|csv|txt|epub)(\?|#|$)/i;
const MEDIA_CT_REGEX = /^(video\/|audio\/|image\/|application\/(vnd\.apple\.mpegurl|x-mpegurl|dash\+xml|pdf|zip|x-7z-compressed|x-rar-compressed|x-tar|x-gzip|x-msdownload|x-msi|vnd\.openxmlformats-officedocument|vnd\.ms-excel|vnd\.ms-powerpoint|msword|epub\+zip)|application\/octet-stream)/i;

// tabId -> Map(url -> item)
const videoMap = new Map();

function addItem(tabId, item) {
  if (tabId === undefined || tabId < 0) return;
  if (!videoMap.has(tabId)) videoMap.set(tabId, new Map());
  const m = videoMap.get(tabId);
  if (m.has(item.url)) {
    // Don't overwrite real data with empty data: keep the existing size /
    // content-type when the new entry lacks them.
    const ex = m.get(item.url);
    if (!item.size && ex.size) item.size = ex.size;
    if (!item.contentType && ex.contentType) item.contentType = ex.contentType;
    item.timestamp = ex.timestamp;
  } else {
    item.timestamp = Date.now();
  }
  m.set(item.url, item);
  chrome.action.setBadgeText({ tabId, text: String(m.size) });
  chrome.action.setBadgeBackgroundColor({ tabId, color: "#2e7d32" });
}

chrome.webRequest.onBeforeRequest.addListener(
  (details) => {
    if (MEDIA_EXT_REGEX.test(details.url)) {
      addItem(details.tabId, { url: details.url, source: "url-pattern", contentType: null });
    }
  },
  { urls: ["<all_urls>"], types: ["media", "xmlhttprequest", "other", "image", "font"] }
);

chrome.webRequest.onHeadersReceived.addListener(
  (details) => {
    const headers = details.responseHeaders || [];
    const ct = headers.find((h) => h.name.toLowerCase() === "content-type");
    const cl = headers.find((h) => h.name.toLowerCase() === "content-length");
    const size = cl ? parseInt(cl.value, 10) || 0 : 0;
    if (ct && MEDIA_CT_REGEX.test(ct.value)) {
      addItem(details.tabId, { url: details.url, source: "content-type",
        contentType: ct.value, size });
    } else if (size > 0 && MEDIA_EXT_REGEX.test(details.url)) {
      addItem(details.tabId, { url: details.url, source: "url-pattern",
        contentType: null, size });
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

async function getApiToken() {
  const { apiToken } = await chrome.storage.local.get("apiToken");
  return apiToken || "";
}

async function authedFetch(path, options = {}) {
  const token = await getApiToken();
  const headers = { ...(options.headers || {}), "X-API-Token": token };
  const res = await fetch(`${BACKEND_BASE}${path}`, { ...options, headers });
  if (res.status === 401) {
    return { res, unpaired: true };
  }
  return { res, unpaired: false };
}

async function pairIfNeeded() {
  // Auto-pairing: if no token is stored, ask the desktop app for one.
  // The app only answers on localhost and only once per session.
  const { apiToken } = await chrome.storage.local.get("apiToken");
  if (apiToken) return;
  try {
    const res = await fetch(`${BACKEND_BASE}/pair`, { method: "POST" });
    if (res.ok) {
      const data = await res.json();
      if (data.token) {
        await chrome.storage.local.set({ apiToken: data.token });
        console.debug("Video Grabber: paired automatically");
      }
    } else {
      console.debug("Video Grabber: pair request rejected (", res.status, ")");
    }
  } catch (e) {
    console.debug("Video Grabber: auto-pair failed (app not running?)", e);
  }
}

chrome.runtime.onInstalled.addListener(() => {
  pairIfNeeded();
});

// Service-worker module scope runs on every wake-up — covers the case where
// the app started after the extension was installed.
pairIfNeeded();

async function sendToBackend({ url, pageUrl, filename, format_id, target_format }) {
  const alive = await checkBackend();
  if (!alive) {
    return { ok: false, error: "Video Grabber app isn't running. Launch the desktop app, then try again." };
  }
  const cookie = await cookieHeaderFor(url).catch(() => "");
  const pageCookie = await cookieHeaderFor(pageUrl).catch(() => "");
  try {
    const { res, unpaired } = await authedFetch("/download", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url,
        referer: pageUrl,
        cookie: cookie || pageCookie,
        user_agent: navigator.userAgent,
        filename: filename || null,
        format_id: format_id || null,
        target_format: target_format || null,
      }),
    });
    if (unpaired) {
      return { ok: false, error: "Not paired yet — open the extension's Options page and paste in the pairing token from the desktop app." };
    }
    const data = await res.json().catch(() => ({}));
    return { ok: res.ok, job_id: data.job_id, error: data.error };
  } catch (e) {
    return { ok: false, error: String(e) };
  }
}

chrome.downloads.onCreated.addListener(async (item) => {
  const { interceptDownloads } = await chrome.storage.local.get("interceptDownloads");
  if (!interceptDownloads) return;

  if (!item.url) return;
  if (item.url.startsWith("blob:") || item.url.startsWith("data:")) return;
  if (item.byExtensionId === chrome.runtime.id) return;

  // Only intercept URLs that look like intentional downloads. Without
  // these gates, browser-internal fetches get hijacked (the v3.3 flood).
  const KNOWN_EXTS = /\.(mp4|m4v|mov|webm|mkv|avi|mp3|m4a|wav|flac|zip|rar|7z|tar|gz|pdf|doc|docx|xls|xlsx|ppt|pptx|exe|msi|dmg|iso|epub)(\?|#|$)/i;
  const isLocalhost = /^https?:\/\/(127\.0\.0\.1|localhost|\[::1\])/i.test(item.url);
  const isIpAddress = /^https?:\/\/\d{1,3}(\.\d{1,3}){3}/.test(item.url);
  if (isLocalhost || isIpAddress) return;
  if (!KNOWN_EXTS.test(item.url)) {
    // No recognizable extension — one HEAD probe for an attachment
    // disposition; anything else is left alone.
    try {
      const head = await fetch(item.url, { method: "HEAD" });
      const cd = head.headers.get("content-disposition") || "";
      if (!/attachment/i.test(cd)) return;
    } catch (e) {
      return;
    }
  }

  try {
    chrome.downloads.cancel(item.id);
  } catch (e) {
    return;
  }

  let filename = null;
  try {
    const [full] = await chrome.downloads.search({ id: item.id });
    if (full && full.filename) {
      filename = full.filename.split(/[\\/]/).pop();
    }
  } catch (e) {}

  const cookie = await cookieHeaderFor(item.url).catch(() => "");
  try {
    await authedFetch("/download", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url: item.url,
        referer: item.referrer || "",
        cookie: cookie,
        user_agent: navigator.userAgent,
        filename: filename,
      }),
    });
  } catch (e) {
    try {
      chrome.downloads.download({ url: item.url, filename: filename || undefined });
    } catch (_) {}
  }
});

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === "GET_VIDEOS") {
    const m = videoMap.get(msg.tabId);
    sendResponse({ videos: m ? Array.from(m.values()) : [] });
    return false;
  }

  if (msg.type === "CLEAR_VIDEOS") {
    videoMap.delete(msg.tabId);
    chrome.action.setBadgeText({ tabId: msg.tabId, text: "" });
    sendResponse({ ok: true });
    return false;
  }

  if (msg.type === "GET_ALL_VIDEOS") {
    // Every tab with detected media: [{id, url, title, items}]. Tabs that
    // have been closed since detection are skipped.
    (async () => {
      const out = [];
      for (const [tabId, m] of videoMap) {
        if (!m.size) continue;
        try {
          const tab = await chrome.tabs.get(tabId);
          out.push({ id: tabId, url: tab.url, title: tab.title,
                     items: Array.from(m.values()) });
        } catch (e) { /* tab closed */ }
      }
      sendResponse({ tabs: out });
    })();
    return true; // async
  }

  if (msg.type === "RELAY_DOWNLOAD") {
    // From content.js overlay OR popup.js
    const tabId = sender.tab ? sender.tab.id : msg.tabId;
    addItem(tabId, { url: msg.url, source: "overlay", contentType: null });
    sendToBackend({
      url: msg.url,
      pageUrl: msg.pageUrl,
      filename: msg.filename,
      format_id: msg.format_id,
      target_format: msg.target_format,
    }).then(sendResponse);
    return true; // async
  }

  if (msg.type === "RELAY_BATCH") {
    // items: [{url, filename}], from the popup's "Grab all media on this page"
    const items = msg.items || [];
    (async () => {
      const alive = await checkBackend();
      if (!alive) {
        sendResponse({ ok: false, error: "Video Grabber app isn't running." });
        return;
      }
      const cookie = await cookieHeaderFor(msg.pageUrl).catch(() => "");
      try {
        const { res, unpaired } = await authedFetch("/batch", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            items,
            referer: msg.pageUrl,
            cookie,
            user_agent: navigator.userAgent,
          }),
        });
        if (unpaired) {
          sendResponse({ ok: false, error: "Not paired yet — open the extension's Options page and paste in the pairing token from the desktop app." });
          return;
        }
        const data = await res.json().catch(() => ({}));
        sendResponse({ ok: res.ok, job_ids: data.job_ids, error: data.error });
      } catch (e) {
        sendResponse({ ok: false, error: String(e) });
      }
    })();
    return true; // async
  }

  if (msg.type === "PROBE_FORMATS") {
    // Format-picker probe from the hover pill. Content scripts can't read
    // chrome.storage.local, so the token gets attached here.
    (async () => {
      const alive = await checkBackend();
      if (!alive) {
        sendResponse({ ok: false, error: "Video Grabber app isn't running." });
        return;
      }
      try {
        const { res, unpaired } = await authedFetch("/probe-formats", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ url: msg.url }),
        });
        const data = await res.json().catch(() => ({}));
        sendResponse({ ok: res.ok && !unpaired, formats: data.formats || [],
                       error: unpaired ? "not paired" : data.error });
      } catch (e) {
        sendResponse({ ok: false, formats: [], error: String(e) });
      }
    })();
    return true; // async
  }

  if (msg.type === "GET_UPLOAD_NONCE") {
    // One-time nonce for a direct content-script -> backend upload. The
    // recording itself no longer crosses chrome.runtime.sendMessage (32MB
    // cap truncated long takes and produced corrupt files); only this
    // tiny nonce does.
    (async () => {
      try {
        const { res, unpaired } = await authedFetch("/upload-token", { method: "POST" });
        if (unpaired) {
          sendResponse({ ok: false, error: "not paired" });
          return;
        }
        const data = await res.json();
        sendResponse({ ok: res.ok, nonce: data.nonce, error: data.error });
      } catch (e) {
        sendResponse({ ok: false, error: String(e) });
      }
    })();
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
