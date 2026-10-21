const BACKEND_BASE = "http://127.0.0.1:5757";

// ---------------- helpers ----------------

function fmtBytes(n) {
  if (!n) return "";
  const units = ["B", "KB", "MB", "GB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(n >= 100 || i === 0 ? 0 : 1)} ${units[i]}`;
}

function fileName(url) {
  try {
    return decodeURIComponent(url.split("/").pop().split("?")[0] || url);
  } catch (e) {
    return url;
  }
}

// Reddit's CDN names files m2-res_1280p.mp4 / DASH_720.mp4. For those hosts
// use the tab title instead (same sanitizing as content.js sendChoice).
function redditCdnName(url, tab) {
  try {
    if (!/(^|\.)(v|packaged-media)\.redd\.it$/i.test(new URL(url).hostname)) return undefined;
    const title = tab && tab.title;
    const ext = (new URL(url).pathname.match(/(\.(?:mp4|m4v|mov|webm|mkv|mp3|m4a|aac))$/i) || [""])[0];
    if (!title || !ext) return undefined;
    return title.replace(/[\\/:*?"<>|]/g, "_").slice(0, 80) + ext;
  } catch (e) {
    return undefined;
  }
}

// 7.7: CDN chunk URLs end in a long random ID ("AbC123...xyz.mp4"), which says
// nothing. Show what the item is and how big instead; the full URL goes in
// the tooltip. Normal file names are shown as before.
const CHUNK_ID_RE = /^[A-Za-z0-9_-]{16,}\./;

function chunkLabel(it) {
  const name = fileName(it.url);
  if (!CHUNK_ID_RE.test(name)) return null;
  const ct = (it.contentType || "").toLowerCase();
  const e = extOf(it.url);
  let kind = "Stream";
  if (ct.startsWith("video/") || ["mp4", "m4v", "mov", "webm", "mkv", "avi"].includes(e)) kind = "Video";
  else if (ct.startsWith("audio/") || ["mp3", "m4a", "aac", "wav", "flac", "opus", "ogg"].includes(e)) kind = "Audio";
  return `${kind} \u00b7 ${fmtBytes(it.size) || "unknown"}`;
}

function extOf(url) {
  const m = url.match(/\.([a-z0-9]{1,5})(\?|#|$)/i);
  return m ? m[1].toLowerCase() : "";
}

function categoryOf(url) {
  const e = extOf(url);
  if (["mp4", "m4v", "mov", "avi", "flv", "wmv", "mpg", "mpeg", "ts", "3gp", "3g2", "m2ts", "mts", "vob", "ogv", "rm", "rmvb", "asf", "mxf", "divx"].includes(e)) return "mp4";
  if (e === "webm" || e === "mkv") return "webm";
  if (e === "m3u8" || e === "mpd") return "m3u8";
  if (["mp3", "m4a", "aac", "wav", "flac", "opus", "ogg", "wma", "aiff", "aif", "alac", "ape", "m4b", "mid", "midi", "amr"].includes(e)) return "mp3";
  if (["jpg", "jpeg", "jpe", "png", "gif", "webp", "avif", "heic", "heif", "bmp", "ico", "tif", "tiff", "jxl", "psd", "raw", "dng", "cr2", "nef", "xcf", "svg"].includes(e)) return "image";
  if (["pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "csv", "tsv", "txt", "md", "rtf", "odt", "ods", "odp", "json", "xml", "yaml", "yml", "epub", "mobi", "azw3"].includes(e)) return "doc";
  if (["zip", "rar", "7z", "tar", "gz", "tgz", "bz2", "xz", "lz", "lzma", "zst", "cab", "iso", "img"].includes(e)) return "archive";
  if (["exe", "msi", "dmg", "deb", "rpm", "apk", "appimage", "appx", "bat", "cmd", "sh", "ps1", "jar", "py", "js"].includes(e)) return "programs";
  if (["ttf", "otf", "woff", "woff2", "eot"].includes(e)) return "font";
  return "other";
}

// ---------------- state ----------------

let currentTab = null;
let tabMode = "current";       // "current" | "other"
let otherTabs = [];
let otherSelId = null;
let mediaItems = [];           // rows: {url, size, timestamp, checked, ...}
let filterSet = null;          // null = all visible; else Set of categories
let searchText = "";
let sortMode = "time";         // E5: "time" | "size" | "name" (saved as popupSort)
let _autoTried = false;        // v4.2: single-item auto-download runs once per popup open
const GRAB_CONFIRM_LIMIT = 5;  // v4.2: Grab-all sends above this need a confirmation

// v4.2: small toast at the bottom of the popup.
let _toastTimer = null;
function showToast(text, isErr) {
  const el = document.getElementById("toast");
  if (!el) return;
  el.textContent = text;
  el.classList.toggle("err", !!isErr);
  el.classList.add("show");
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => el.classList.remove("show"), 2200);
}

async function checkBackend() {
  const statusEl = document.getElementById("status");
  try {
    const res = await fetch(`${BACKEND_BASE}/ping`);
    if (!res.ok) throw new Error("bad status");
    const { apiToken } = await chrome.storage.local.get("apiToken");
    if (!apiToken) {
      statusEl.textContent = "App running, but not paired. Open this extension's Options and test the connection.";
      statusEl.className = "bad";
      return true;
    }
    let extra = "";
    try {
      const q = await fetch(`${BACKEND_BASE}/queue-status`, { headers: { "X-API-Token": apiToken } });
      if (q.status === 401) {
        statusEl.textContent = "App running, but the saved token was rejected. Re-pair in Options.";
        statusEl.className = "bad";
        return true;
      }
      if (q.ok) {
        const s = await q.json();
        const bits = [];
        if (s.active) bits.push(`${s.active} downloading`);
        if (s.queued) bits.push(`${s.queued} queued`);
        if (s.youtube_hold_s > 0) bits.push(`YouTube held ${Math.ceil(s.youtube_hold_s / 60)} min`);
        if (s.low_disk) bits.push("low disk space");
        extra = bits.length ? " · " + bits.join(" · ") : "";
      }
    } catch (e) { /* older app without /queue-status: plain status */ }
    statusEl.textContent = "Desktop app connected ✓" + extra;
    statusEl.className = "ok";
    return true;
  } catch (e) {
    statusEl.textContent = "Desktop app not running. Launch it, then reopen this popup.";
    statusEl.className = "bad";
    return false;
  }
}

// ---------------- media list ----------------

// E5: Time is newest first (the original behaviour), Size is largest first
// (unknown sizes last), Name is A to Z.
function compareItems(a, b) {
  const byTime = (b.timestamp || 0) - (a.timestamp || 0);
  if (sortMode === "size") return (b.size || 0) - (a.size || 0) || byTime;
  if (sortMode === "name") {
    return fileName(a.url).toLowerCase().localeCompare(fileName(b.url).toLowerCase()) || byTime;
  }
  return byTime;
}

function updateSortButton() {
  const btn = document.getElementById("btnSort");
  if (btn) btn.textContent = "Sort: " + { time: "Time", size: "Size", name: "Name" }[sortMode];
}

function setItems(items, tabRef) {
  // Preserve checkbox state across reloads, keyed by URL.
  const prev = new Map(mediaItems.map((i) => [i.url, i.checked]));
  mediaItems = (items || [])
    .slice()
    .sort(compareItems)
    .map((i) => ({
      ...i,
      size: i.size || 0,
      checked: prev.has(i.url) ? prev.get(i.url) : true,
      tabRef,
    }));
  renderMedia();
}

// ---- v5: recent downloads (live) and the per-site pill switch -------------
function fmtB(n) {
  if (!n) return "";
  const u = ["B", "KB", "MB", "GB"]; let i = 0; let v = n;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(i ? 1 : 0)} ${u[i]}`;
}

async function refreshRecent() {
  const box = document.getElementById("recent");
  const list = document.getElementById("recentList");
  try {
    const { apiToken } = await chrome.storage.local.get("apiToken");
    if (!apiToken) { box.style.display = "none"; return; }
    const res = await fetch(`${BACKEND_BASE}/jobs`, { headers: { "X-API-Token": apiToken } });
    if (!res.ok) { box.style.display = "none"; return; }
    // Keep the id (the object key) so a row can ask the app to open its folder.
    const jobsObj = await res.json();
    const jobs = Object.entries(jobsObj).map(([key, j]) => ({ ...j, _id: j.id || j.job_id || key }));
    jobs.sort((a, b) => (b.created_ts || 0) - (a.created_ts || 0));
    const recent = jobs.filter((j) => j.status !== "skipped").slice(0, 5);
    if (!recent.length) { box.style.display = "none"; return; }
    list.textContent = "";
    for (const j of recent) {
      const pct = j.size_total ? Math.min(100, Math.round(100 * (j.size_done || 0) / j.size_total))
                               : (j.status === "done" ? 100 : 0);
      const row = document.createElement("div");
      row.className = "rjob" + (j.status === "error" ? " err" : "");
      const name = document.createElement("span");
      name.className = "rname"; name.textContent = j.filename || j.url; name.title = j.filename || "";
      const bar = document.createElement("div");
      bar.className = "rbar";
      const fill = document.createElement("i"); fill.style.width = pct + "%";
      bar.appendChild(fill);
      const meta = document.createElement("div");
      meta.className = "rmeta";
      const stat = document.createElement("span");
      stat.className = "rstat";
      const retrying = j.status === "queued" && (j.retry_after || 0) > Date.now() / 1000;
      stat.textContent = retrying ? "retrying soon" : j.status + (j.status === "downloading" ? ` ${pct}%` : "");
      if (j.status === "done") {
        stat.classList.add("link");
        stat.title = "Open folder";
        stat.addEventListener("click", () => openJobFolder(j));
      }
      const right = document.createElement("span");
      right.textContent = j.status === "downloading" ? (j.speed || "") : fmtB(j.size_total);
      meta.appendChild(stat); meta.appendChild(right);
      row.appendChild(name); row.appendChild(bar); row.appendChild(meta);
      list.appendChild(row);
    }
    box.style.display = "block";
  } catch (e) {
    box.style.display = "none";
  }
}

// v4.2: ask the app to open Explorer at a finished job's file.
async function openJobFolder(j) {
  try {
    const { apiToken } = await chrome.storage.local.get("apiToken");
    const res = await fetch(`${BACKEND_BASE}/open-folder`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-API-Token": apiToken || "" },
      body: JSON.stringify({ job_id: j._id, filename: j.filename || null }),
    });
    if (res.ok) { showToast("Opening folder…"); return; }
    const d = await res.json().catch(() => ({}));
    showToast(d.error || (res.status === 404 ? "Open folder needs a newer Video Grabber app" : "Couldn't open the folder"), true);
  } catch (e) {
    showToast("Desktop app not reachable", true);
  }
}

function startRecent() {
  refreshRecent();
  setInterval(refreshRecent, 1500);
}

async function setupSiteSwitch() {
  const btn = document.getElementById("siteToggle");
  const nameEl = document.getElementById("siteName");
  const row = document.getElementById("siteRow");
  let host = "";
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    host = new URL(tab.url).hostname.replace(/^www\./, "").toLowerCase();
  } catch (e) {}
  if (!host) { row.style.display = "none"; return; }
  nameEl.textContent = host;
  const paint = async () => {
    const { disabledSites = [] } = await chrome.storage.local.get("disabledSites");
    const off = disabledSites.includes(host);
    btn.textContent = off ? "Show pill on this site" : "Hide pill on this site";
  };
  btn.addEventListener("click", async () => {
    const { disabledSites = [] } = await chrome.storage.local.get("disabledSites");
    const next = disabledSites.includes(host)
      ? disabledSites.filter((h) => h !== host) : [...disabledSites, host];
    await chrome.storage.local.set({ disabledSites: next });
    paint();
  });
  paint();
}

const TINY_BYTES = 100 * 1024;

function visibleItems() {
  const hideTiny = document.getElementById("hideTiny");
  const tinyOn = !hideTiny || hideTiny.checked;
  return mediaItems.filter((it) => {
    if (filterSet && !filterSet.has(categoryOf(it.url))) return false;
    // Known-size tiny files are almost always page assets, not downloads.
    // HLS/DASH manifests are tiny by nature, so they are exempt.
    if (tinyOn && it.size > 0 && it.size < TINY_BYTES
        && categoryOf(it.url) !== "m3u8") return false;
    if (searchText) {
      const hay = (it.url + " " + fileName(it.url)).toLowerCase();
      if (!hay.includes(searchText)) return false;
    }
    return true;
  });
}

function renderMedia() {
  const list = document.getElementById("mediaList");
  const statusEl = document.getElementById("mediaStatus");
  list.innerHTML = "";
  // Sizes arrive later (HEAD probes), so the order is re-applied every render.
  mediaItems.sort(compareItems);
  updateSortButton();
  const visible = visibleItems();
  const hidden = mediaItems.length - visible.length;
  if (!visible.length) {
    // 7.4: say why the list is empty. Nothing detected vs. all filtered out.
    const empty = document.createElement("div");
    empty.className = "empty";
    if (!hidden) {
      empty.textContent = "No media on this page.";
    } else {
      empty.append(`${hidden} item(s) hidden by filter \u2014 `);
      const showAll = document.createElement("button");
      showAll.type = "button";
      showAll.className = "linkbtn";
      showAll.textContent = "Show all";
      showAll.addEventListener("click", showAllItems);
      empty.appendChild(showAll);
    }
    list.appendChild(empty);
  } else {
    for (const it of visible) list.appendChild(rowEl(it));
  }
  const checked = visible.filter((i) => i.checked);
  statusEl.textContent = mediaItems.length
    ? `${checked.length} of ${visible.length} selected` + (hidden ? ` (${hidden} hidden by filter)` : "")
    : "";
  // 7.5: nothing visible and nothing hidden: no status line at all.
  statusEl.style.display = (!visible.length && !hidden) ? "none" : "";
  updateToolbar();
  probeUnknownSizes(visible);
}

// HEAD-probe items whose size is unknown (a few at a time) so the tiny-file
// filter and the size labels have real numbers. Extension pages are exempt
// from CORS thanks to host_permissions.
const _sizeProbed = new Set();
async function probeUnknownSizes(items) {
  const todo = items.filter((i) => !i.size && !_sizeProbed.has(i.url)
                                   && /^https?:/i.test(i.url)).slice(0, 25);
  if (!todo.length) return;
  todo.forEach((i) => _sizeProbed.add(i.url));
  let changed = false;
  const queue = todo.slice();
  const worker = async () => {
    while (queue.length) {
      const it = queue.shift();
      try {
        const r = await fetch(it.url, { method: "HEAD", signal: AbortSignal.timeout(6000) });
        const n = parseInt(r.headers.get("content-length") || "0", 10) || 0;
        if (n) { it.size = n; changed = true; }
      } catch (e) { /* leave size unknown */ }
    }
  };
  await Promise.all([worker(), worker(), worker(), worker()]);
  if (changed) renderMedia();
}

// E4: 16x16 favicon of the page the item came from, from the browser's own
// favicon cache (needs the "favicon" permission, already declared). The
// letter tile shows until the image loads and stays if it never does.
function faviconEl(it) {
  const tab = it.tabRef || currentTab || {};
  const pageUrl = tab.url || "";
  let host = "";
  try { host = new URL(pageUrl || it.url).hostname.replace(/^www\./, ""); } catch (e) {}
  const tile = document.createElement("span");
  tile.className = "fav";
  tile.textContent = (host[0] || "?").toUpperCase();
  if (/^https?:/i.test(pageUrl)) {
    try {
      const u = new URL(chrome.runtime.getURL("/_favicon/"));
      u.searchParams.set("pageUrl", pageUrl);
      u.searchParams.set("size", "16");
      const img = document.createElement("img");
      img.alt = "";
      img.width = 16;
      img.height = 16;
      img.addEventListener("load", () => {
        tile.textContent = "";
        tile.classList.add("has-img");
        tile.appendChild(img);
      });
      img.src = u.toString();
    } catch (e) { /* keep the letter tile */ }
  }
  return tile;
}

// v4.2: the per-row send, shared by the row's download button and the
// single-item auto-download. Same message and filename rules as before.
function dlFilename(it, tab) {
  return redditCdnName(it.url, tab) || (/\.(mp4|m4v|mov|webm|mkv|avi|flv|wmv|mpg|mpeg|mp3|m4a|aac|wav|flac|ogg|opus|wma|aiff|m4b|m3u8|mpd|pdf|zip|rar|7z|tar|gz|tgz|bz2|xz|zst|cab|iso|epub|mobi|azw3|docx?|xlsx?|pptx?|txt|md|rtf|odt|ods|odp|csv|tsv|json|xml|yaml|yml|exe|msi|dmg|deb|rpm|apk|appimage|bat|cmd|sh|ps1|jar|jpe?g|png|gif|webp|avif|heic|heif|bmp|ico|tif|tiff|jxl|psd|raw|woff2?|eot|ttf|otf|3g2|3gp|aif|alac|amr|ape|appx|asf|cr2|divx|dng|img|jpe|js|lz|lzma|m2ts|mid|midi|mts|mxf|nef|ogv|py|rm|rmvb|svg|ts|vob|xcf)(\?|#|$)/i.test(it.url)
          ? fileName(it.url) : undefined);
}

function relayOne(it, tab, cb) {
  chrome.runtime.sendMessage(
    { type: "RELAY_DOWNLOAD", url: it.url, pageUrl: tab.url,
      tabId: tab.id, filename: dlFilename(it, tab) },
    cb
  );
}

function rowEl(it) {
  const row = document.createElement("div");
  row.className = "mrow";

  const cb = document.createElement("input");
  cb.type = "checkbox";
  cb.checked = it.checked;
  cb.addEventListener("change", () => { it.checked = cb.checked; renderMedia(); });

  const name = document.createElement("span");
  name.className = "mname";
  name.textContent = chunkLabel(it)
    || `${fileName(it.url)} — ${fmtBytes(it.size) || "unknown"}`;
  name.title = it.url;   // full URL on hover
  // v4.2: mark the top-level HLS playlist so it isn't mistaken for a sub-stream.
  if (extOf(it.url) === "m3u8" && /master/i.test(fileName(it.url))) {
    const tag = document.createElement("span");
    tag.className = "vtag";
    tag.textContent = "[Master]";
    tag.title = "Top-level playlist (contains every quality)";
    name.appendChild(tag);
  }

  const acts = document.createElement("span");
  acts.className = "macts";

  // ⬇ — same as the old per-row "Send to Grabber"
  const bDl = document.createElement("button");
  bDl.textContent = "⬇";
  bDl.title = "Send to Grabber";
  bDl.addEventListener("click", () => {
    bDl.textContent = "…";
    const tab = it.tabRef || currentTab || {};
    relayOne(it, tab, (resp) => {
      bDl.textContent = "⬇";
      setStatus(resp && resp.ok ? `Sent ${fileName(it.url)} ✓`
                                : (resp && resp.error) || "Failed");
    });
  });

  const bOpen = document.createElement("button");
  bOpen.textContent = "▶";
  bOpen.title = "Open in new tab";
  bOpen.addEventListener("click", () => chrome.tabs.create({ url: it.url }));

  const bCopy = document.createElement("button");
  bCopy.textContent = "📋";
  bCopy.title = "Copy URL";
  bCopy.addEventListener("click", () => {
    navigator.clipboard.writeText(it.url).then(() => setStatus("URL copied ✓"));
  });

  acts.append(bDl, bOpen, bCopy);
  row.append(cb, faviconEl(it), name, acts);
  return row;
}

function setStatus(text) {
  const el = document.getElementById("mediaStatus");
  el.textContent = text;
  el.style.display = "";   // a message must show even when the list is empty
}

// "Show all" in the hidden-by-filter message: every type on, small files
// shown, search cleared.
function showAllItems() {
  document.querySelectorAll("#filterPanel input[value]").forEach((b) => {
    if (b.id !== "hideTiny") b.checked = true;
  });
  const tiny = document.getElementById("hideTiny");
  if (tiny) tiny.checked = false;
  const s = document.getElementById("searchInput");
  if (s) s.value = "";
  searchText = "";
  rebuildFilterSet();
}

// ---------------- toolbar ----------------

function updateToolbar() {
  const checked = visibleItems().filter((i) => i.checked);
  document.getElementById("btnMerge").disabled =
    !checked.some((i) => categoryOf(i.url) === "m3u8");
  document.getElementById("btnDownload").disabled = !checked.length;
  document.getElementById("btnDownload").textContent =
    checked.length ? `Download (${checked.length})` : "Download";
  document.getElementById("btnCopy").disabled = !checked.length;
}

function sendBatch(viaMerge) {
  const checked = visibleItems().filter((i) => i.checked);
  if (!checked.length) return;
  if (viaMerge && checked.some((i) => categoryOf(i.url) === "m3u8")) {
    if (!confirm("HLS streams (.m3u8) will be merged into a single file by the desktop app. Continue?")) return;
  }
  const pageUrl = (currentTab && currentTab.url) || "";
  setStatus(`Sending ${checked.length} item(s)…`);
  chrome.runtime.sendMessage(
    { type: "RELAY_BATCH",
      items: checked.map((i) => {
        const { url, format_id, target_format } = i;
        const filename = redditCdnName(url, i.tabRef || currentTab);
        return filename ? { url, format_id, target_format, filename }
                        : { url, format_id, target_format };
      }),
      pageUrl },
    (resp) => {
      setStatus(resp && resp.ok
        ? `Sent ${(resp.job_ids || []).length} item(s) to Grabber ✓`
        : (resp && resp.error) || "Failed to send.");
    }
  );
}

// ---------------- tabs / loading ----------------

function showTab(mode) {
  tabMode = mode;
  document.getElementById("tabCurrent").classList.toggle("active", mode === "current");
  document.getElementById("tabOther").classList.toggle("active", mode === "other");
  document.getElementById("otherTabs").style.display = mode === "other" ? "block" : "none";
  if (mode === "current") loadCurrent();
  else loadOther();
}

async function loadCurrent() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab) { setItems([], null); return; } // tab closed mid-query
  currentTab = tab;
  chrome.runtime.sendMessage({ type: "GET_VIDEOS", tabId: tab.id }, (resp) => {
    setItems((resp && resp.videos) || [], tab);
    if (!_autoTried) { _autoTried = true; maybeAutoDownload(tab); }
  });
}

// v4.2: exactly one visible media item on the current page -> send it right
// away, toast, and close the popup. Runs once per popup open, and never twice
// for the same URL in the same tab (the second open shows the normal list).
async function maybeAutoDownload(tab) {
  try {
    if (tabMode !== "current" || !/^https?:/i.test(tab.url || "")) return;
    const vis = visibleItems();
    if (vis.length !== 1) return;
    const it = vis[0];
    const KEY = "autoSent";
    const st = await chrome.storage.session.get(KEY);
    const sent = st[KEY] || {};
    if (sent[tab.id] === it.url) return;
    sent[tab.id] = it.url;
    await chrome.storage.session.set({ [KEY]: sent });
    showToast(`Sending ${fileName(it.url)}…`);
    relayOne(it, tab, async (resp) => {
      if (resp && resp.ok) {
        showToast(`Sent ${fileName(it.url)} ✓`);
        setTimeout(() => window.close(), 1300);
      } else {
        delete sent[tab.id];
        try { await chrome.storage.session.set({ [KEY]: sent }); } catch (e) {}
        showToast((resp && resp.error) || "Failed to send", true);
      }
    });
  } catch (e) { /* keep the normal list */ }
}

// 7.6: "Other Pages (N)" once other tabs have detected media. The current tab
// is not "other", so it is left out of the list and the count.
function setOtherLabel() {
  document.getElementById("tabOther").textContent =
    otherTabs.length > 0 ? `Other Pages (${otherTabs.length})` : "Other Pages";
}

function fetchOtherTabs(cb) {
  chrome.runtime.sendMessage({ type: "GET_ALL_VIDEOS" }, (resp) => {
    const curId = currentTab && currentTab.id;
    otherTabs = ((resp && resp.tabs) || []).filter((t) => t.id !== curId);
    setOtherLabel();
    if (cb) cb();
  });
}

function loadOther() {
  fetchOtherTabs(() => {
    renderOtherTabs();
    if (otherTabs.length) selectOtherTab(otherTabs[0].id, false);
    else setItems([], null);
  });
}

function renderOtherTabs() {
  const box = document.getElementById("otherTabs");
  box.innerHTML = "";
  if (!otherTabs.length) {
    box.innerHTML = '<div class="empty">No media detected on other tabs.</div>';
    return;
  }
  for (const t of otherTabs) {
    const el = document.createElement("div");
    el.className = "otab" + (t.id === otherSelId ? " sel" : "");
    const title = document.createElement("span");
    title.textContent = t.title || t.url || `Tab ${t.id}`;
    title.style.overflow = "hidden";
    title.style.textOverflow = "ellipsis";
    title.style.whiteSpace = "nowrap";
    title.style.maxWidth = "260px";
    const count = document.createElement("span");
    count.textContent = `${t.items.length} item(s)`;
    el.append(title, count);
    el.addEventListener("click", () => selectOtherTab(t.id, true));
    box.appendChild(el);
  }
}

function selectOtherTab(tabId, activate) {
  otherSelId = tabId;
  renderOtherTabs();
  const t = otherTabs.find((x) => x.id === tabId);
  if (!t) return;
  if (activate) chrome.tabs.update(tabId, { active: true });
  setItems(t.items, { id: t.id, url: t.url, title: t.title });
}

// ---------------- filter / search ----------------

function rebuildFilterSet() {
  const boxes = Array.from(document.querySelectorAll("#filterPanel input[data-type], #filterPanel input[value]"))
    .filter((b) => b.value && b.id !== "hideTiny");
  const on = new Set(boxes.filter((b) => b.checked).map((b) => b.value));
  filterSet = on.size === boxes.length ? null : on;
  const tiny = document.getElementById("hideTiny");
  chrome.storage.local.set({ popupFilter: { types: Array.from(on), hideTiny: tiny ? tiny.checked : true } });
  renderMedia();
}

async function restoreFilter() {
  const { popupFilter } = await chrome.storage.local.get("popupFilter");
  if (!popupFilter || !Array.isArray(popupFilter.types)) return;
  const want = new Set(popupFilter.types);
  document.querySelectorAll("#filterPanel input[value]").forEach((b) => {
    if (b.id !== "hideTiny") b.checked = want.has(b.value);
  });
  const tiny = document.getElementById("hideTiny");
  if (tiny && typeof popupFilter.hideTiny === "boolean") tiny.checked = popupFilter.hideTiny;
}

// ---------------- grab-all (secondary, unchanged feature) ----------------

// Scans the page DOM for downloadable-looking links and media sources.
// Runs inside the page via chrome.scripting.executeScript — keep it
// self-contained (no outside references).
function scanPageForDownloads() {
  const EXT_RE = /\.(mp4|m4v|mov|webm|mkv|avi|mp3|wav|flac|m4a|zip|rar|7z|pdf|docx?|pptx?|exe|msi)(\?|#|$)/i;
  const found = new Map();
  document.querySelectorAll("a[href]").forEach((a) => {
    if (EXT_RE.test(a.href)) found.set(a.href, a.textContent.trim() || a.href);
  });
  document.querySelectorAll("video, audio").forEach((el) => {
    if (el.currentSrc && !el.currentSrc.startsWith("blob:")) found.set(el.currentSrc, document.title);
    el.querySelectorAll("source[src]").forEach((s) => {
      if (s.src && !s.src.startsWith("blob:")) found.set(s.src, document.title);
    });
  });
  return Array.from(found.entries()).map(([url, filename]) => ({ url, filename }));
}

let grabItems = [];

// v4.2: "Queue X items? [Yes] [Cancel]" for big sends.
function hideGrabConfirm() {
  const box = document.getElementById("grabConfirm");
  if (box) box.style.display = "none";
  const sendBtn = document.getElementById("grabSendBtn");
  if (sendBtn) sendBtn.style.display = "block";
}

function showGrabConfirm(count, onYes) {
  document.getElementById("grabConfirmText").textContent = `Queue ${count} items?`;
  document.getElementById("grabSendBtn").style.display = "none";
  document.getElementById("grabConfirm").style.display = "flex";
  document.getElementById("grabConfirmYes").onclick = () => { hideGrabConfirm(); onYes(); };
  document.getElementById("grabConfirmNo").onclick = hideGrabConfirm;
}

function renderGrabChecklist() {
  hideGrabConfirm();
  const checklist = document.getElementById("grabChecklist");
  const toolbar = document.getElementById("grabToolbar");
  const sendBtn = document.getElementById("grabSendBtn");
  checklist.innerHTML = "";
  if (!grabItems.length) {
    toolbar.style.display = "none";
    sendBtn.style.display = "none";
    return;
  }
  toolbar.style.display = "flex";
  sendBtn.style.display = "block";
  grabItems.forEach((item, idx) => {
    const row = document.createElement("div");
    row.className = "grab-item";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = item.checked;
    cb.addEventListener("change", () => {
      grabItems[idx].checked = cb.checked;
      updateGrabToolbar();
    });
    const name = document.createElement("span");
    name.className = "name";
    name.textContent = item.filename && item.filename !== item.url
      ? `${item.filename} — ${item.url.length > 60 ? item.url.slice(0, 60) + "…" : item.url}`
      : (item.url.length > 90 ? item.url.slice(0, 90) + "…" : item.url);
    row.appendChild(cb);
    row.appendChild(name);
    checklist.appendChild(row);
  });
  updateGrabToolbar();
}

function updateGrabToolbar() {
  hideGrabConfirm();
  const countEl = document.getElementById("grabCount");
  const sendBtn = document.getElementById("grabSendBtn");
  const checkedCount = grabItems.filter((i) => i.checked).length;
  countEl.textContent = `${checkedCount} of ${grabItems.length} selected`;
  sendBtn.disabled = checkedCount === 0;
  sendBtn.textContent = checkedCount
    ? `Download Selected (${checkedCount})`
    : "Download Selected";
}

async function grabAll(tab) {
  const resultEl = document.getElementById("grabResult");
  resultEl.textContent = "Scanning page…";
  grabItems = [];
  renderGrabChecklist();
  let results;
  try {
    const frames = await chrome.scripting.executeScript({
      target: { tabId: tab.id, allFrames: true },
      func: scanPageForDownloads,
    });
    // Same URL can surface from several frames (iframe + parent) — dedupe.
    const seen = new Set();
    results = frames.flatMap((f) => f.result || []).filter((r) => {
      if (seen.has(r.url)) return false;
      seen.add(r.url);
      return true;
    });
  } catch (e) {
    resultEl.textContent = "Couldn't scan this page.";
    return;
  }
  if (!results || !results.length) {
    resultEl.textContent = "No downloadable links found on this page.";
    return;
  }
  grabItems = results.map((r) => ({ ...r, checked: true }));
  resultEl.textContent = `Found ${results.length} item(s) — choose which to send below.`;
  renderGrabChecklist();
}

// ---------------- v4.3: overflow menu (the "..." button) ----------------
// Merge, Copy, Toggle, Sort, Filter, Clear and Search live in #moreMenu. They
// are the same buttons with the same ids and handlers; this only opens and
// closes the menu (click, Escape, arrow keys, click outside).
function setupOverflowMenu() {
  const btn = document.getElementById("btnMore");
  const menu = document.getElementById("moreMenu");
  const items = () => Array.from(menu.querySelectorAll("button")).filter((b) => !b.disabled);
  const close = (refocus) => {
    menu.hidden = true;
    btn.setAttribute("aria-expanded", "false");
    if (refocus) btn.focus();
  };
  const open = () => {
    menu.hidden = false;
    btn.setAttribute("aria-expanded", "true");
    const first = items()[0];
    if (first) first.focus();
  };
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    if (menu.hidden) open(); else close(true);
  });
  // The button's own handler runs first (target phase), then the menu closes.
  menu.addEventListener("click", (e) => {
    if (e.target.closest("button:not(:disabled)")) close(false);
  });
  document.addEventListener("click", (e) => {
    if (!menu.hidden && !menu.contains(e.target) && e.target !== btn) close(false);
  });
  document.addEventListener("keydown", (e) => {
    if (menu.hidden) return;
    if (e.key === "Escape") { e.preventDefault(); close(true); return; }
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const list = items();
      if (!list.length) return;
      const i = list.indexOf(document.activeElement);
      const next = e.key === "ArrowDown" ? (i + 1) % list.length : (i - 1 + list.length) % list.length;
      list[next].focus();
    }
  });
}

// ---------------- v4.3: settings panel (cog) ----------------
// Sections: Pill / Image Micro-Pill / Sniffer / Downloads / Notifications.
// Every control saves straight to chrome.storage.local (key vgSettings,
// see vgsettings.js); content.js and background.js pick the change up
// through storage.onChanged.
async function setupSettings() {
  const $ = (id) => document.getElementById(id);
  const mainView = $("mainView");
  const setView = $("settingsView");
  const showSettings = (on) => {
    mainView.style.display = on ? "none" : "";
    setView.style.display = on ? "block" : "none";
    if (on) paintSites();
  };
  $("btnCog").addEventListener("click", () => showSettings(setView.style.display !== "block"));
  $("setBack").addEventListener("click", () => showSettings(false));

  let cfg = vgNormalize((await chrome.storage.local.get(VG_SETTINGS_KEY))[VG_SETTINGS_KEY]);
  // [element id, setting key, kind]
  const controls = [
    ["setPillVisible", "pillVisible", "bool"],
    ["setMicroPill", "microPill", "bool"],
    ["setMicroPos", "microPillPos", "str"],
    ["setMicroSize", "microPillSize", "bool"],
    ["setIntercept", "interceptDownloads", "bool"],
    ["setCorner", "pillCorner", "str"],
    ["setQuality", "defaultQuality", "num"],
    ["setMenuH", "maxMenuHeight", "num"],
    ["setSniffer", "snifferMode", "str"],
    ["setNotifyRec", "notifyRecording", "bool"],
    ["setNotifyAct", "notifyActions", "bool"],
  ];
  const paint = () => {
    for (const [id, key, kind] of controls) {
      const el = $(id);
      if (kind === "bool") el.checked = !!cfg[key];
      else el.value = String(cfg[key]);
    }
  };
  for (const [id, key, kind] of controls) {
    $(id).addEventListener("change", async () => {
      const el = $(id);
      const value = kind === "bool" ? el.checked : kind === "num" ? Number(el.value) : el.value;
      cfg = vgNormalize({ ...cfg, [key]: value });
      try { await chrome.storage.local.set({ [VG_SETTINGS_KEY]: cfg }); }
      catch (e) { showToast("Couldn't save the setting", true); }
      paint();
    });
  }
  paint();

  // Sites the pill is hidden on (the same list the "Hide pill on this site"
  // button edits), each removable here.
  async function paintSites() {
    const box = $("setSites");
    const { disabledSites = [] } = await chrome.storage.local.get("disabledSites");
    box.textContent = "";
    if (!disabledSites.length) {
      const none = document.createElement("div");
      none.className = "none";
      none.textContent = "None. The pill shows on every site.";
      box.appendChild(none);
      return;
    }
    for (const host of disabledSites) {
      const row = document.createElement("div");
      row.className = "site";
      const name = document.createElement("span");
      name.textContent = host;
      const rm = document.createElement("button");
      rm.textContent = "\u00d7";
      rm.title = "Show the pill on " + host;
      rm.setAttribute("aria-label", "Show the pill on " + host);
      rm.addEventListener("click", async () => {
        const cur = (await chrome.storage.local.get("disabledSites")).disabledSites || [];
        await chrome.storage.local.set({ disabledSites: cur.filter((h) => h !== host) });
        paintSites();
      });
      row.append(name, rm);
      box.appendChild(row);
    }
  }
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area === "local" && changes.disabledSites && setView.style.display === "block") paintSites();
  });

  $("setResetPos").addEventListener("click", async () => {
    await chrome.storage.local.remove("pillPrefs");
    showToast("Pill positions reset");
  });
  $("setOptions").addEventListener("click", () => {
    try { chrome.runtime.openOptionsPage(); } catch (e) { showToast("Couldn't open Options", true); }
  });
}

// ---------------- wiring ----------------

(async () => {
  await checkBackend();
  startRecent();
  setupSiteSwitch();
  setupSettings();
  setupOverflowMenu();

  document.getElementById("tabCurrent").addEventListener("click", () => showTab("current"));
  document.getElementById("tabOther").addEventListener("click", () => showTab("other"));

  document.getElementById("btnMerge").addEventListener("click", () => sendBatch(true));
  document.getElementById("btnDownload").addEventListener("click", () => sendBatch(false));
  document.getElementById("btnCopy").addEventListener("click", () => {
    const urls = visibleItems().filter((i) => i.checked).map((i) => i.url);
    if (!urls.length) return;
    navigator.clipboard.writeText(urls.join("\n")).then(() =>
      setStatus(`Copied ${urls.length} URL(s) ✓`));
  });
  document.getElementById("btnToggle").addEventListener("click", () => {
    // Toggle only what the user can see — hidden (filtered/searched-out)
    // items must not get swept into a batch download.
    const vis = visibleItems();
    const allChecked = vis.length > 0 && vis.every((i) => i.checked);
    vis.forEach((i) => { i.checked = !allChecked; });
    renderMedia();
  });
  document.getElementById("btnFilter").addEventListener("click", () => {
    // Read the real state: the panel's initial display comes from CSS, so
    // comparing the inline style made the first click a no-op.
    const p = document.getElementById("filterPanel");
    p.style.display = getComputedStyle(p).display === "none" ? "block" : "none";
  });
  await restoreFilter();
  // E5: restore the saved sort order and let the button cycle it.
  try {
    const { popupSort } = await chrome.storage.local.get("popupSort");
    if (["time", "size", "name"].includes(popupSort)) sortMode = popupSort;
  } catch (e) {}
  updateSortButton();
  document.getElementById("btnSort").addEventListener("click", () => {
    sortMode = sortMode === "time" ? "size" : sortMode === "size" ? "name" : "time";
    chrome.storage.local.set({ popupSort: sortMode });
    renderMedia();
  });
  document.querySelectorAll("#filterPanel input[type=checkbox]")
    .forEach((b) => b.addEventListener("change", rebuildFilterSet));
  rebuildFilterSet();
  document.getElementById("btnClear").addEventListener("click", () => {
    if (!currentTab) return;
    chrome.runtime.sendMessage({ type: "CLEAR_VIDEOS", tabId: currentTab.id }, () => {
      setStatus("List cleared.");
      showTab(tabMode);
    });
  });
  document.getElementById("btnSearch").addEventListener("click", () => {
    const s = document.getElementById("searchInput");
    s.style.display = s.style.display === "none" ? "block" : "none";
    if (s.style.display === "block") s.focus();
    else { s.value = ""; searchText = ""; renderMedia(); }
  });
  document.getElementById("searchInput").addEventListener("input", (e) => {
    searchText = e.target.value.trim().toLowerCase();
    renderMedia();
  });

  // grab-all section
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  currentTab = tab || null;
  fetchOtherTabs();   // fills in the "Other Pages (N)" label up front
  if (tab) {
    document.getElementById("grabAllBtn").addEventListener("click", () => grabAll(tab));
  }
  document.getElementById("grabCheckAll").addEventListener("click", () => {
    grabItems = grabItems.map((i) => ({ ...i, checked: true }));
    renderGrabChecklist();
  });
  document.getElementById("grabUncheckAll").addEventListener("click", () => {
    grabItems = grabItems.map((i) => ({ ...i, checked: false }));
    renderGrabChecklist();
  });
  const sendGrabSelected = () => {
    const resultEl = document.getElementById("grabResult");
    const selected = grabItems.filter((i) => i.checked).map(({ url, filename }) => ({ url, filename }));
    if (!selected.length) return;
    resultEl.textContent = `Sending ${selected.length} item(s) to Grabber…`;
    chrome.runtime.sendMessage(
      { type: "RELAY_BATCH", items: selected, pageUrl: tab.url },
      (resp) => {
        resultEl.textContent = resp && resp.ok
          ? `Sent ${(resp.job_ids || []).length} item(s) to Grabber ✓`
          : (resp && resp.error) || "Failed to send.";
        if (resp && resp.ok) {
          grabItems = [];
          renderGrabChecklist();
        }
      }
    );
  };
  document.getElementById("grabSendBtn").addEventListener("click", () => {
    const n = grabItems.filter((i) => i.checked).length;
    if (n > GRAB_CONFIRM_LIMIT) { showGrabConfirm(n, sendGrabSelected); return; }
    sendGrabSelected();
  });

  showTab("current");
})();