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
  chrome.action.setBadgeText({ tabId, text: String(m.size) }).catch(() => {});
  chrome.action.setBadgeBackgroundColor({ tabId, color: "#2e7d32" }).catch(() => {});
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
    chrome.action.setBadgeText({ tabId: details.tabId, text: "" }).catch(() => {});
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

let _pairInFlight = null; // module-level guard: concurrent wakes share one pair attempt
async function pairIfNeeded() {
  // Auto-pairing: if no token is stored, ask the desktop app for one.
  // The app only answers on localhost and only once per session.
  const { apiToken } = await chrome.storage.local.get("apiToken");
  if (apiToken) return;
  if (!_pairInFlight) {
    _pairInFlight = (async () => {
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
    })();
    // Let the next wake retry if this attempt failed.
    _pairInFlight = _pairInFlight.then(() => { _pairInFlight = null; },
                                       () => { _pairInFlight = null; });
  }
  return _pairInFlight;
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

  // Task 4: every intercepted download opens the app's Add dialog now —
  // the app is what decides what to do with it, not a hardcoded extension
  // whitelist. Only true internal-app-request exclusions remain: the
  // backend itself (localhost/raw IP — these are the app's own HTTP
  // traffic, not user downloads) and the extension's own downloads
  // (guarded above by byExtensionId). The old KNOWN_EXTS regex and its
  // HEAD-probe-for-attachment-disposition fallback are gone — both only
  // added latency/gaps without changing the user's actual intent, which
  // is "show me the dialog for every download I click". Users who don't
  // want the dialog already have the correct escape hatch server-side via
  // the skip_add_dialog setting (api.py auto-queues instead of prompting);
  // that logic stays in the backend and is not duplicated here.
  const isLocalhost = /^https?:\/\/(127\.0\.0\.1|localhost|\[::1\])/i.test(item.url);
  const isIpAddress = /^https?:\/\/\d{1,3}(\.\d{1,3}){3}/.test(item.url);
  if (isLocalhost || isIpAddress) return;

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
  let handedOff = false;
  try {
    // v4.0.1 Fix 1: intercepted downloads open the Add dialog instead of
    // queueing silently (skip_add_dialog still auto-queues server-side).
    const { res } = await authedFetch("/show-add-dialog", {
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
    handedOff = res.ok;
  } catch (e) {
    handedOff = false;
  }
  if (!handedOff) {
    // The browser download was already cancelled — give it back to the
    // browser if the app rejected the handoff (validation error, unpaired,
    // app busy...) so the user's click never just vanishes.
    try {
      chrome.downloads.download({ url: item.url, filename: filename || undefined });
    } catch (_) {}
  }
});

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
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
    // and media-checklist send buttons. VERIFIED LIVE (popup.js x2 senders) —
    // audit F2 was wrong about this one; retained.
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

  if (msg.type === "GET_VIDEOS") {
    const m = videoMap.get(msg.tabId);
    sendResponse({ videos: m ? Array.from(m.values()) : [] });
    return false;
  }

  if (msg.type === "CLEAR_VIDEOS") {
    videoMap.delete(msg.tabId);
    chrome.action.setBadgeText({ tabId: msg.tabId, text: "" }).catch(() => {});
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

  if (msg.type === "SHOW_ADD_DIALOG") {
    // Pill -> pre-download confirmation dialog in the desktop app.
    (async () => {
      const alive = await checkBackend();
      if (!alive) {
        sendResponse({ ok: false, error: "Video Grabber app isn't running." });
        return;
      }
      const cookie = await cookieHeaderFor(msg.url).catch(() => "");
      const pageCookie = await cookieHeaderFor(msg.pageUrl).catch(() => "");
      try {
        const { res, unpaired } = await authedFetch("/show-add-dialog", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            url: msg.url,
            filename: msg.filename || null,
            referer: msg.pageUrl,
            cookie: cookie || pageCookie,
            user_agent: navigator.userAgent,
            format_id: msg.format_id || null,
            target_format: msg.target_format || null,
          }),
        });
        if (unpaired) {
          sendResponse({ ok: false, error: "Not paired yet." });
          return;
        }
        const data = await res.json().catch(() => ({}));
        sendResponse({ ok: res.ok, job_id: data.job_id,
                       auto_queued: data.auto_queued, error: data.error });
      } catch (e) {
        sendResponse({ ok: false, error: String(e) });
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

  if (msg.type === "SHOW_NOTIFICATION") {
    // v4.0.2 Q1: recorder notifications via chrome.notifications — content
    // scripts can't call it directly, so recNotify() routes through here.
    // The "notifications" permission is declared in manifest.json.
    try {
      chrome.notifications.create("", {
        type: "basic",
        iconUrl: chrome.runtime.getURL("icons/icon128.png"),
        title: String(msg.title || "Video Grabber"),
        message: String(msg.message || ""),
      });
    } catch (e) {}
    sendResponse({ ok: true });
    return false;
  }
});
