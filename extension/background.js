// background.js — Universal Video Grabber
// Responsibilities:
//   1. Sniff network traffic for direct video URLs (mp4/HLS/DASH/etc).
//   2. Keep a per-tab list of detected candidates (used by the popup).
//   3. Relay "download this" requests from the in-page overlay (content.js)
//      to the local desktop app, attaching cookies/referer/UA so yt-dlp
//      can authenticate the same way the browser did.

const BACKEND_BASE = "http://127.0.0.1:5757";

const MEDIA_EXT_REGEX = /\.(mp4|m4v|mov|webm|mkv|avi|divx|flv|wmv|mpg|mpeg|ts|m2ts|mts|vob|ogv|rm|rmvb|asf|mxf|3gp|3g2|m3u8|mpd|mp3|m4a|aac|wav|flac|opus|ogg|wma|aiff|aif|alac|ape|m4b|mid|midi|amr|jpe?g|png|gif|webp|avif|heic|heif|bmp|svg|ico|tif|tiff|jxl|psd|raw|dng|cr2|nef|xcf|pdf|epub|mobi|azw3|docx?|xlsx?|pptx?|txt|md|rtf|odt|ods|odp|csv|tsv|json|xml|yaml|yml|zip|rar|7z|tar|gz|tgz|bz2|xz|lz|lzma|zst|cab|iso|img|exe|msi|dmg|deb|rpm|apk|appimage|appx|bat|cmd|sh|ps1|jar|ttf|otf|woff2?|eot)(\?|#|$)/i;
const MEDIA_CT_REGEX = /^(video\/|audio\/|image\/|application\/(vnd\.apple\.mpegurl|x-mpegurl|dash\+xml|pdf|zip|x-7z-compressed|x-rar-compressed|x-tar|x-gzip|x-msdownload|x-msi|vnd\.openxmlformats-officedocument|vnd\.ms-excel|vnd\.ms-powerpoint|msword|epub\+zip)|application\/octet-stream)/i;

// tabId -> Map(dedupKey -> item). The key is the URL minus chunk-range
// query params (see dedupKey); item.url / item.originalUrl keep the URL as it
// was first detected.
const videoMap = new Map();

// 6.4: a chunked stream (e.g. googlevideo) is requested as many URLs that
// differ only in these params. They must not each become a list entry.
const CHUNK_PARAMS = new Set(["range", "rn", "rbum"]);
const MAX_ITEMS_PER_TAB = 200;

function dedupKey(url) {
  try {
    const u = new URL(url);
    const kept = [];
    for (const [k, v] of u.searchParams) {
      if (!CHUNK_PARAMS.has(k.toLowerCase())) kept.push([k, v]);
    }
    const q = new URLSearchParams(kept).toString();
    return u.origin + u.pathname + (q ? "?" + q : "");
  } catch (e) {
    return url;
  }
}

// 6.2: the service worker is torn down after ~30 s idle and the in-memory Map
// goes with it, so the per-tab list is mirrored into chrome.storage.session
// (cleared when the browser closes) and hydrated again on startup.
const VIDEO_MAP_KEY = "videoMapState";
let _persistTimer = null;

function writeVideoMap() {
  try {
    const out = {};
    for (const [tabId, m] of videoMap) {
      if (m.size) out[tabId] = Array.from(m.values());
    }
    return chrome.storage.session.set({ [VIDEO_MAP_KEY]: out }).catch(() => {});
  } catch (e) {
    return Promise.resolve();
  }
}

// Debounced: webRequest can fire hundreds of times a second on a busy page.
function persistVideoMap(immediate) {
  if (immediate) {
    clearTimeout(_persistTimer);
    _persistTimer = null;
    return writeVideoMap();
  }
  if (_persistTimer) return;
  _persistTimer = setTimeout(() => { _persistTimer = null; writeVideoMap(); }, 300);
}

// Resolves once the saved list has been merged in. Message handlers that
// read the list wait on it so a popup opened right after a worker restart
// doesn't see an empty list.
const videoMapReady = (async () => {
  try {
    const st = await chrome.storage.session.get(VIDEO_MAP_KEY);
    const saved = st[VIDEO_MAP_KEY] || {};
    let openTabs = null;
    try { openTabs = new Set((await chrome.tabs.query({})).map((t) => t.id)); } catch (e) {}
    for (const [tabKey, items] of Object.entries(saved)) {
      const tabId = Number(tabKey);
      if (openTabs && !openTabs.has(tabId)) continue;   // tab closed meanwhile
      if (!videoMap.has(tabId)) videoMap.set(tabId, new Map());
      const m = videoMap.get(tabId);
      for (const it of items || []) {
        if (!it || !it.url) continue;
        const key = dedupKey(it.url);
        if (!m.has(key)) m.set(key, it);   // never clobber what was sniffed since startup
      }
      while (m.size > MAX_ITEMS_PER_TAB) m.delete(m.keys().next().value);
    }
  } catch (e) { /* session storage unavailable: start empty */ }
})();

function addItem(tabId, item) {
  if (tabId === undefined || tabId < 0) return;
  if (!videoMap.has(tabId)) videoMap.set(tabId, new Map());
  const m = videoMap.get(tabId);
  const key = dedupKey(item.url);
  if (m.has(key)) {
    // Don't overwrite real data with empty data: keep the existing size /
    // content-type when the new entry lacks them.
    const ex = m.get(key);
    if (!item.size && ex.size) item.size = ex.size;
    if (!item.contentType && ex.contentType) item.contentType = ex.contentType;
    item.timestamp = ex.timestamp;
    // The first-detected URL stays the item's URL (and the original).
    item.url = ex.url;
    item.originalUrl = ex.originalUrl || ex.url;
  } else {
    item.timestamp = Date.now();
    item.originalUrl = item.url;
  }
  m.set(key, item);
  // Cap the per-tab list: Map keeps insertion order, so the oldest go first.
  while (m.size > MAX_ITEMS_PER_TAB) m.delete(m.keys().next().value);
  chrome.action.setBadgeText({ tabId, text: String(m.size) }).catch(() => {});
  chrome.action.setBadgeBackgroundColor({ tabId, color: "#2e7d32" }).catch(() => {});
  persistVideoMap();
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
    persistVideoMap(true);
  }
});

// 6.3: single-page apps change the URL with pushState/replaceState and never
// fire onBeforeNavigate, so the previous "page"'s media piled up in the list.
chrome.webNavigation.onHistoryStateUpdated.addListener((details) => {
  if (details.frameId === 0) {
    videoMap.delete(details.tabId);
    chrome.action.setBadgeText({ tabId: details.tabId, text: "" }).catch(() => {});
    persistVideoMap(true);
  }
});

chrome.tabs.onRemoved.addListener((tabId) => {
  videoMap.delete(tabId);
  persistVideoMap();
});

async function cookieHeaderFor(url) {
  try {
    const cookies = await chrome.cookies.getAll({ url });
    return cookies.map((c) => `${c.name}=${c.value}`).join("; ");
  } catch (e) {
    return "";
  }
}

// Same-host check used to decide whether page cookies may accompany an item.
function sameHost(a, b) {
  try {
    return new URL(a).hostname.toLowerCase() === new URL(b).hostname.toLowerCase();
  } catch (e) {
    return false;
  }
}

// Cookies for an item: always the item's own URL first. The page's cookies
// are only a fallback when the item lives on the page's own host, so session
// cookies never travel to a third-party CDN.
async function cookieHeaderForItem(url, pageUrl) {
  const own = await cookieHeaderFor(url).catch(() => "");
  if (own) return own;
  if (pageUrl && sameHost(url, pageUrl)) {
    return await cookieHeaderFor(pageUrl).catch(() => "");
  }
  return "";
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

// ---- v5.2: copy the app's theme into storage so the pill follows it.
async function refreshTheme() {
  try {
    const { res, unpaired } = await authedFetch("/theme");
    if (unpaired || !res.ok) return;
    await chrome.storage.local.set({ theme: await res.json() });
  } catch (e) { /* app not running: keep the last saved theme */ }
}
refreshTheme();

// ---- v5: site favicon, sent once per domain so the app can show a source icon.
const _faviconTried = new Set();
async function noteFavicon(pageUrl) {
  try {
    const host = new URL(pageUrl).hostname.replace(/^www\./, "").toLowerCase();
    if (!host || _faviconTried.has(host) || /^[\d.:]+$/.test(host)) return;
    _faviconTried.add(host);
    const { faviconSent = {} } = await chrome.storage.local.get("faviconSent");
    if (faviconSent[host]) return;
    const u = new URL(chrome.runtime.getURL("/_favicon/"));
    u.searchParams.set("pageUrl", pageUrl);
    u.searchParams.set("size", "32");
    const r = await fetch(u.toString());
    if (!r.ok) { _faviconTried.delete(host); return; }
    const blob = await r.blob();
    if (blob.type !== "image/png" || blob.size > 60000) return;
    const dataUrl = await new Promise((resolve, reject) => {
      const fr = new FileReader();
      fr.onload = () => resolve(fr.result);
      fr.onerror = () => reject(fr.error);
      fr.readAsDataURL(blob);
    });
    const { res } = await authedFetch("/favicon", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ domain: host, data_url: dataUrl }),
    });
    if (res.ok) {
      faviconSent[host] = Date.now();
      await chrome.storage.local.set({ faviconSent });
    } else {
      _faviconTried.delete(host);
    }
  } catch (e) {
    console.debug("Video Grabber: favicon skipped", e);
  }
}

// Pill / context menu -> the app's Add dialog (or auto-queue if the user
// ticked "don't show again" in the app).
async function showAddDialogFor(msg) {
  const alive = await checkBackend();
  if (!alive) {
    return { ok: false, error: "Video Grabber app isn't running." };
  }
  const cookie = await cookieHeaderForItem(msg.url, msg.pageUrl);
  try {
    const { res, unpaired } = await authedFetch("/show-add-dialog", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url: msg.url,
        filename: msg.filename || null,
        referer: msg.pageUrl,
        page_url: msg.pageUrl,
        cookie: cookie,
        user_agent: navigator.userAgent,
        format_id: msg.format_id || null,
        target_format: msg.target_format || null,
        download_playlist: msg.download_playlist || false,
        pick_playlist: msg.pick_playlist || false,
      }),
    });
    if (unpaired) {
      return { ok: false, error: "Not paired yet." };
    }
    const data = await res.json().catch(() => ({}));
    if (res.ok) noteFavicon(msg.pageUrl || msg.url);
    return { ok: res.ok, job_id: data.job_id, auto_queued: data.auto_queued,
             duplicate_of: data.duplicate_of || null, error: data.error };
  } catch (e) {
    return { ok: false, error: String(e) };
  }
}

// ---- v5: right-click "Download with Video Grabber"
function ensureContextMenus() {
  try {
    chrome.contextMenus.removeAll(() => {
      chrome.contextMenus.create({
        id: "vg-link", title: "Download with Video Grabber",
        contexts: ["link", "video", "audio", "image"],
      });
      chrome.contextMenus.create({
        id: "vg-page", title: "Send this page to Video Grabber",
        contexts: ["page"],
      });
    });
  } catch (e) {}
}
chrome.runtime.onInstalled.addListener(ensureContextMenus);
chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  const url = info.linkUrl || info.srcUrl || info.pageUrl;
  if (!url || /^(blob|data):/i.test(url)) return;
  const resp = await showAddDialogFor({
    url, pageUrl: (tab && tab.url) || info.pageUrl || url,
  });
  if (!resp.ok) {
    try {
      chrome.notifications.create("", {
        type: "basic",
        iconUrl: chrome.runtime.getURL("icons/icon128.png"),
        title: "Video Grabber",
        message: resp.error || "Couldn't send that to the app.",
      });
    } catch (e) {}
  }
});

// ---- E6: keyboard shortcuts (manifest "commands") --------------------------
// download-media (Ctrl+Shift+D): send the active tab's largest detected media
// through the same path as RELAY_DOWNLOAD. open-add-dialog (Ctrl+Shift+A):
// open the app's Add dialog for the tab's page, same path as SHOW_ADD_DIALOG.
// Both read the per-tab list directly: a service worker can't message itself,
// and this is the same data GET_VIDEOS returns.
function commandNotify(message) {
  try {
    chrome.notifications.create("", {
      type: "basic",
      iconUrl: chrome.runtime.getURL("icons/icon128.png"),
      title: "Video Grabber",
      message: String(message || ""),
    });
  } catch (e) {}
}

const MEDIA_ITEM_REGEX = /\.(mp4|m4v|mov|webm|mkv|avi|m3u8|mpd|mp3|m4a|aac|wav|flac|opus|ogg)(\?|#|$)/i;

// Largest item wins (unknown sizes count as 0), newest breaks ties. Pictures,
// fonts and documents only compete when the tab has no audio/video at all.
function pickLargestItem(items) {
  const isMedia = (it) => MEDIA_ITEM_REGEX.test(it.url)
    || /^(video|audio)\//i.test(it.contentType || "")
    || /mpegurl|dash\+xml/i.test(it.contentType || "");
  const media = items.filter(isMedia);
  const pool = media.length ? media : items;
  let best = null;
  for (const it of pool) {
    if (!best
        || (it.size || 0) > (best.size || 0)
        || ((it.size || 0) === (best.size || 0) && (it.timestamp || 0) >= (best.timestamp || 0))) {
      best = it;
    }
  }
  return best;
}

chrome.commands.onCommand.addListener(async (command, tab) => {
  try {
    let t = tab;
    if (!t || !t.id || !t.url) {
      [t] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
    }
    if (!t || !/^https?:/i.test(t.url || "")) {
      commandNotify("Open a web page first.");
      return;
    }
    if (command === "download-media") {
      await videoMapReady;
      const m = videoMap.get(t.id);
      const best = pickLargestItem(m ? Array.from(m.values()) : []);
      if (!best) {
        commandNotify("No media detected on this page yet. Play it first.");
        return;
      }
      const resp = await sendToBackend({ url: best.url, pageUrl: t.url });
      commandNotify(resp.ok ? "Sent to Video Grabber \u2713" : (resp.error || "Couldn't send that to the app."));
    } else if (command === "open-add-dialog") {
      const resp = await showAddDialogFor({ url: t.url, pageUrl: t.url });
      if (!resp.ok) commandNotify(resp.error || "Couldn't open the app's Add dialog.");
    }
  } catch (e) {
    commandNotify("Shortcut failed: " + String(e));
  }
});

async function sendToBackend({ url, pageUrl, filename, format_id, target_format }) {
  const alive = await checkBackend();
  if (!alive) {
    return { ok: false, error: "Video Grabber app isn't running. Launch the desktop app, then try again." };
  }
  const cookie = await cookieHeaderForItem(url, pageUrl);
  try {
    const { res, unpaired } = await authedFetch("/download", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url,
        referer: pageUrl,
        page_url: pageUrl,
        cookie: cookie,
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
    if (res.ok) noteFavicon(pageUrl || url);
    return { ok: res.ok, job_id: data.job_id,
             duplicate_of: data.duplicate_of || null, error: data.error };
  } catch (e) {
    return { ok: false, error: String(e) };
  }
}

// Mirror of the interceptDownloads option so the downloads listener never has
// to await storage before it can cancel. Loaded once per service-worker start
// and kept current by storage.onChanged.
let interceptDownloads = false;
let interceptLoaded = false;
const interceptReady = chrome.storage.local.get("interceptDownloads").then(
  (v) => { interceptDownloads = !!v.interceptDownloads; interceptLoaded = true; },
  () => { interceptLoaded = true; }
);
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && changes.interceptDownloads) {
    interceptDownloads = !!changes.interceptDownloads.newValue;
    interceptLoaded = true;
  }
});

chrome.downloads.onCreated.addListener(async (item) => {
  console.debug("[VG] onCreated fired:", (item.url || "").slice(0, 120),
                "byExtensionId:", item.byExtensionId);
  const t0 = Date.now();
  // Only true on the very first event after a cold service-worker wake,
  // before the startup read above has landed. Normally skipped (no await).
  if (!interceptLoaded) await interceptReady;
  if (!interceptDownloads) return;

  if (!item.url) return;
  if (item.url.startsWith("blob:") || item.url.startsWith("data:")) return;
  if (item.byExtensionId === chrome.runtime.id) return;

  // Every intercepted download opens the app's Add dialog; the app decides
  // what to do with it. Only the backend's own traffic (localhost / raw IP)
  // and the extension's own downloads (guarded above) are skipped. Users
  // who don't want the dialog use skip_add_dialog in the app.
  const isLocalhost = /^https?:\/\/(127\.0\.0\.1|localhost|\[::1\])/i.test(item.url);
  const isIpAddress = /^https?:\/\/\d{1,3}(\.\d{1,3}){3}/.test(item.url);
  if (isLocalhost || isIpAddress) return;

  // Cancel first, everything else after: the browser keeps writing until
  // this runs. Note Chrome only creates the download item once the server
  // has answered, so a slow server delays this event no matter what we do;
  // browserLag below shows how much of any delay happened before we ran.
  // The cancel is the first await. If it fails the download has already
  // finished (the browser won the race), so it must never be re-issued below.
  let cancelled = false;
  try {
    await chrome.downloads.cancel(item.id);
    cancelled = true;
  } catch (e) {
    cancelled = false;
  }
  const browserLag = item.startTime ? t0 - Date.parse(item.startTime) : -1;

  // Filename lookup and cookie fetch are independent: run them together.
  const [filename, cookie] = await Promise.all([
    (async () => {
      try {
        const [full] = await chrome.downloads.search({ id: item.id });
        if (full && full.filename) return full.filename.split(/[\\/]/).pop();
      } catch (e) {}
      return null;
    })(),
    cookieHeaderFor(item.url).catch(() => ""),
  ]);

  let handedOff = false;
  try {
    const { res } = await authedFetch("/show-add-dialog", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url: item.url,
        referer: item.referrer || "",
        page_url: item.referrer || "",
        cookie: cookie,
        user_agent: navigator.userAgent,
        filename: filename,
      }),
    });
    handedOff = res.ok;
  } catch (e) {
    handedOff = false;
  }
  console.debug(`[VG] intercept: browser lag ${browserLag}ms, handoff ${Date.now() - t0}ms, ok=${handedOff} cancelled=${cancelled}`);
  if (handedOff) {
    noteFavicon(item.referrer || item.url);
  } else if (cancelled) {
    // The browser download was already cancelled: give it back if the app
    // rejected the handoff so the user's click never just vanishes. When the
    // cancel failed the browser's copy is complete, so re-issuing would only
    // create a duplicate.
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
      download_playlist: msg.download_playlist || false,
    }).then(sendResponse);
    return true; // async
  }

  if (msg.type === "RELAY_BATCH") {
    // items: [{url, filename}], from the popup's "Grab all media on this page"
    // and media-checklist send buttons. VERIFIED LIVE (popup.js x2 senders) —
    // audit F2 was wrong about this one; retained.
    const items = msg.items || [];
    // Playlist support: a top-level flag applies to items without their own.
    if (msg.download_playlist) {
      for (const it of items) {
        if (it.download_playlist === undefined) it.download_playlist = true;
      }
    }
    (async () => {
      const alive = await checkBackend();
      if (!alive) {
        sendResponse({ ok: false, error: "Video Grabber app isn't running." });
        return;
      }
      // Page cookies go in the top-level field only when every item is on the
      // page's own host. Otherwise the top-level cookie is empty and each
      // item carries cookies fetched for its own URL, so nothing reaches a
      // third-party host.
      const allSameHost = items.every((it) => it && it.url && sameHost(it.url, msg.pageUrl));
      let cookie = "";
      let sendItems = items;
      if (allSameHost) {
        cookie = await cookieHeaderFor(msg.pageUrl).catch(() => "");
      } else {
        sendItems = await Promise.all(items.map(async (it) => ({
          ...it,
          cookie: it && it.url ? await cookieHeaderFor(it.url).catch(() => "") : "",
        })));
      }
      try {
        const { res, unpaired } = await authedFetch("/batch", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            items: sendItems,
            referer: msg.pageUrl,
            page_url: msg.pageUrl,
            cookie,
            user_agent: navigator.userAgent,
            download_playlist: msg.download_playlist || false,
          }),
        });
        if (unpaired) {
          sendResponse({ ok: false, error: "Not paired yet — open the extension's Options page and paste in the pairing token from the desktop app." });
          return;
        }
        const data = await res.json().catch(() => ({}));
        if (res.ok) noteFavicon(msg.pageUrl);
        sendResponse({ ok: res.ok, job_ids: data.job_ids, error: data.error });
      } catch (e) {
        sendResponse({ ok: false, error: String(e) });
      }
    })();
    return true; // async
  }

  if (msg.type === "GET_VIDEOS") {
    // Waits for the session-storage hydration (see videoMapReady).
    videoMapReady.then(() => {
      const m = videoMap.get(msg.tabId);
      sendResponse({ videos: m ? Array.from(m.values()) : [] });
    });
    return true; // async
  }

  if (msg.type === "CLEAR_VIDEOS") {
    videoMapReady.then(() => {
      videoMap.delete(msg.tabId);
      chrome.action.setBadgeText({ tabId: msg.tabId, text: "" }).catch(() => {});
      persistVideoMap(true);
      sendResponse({ ok: true });
    });
    return true; // async
  }

  if (msg.type === "GET_ALL_VIDEOS") {
    // Every tab with detected media: [{id, url, title, items}]. Tabs that
    // have been closed since detection are skipped.
    (async () => {
      await videoMapReady;
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
      // Forward the site's own cookies (url's origin first, page as
      // fallback only when the url is on the page's own host), plus
      // referer and UA, so restricted tweets probe the same way they download.
      const cookie = await cookieHeaderForItem(msg.url, msg.pageUrl);
      try {
        const { res, unpaired } = await authedFetch("/probe-formats", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            url: msg.url,
            referer: msg.pageUrl || msg.url,
            cookie: cookie,
            user_agent: navigator.userAgent,
          }),
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
    showAddDialogFor(msg).then(sendResponse);
    return true; // async
  }

  if (msg.type === "VG_STATE") {
    // Armed-state query for the blob-download hook (content.js). "intercept"
    // is the mirrored interceptDownloads flag; awaiting interceptReady keeps
    // a cold service-worker wake from reporting false before the read lands.
    (async () => {
      try {
        const [alive, token] = await Promise.all([checkBackend(), getApiToken(), interceptReady]);
        sendResponse({ alive: !!alive, paired: !!token, intercept: !!interceptDownloads });
      } catch (e) {
        sendResponse({ alive: false, paired: false, intercept: false });
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