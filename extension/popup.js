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

function extOf(url) {
  const m = url.match(/\.([a-z0-9]{1,5})(\?|#|$)/i);
  return m ? m[1].toLowerCase() : "";
}

function categoryOf(url) {
  const e = extOf(url);
  if (["mp4", "m4v", "mov", "avi"].includes(e)) return "mp4";
  if (e === "webm" || e === "mkv") return "webm";
  if (e === "m3u8" || e === "mpd") return "m3u8";
  if (["mp3", "m4a", "aac", "wav", "flac", "opus", "ogg"].includes(e)) return "mp3";
  if (["jpg", "jpeg", "png", "gif", "webp", "avif", "svg", "bmp", "ico"].includes(e)) return "image";
  if (["pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "csv", "txt", "epub"].includes(e)) return "doc";
  if (["zip", "rar", "7z", "tar", "gz"].includes(e)) return "archive";
  if (["exe", "msi", "dmg"].includes(e)) return "programs";
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

function setItems(items, tabRef) {
  // Preserve checkbox state across reloads, keyed by URL.
  const prev = new Map(mediaItems.map((i) => [i.url, i.checked]));
  mediaItems = (items || [])
    .slice()
    .sort((a, b) => (b.timestamp || 0) - (a.timestamp || 0))
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
    const jobs = Object.values(await res.json());
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
  const visible = visibleItems();
  if (!visible.length) {
    list.innerHTML = '<div class="empty">No media detected yet. Play a video first, or use the on-page button.</div>';
  } else {
    for (const it of visible) list.appendChild(rowEl(it));
  }
  const checked = visible.filter((i) => i.checked);
  const hidden = mediaItems.length - visible.length;
  statusEl.textContent = mediaItems.length
    ? `${checked.length} of ${visible.length} selected` + (hidden ? ` (${hidden} hidden by filter)` : "")
    : "";
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

function rowEl(it) {
  const row = document.createElement("div");
  row.className = "mrow";

  const cb = document.createElement("input");
  cb.type = "checkbox";
  cb.checked = it.checked;
  cb.addEventListener("change", () => { it.checked = cb.checked; renderMedia(); });

  const name = document.createElement("span");
  name.className = "mname";
  name.textContent = `${fileName(it.url)} — ${fmtBytes(it.size) || "unknown"}`;

  const acts = document.createElement("span");
  acts.className = "macts";

  // ⬇ — same as the old per-row "Send to Grabber"
  const bDl = document.createElement("button");
  bDl.textContent = "⬇";
  bDl.title = "Send to Grabber";
  bDl.addEventListener("click", () => {
    bDl.textContent = "…";
    const tab = it.tabRef || currentTab || {};
    chrome.runtime.sendMessage(
      { type: "RELAY_DOWNLOAD", url: it.url, pageUrl: tab.url,
        tabId: tab.id,
        filename: /\.(mp4|m4v|mov|webm|mkv|avi|mp3|m4a|aac|wav|flac|m3u8|mpd|pdf|zip|rar|7z|exe|msi|dmg)(\?|#|$)/i.test(it.url)
          ? fileName(it.url) : undefined },
      (resp) => {
        bDl.textContent = "⬇";
        setStatus(resp && resp.ok ? `Sent ${fileName(it.url)} ✓`
                                  : (resp && resp.error) || "Failed");
      }
    );
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
  row.append(cb, name, acts);
  return row;
}

function setStatus(text) {
  document.getElementById("mediaStatus").textContent = text;
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
      items: checked.map(({ url, format_id, target_format }) =>
        ({ url, format_id, target_format })),
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
  });
}

function loadOther() {
  chrome.runtime.sendMessage({ type: "GET_ALL_VIDEOS" }, (resp) => {
    otherTabs = (resp && resp.tabs) || [];
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

function renderGrabChecklist() {
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
  const countEl = document.getElementById("grabCount");
  const toggleBtn = document.getElementById("grabToggleAll");
  const sendBtn = document.getElementById("grabSendBtn");
  const checkedCount = grabItems.filter((i) => i.checked).length;
  countEl.textContent = `${checkedCount} of ${grabItems.length} selected`;
  toggleBtn.textContent = checkedCount === grabItems.length ? "Select none" : "Select all";
  sendBtn.disabled = checkedCount === 0;
  sendBtn.textContent = checkedCount
    ? `Send ${checkedCount} selected to Grabber`
    : "Send selected to Grabber";
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

// ---------------- wiring ----------------

(async () => {
  await checkBackend();
  startRecent();
  setupSiteSwitch();

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
  if (tab) {
    document.getElementById("grabAllBtn").addEventListener("click", () => grabAll(tab));
  }
  document.getElementById("grabToggleAll").addEventListener("click", () => {
    const allChecked = grabItems.every((i) => i.checked);
    grabItems = grabItems.map((i) => ({ ...i, checked: !allChecked }));
    renderGrabChecklist();
  });
  document.getElementById("grabSendBtn").addEventListener("click", () => {
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
  });

  showTab("current");
})();
