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
    const res = await fetch(`${BACKEND_BASE}/health`);
    if (res.ok) {
      statusEl.textContent = "Desktop app connected ✓";
      statusEl.className = "ok";
      return true;
    }
    throw new Error("bad status");
  } catch (e) {
    statusEl.textContent = "Desktop app not running — launch it, then reopen this popup.";
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

function visibleItems() {
  return mediaItems.filter((it) => {
    if (filterSet && !filterSet.has(categoryOf(it.url))) return false;
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
  const checked = mediaItems.filter((i) => i.checked);
  statusEl.textContent = mediaItems.length
    ? `${checked.length} of ${mediaItems.length} selected`
    : "";
  updateToolbar();
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
        tabId: tab.id, filename: tab.title },
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
  const checked = mediaItems.filter((i) => i.checked);
  document.getElementById("btnMerge").disabled =
    !checked.some((i) => categoryOf(i.url) === "m3u8");
  document.getElementById("btnDownload").disabled = !checked.length;
  document.getElementById("btnCopy").disabled = !checked.length;
}

function sendBatch(viaMerge) {
  const checked = mediaItems.filter((i) => i.checked);
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
  const boxes = Array.from(document.querySelectorAll("#filterPanel input[type=checkbox]"));
  const on = new Set(boxes.filter((b) => b.checked).map((b) => b.value));
  filterSet = on.size === boxes.length ? null : on;
  renderMedia();
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
    results = frames.flatMap((f) => f.result || []);
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

  document.getElementById("tabCurrent").addEventListener("click", () => showTab("current"));
  document.getElementById("tabOther").addEventListener("click", () => showTab("other"));

  document.getElementById("btnMerge").addEventListener("click", () => sendBatch(true));
  document.getElementById("btnDownload").addEventListener("click", () => sendBatch(false));
  document.getElementById("btnCopy").addEventListener("click", () => {
    const urls = mediaItems.filter((i) => i.checked).map((i) => i.url);
    if (!urls.length) return;
    navigator.clipboard.writeText(urls.join("\n")).then(() =>
      setStatus(`Copied ${urls.length} URL(s) ✓`));
  });
  document.getElementById("btnToggle").addEventListener("click", () => {
    const allChecked = mediaItems.length > 0 && mediaItems.every((i) => i.checked);
    mediaItems.forEach((i) => { i.checked = !allChecked; });
    renderMedia();
  });
  document.getElementById("btnFilter").addEventListener("click", () => {
    const p = document.getElementById("filterPanel");
    p.style.display = p.style.display === "none" ? "block" : "none";
  });
  document.querySelectorAll("#filterPanel input[type=checkbox]")
    .forEach((b) => b.addEventListener("change", rebuildFilterSet));
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
  currentTab = tab;
  document.getElementById("grabAllBtn").addEventListener("click", () => grabAll(tab));
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
          ? `Sent ${resp.job_ids.length} item(s) to Grabber ✓`
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
