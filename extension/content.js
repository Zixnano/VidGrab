// content.js — Universal Video Grabber
// Watches every <video> element on the page (this frame; manifest sets
// all_frames:true so embedded-iframe players are covered too) and shows a
// small floating pill over it, like IDM's "Download this video" button.
//
//   - If the video's currentSrc is a normal http(s) URL -> "Download" sends
//     it to the local desktop app via the background service worker, which
//     attaches cookies/referer so yt-dlp can fetch it at full speed.
//   - If the video's currentSrc is a blob: URL (MSE/DRM-less streaming
//     players) there is no file to hand off — the only option is to record
//     it live with MediaRecorder. The button switches to "Record" for those.

(() => {
  // v4.1.1: never run on the browser's own internal pages (Vivaldi's
  // startpage/welcome/sync, extension pages, devtools). all_frames:true
  // matches some of these as about:blank/data: iframes, and scanning them
  // every 2s froze Vivaldi's UI on startup.
  if (location.protocol === "chrome-extension:" ||
      location.protocol === "vivaldi:" ||
      location.protocol === "about:" ||
      location.protocol === "devtools:") {
    return;
  }

  // Theme colors copied from the app (see background.js refreshTheme).
  let TH = { bg_base: "#0a0a0f", bg_elevated: "#1a1a24", border_hi: "#33333f", text: "#e6e6eb",
             text_muted: "#8a8a95", accent: "#26c6da", success: "#4caf50", error: "#ef5350",
             info: "#42a5f5" };
  try {
    chrome.storage.local.get("theme").then((st) => { if (st.theme) TH = { ...TH, ...st.theme }; }).catch(() => {});
    chrome.storage.onChanged.addListener((c, area) => {
      if (area === "local" && c.theme && c.theme.newValue) TH = { ...TH, ...c.theme.newValue };
    });
  } catch (e) { /* keep defaults */ }

  const HANDLED = new WeakSet();
  const DISMISSED = new WeakSet();
  const RECORDERS = new WeakMap();

  // ---- v5: per-site on/off switch and per-site quality memory -------------
  const SITE = location.hostname.replace(/^www\./, "").toLowerCase();
  let siteDisabled = false;
  const LIVE = [];            // {video, overlay} for every pill we created
  let lastQuality = {};       // host -> {res: "1920x1080"}

  try {
    chrome.storage.local.get(["disabledSites", "lastQuality", "pillPrefs"]).then((st) => {
      siteDisabled = Array.isArray(st.disabledSites) && st.disabledSites.includes(SITE);
      lastQuality = st.lastQuality || {};
      pillPrefs = st.pillPrefs || {};
      migrateLegacyPillOffset();
      syncAllPills();
      if (siteDisabled) removeAllPills(); else scan();
    }).catch(() => {});
    chrome.storage.onChanged.addListener((changes, area) => {
      if (area !== "local") return;
      if (changes.lastQuality) lastQuality = changes.lastQuality.newValue || {};
      if (changes.pillPrefs) {
        pillPrefs = changes.pillPrefs.newValue || {};
        syncAllPills();
      }
      if (changes.disabledSites) {
        const list = changes.disabledSites.newValue;
        const now = Array.isArray(list) && list.includes(SITE);
        if (now === siteDisabled) return;
        siteDisabled = now;
        if (siteDisabled) removeAllPills(); else scan();
      }
    });
  } catch (e) { /* extension context gone: pills simply stay enabled */ }

  function removeAllPills() {
    // v4.1.1: full teardown via each pill's cleanup() — wrap.remove() alone
    // leaked the pill's scroll/resize listeners and its ResizeObserver.
    for (const entry of LIVE.slice()) {
      try { entry.overlay.cleanup(); } catch (e) {}
    }
    LIVE.length = 0;
  }

  // ---- v8: pill prefs (drag offset, snap corner, mini), per site ---------
  // Stored in chrome.storage.local as { [site]: { dx, dy, corner, mini } }.
  // corner: null (free position, uses dx/dy) or "tl" | "tr" | "bl" | "br".
  let pillPrefs = {};
  const PILL_DEFAULT = { dx: 8, dy: 8, corner: null, mini: false };
  const POSITION_CHOICES = [["tl", "Top-Left"], ["tr", "Top-Right"],
                            ["bl", "Bottom-Left"], ["br", "Bottom-Right"]];

  function prefsFor(host) {
    return { ...PILL_DEFAULT, ...(pillPrefs[host] || {}) };
  }

  function savePillPrefs(host, state) {
    pillPrefs = { ...pillPrefs, [host]: { dx: state.dx, dy: state.dy,
                                          corner: state.corner, mini: !!state.mini } };
    try { chrome.storage.local.set({ pillPrefs }); } catch (e) { /* context gone */ }
  }

  // The earlier build kept the drag offset in localStorage; carry it over once.
  function migrateLegacyPillOffset() {
    if (pillPrefs[SITE]) return;
    try {
      const old = JSON.parse(localStorage.getItem(`vg_pill_offset_${location.hostname}`) || "null");
      if (old && typeof old.dx === "number" && typeof old.dy === "number") {
        savePillPrefs(SITE, { ...PILL_DEFAULT, dx: old.dx, dy: old.dy });
      }
    } catch (e) { /* no legacy value */ }
  }

  function syncAllPills() {
    for (const entry of LIVE) {
      try { entry.overlay.syncPrefs(); } catch (e) { /* pill already gone */ }
    }
  }

  let pillStyleDone = false;
  function ensurePillStyle() {
    if (pillStyleDone) return;
    pillStyleDone = true;
    const s = document.createElement("style");
    s.id = "vg-pill-style";
    s.textContent = `
      .vg-pill-mini { padding: 3px 4px !important; gap: 2px !important; border-radius: 999px !important; }
      .vg-pill-mini .vg-pill-label { display: none !important; }
      .vg-pill-mini .vg-pill-btn { padding: 2px 6px !important; }
    `;
    (document.head || document.documentElement).appendChild(s);
  }

  // ---- v8: one small context menu, shared by the pill and the micro-pill --
  // rows: { label, onClick, checked?, indent? } | { header } | { sep: true }
  let pillCtx = null;

  function closePillCtx() {
    if (pillCtx) { pillCtx.remove(); pillCtx = null; }
    document.removeEventListener("mousedown", onCtxOutside, true);
    document.removeEventListener("keydown", onCtxKey, true);
    window.removeEventListener("scroll", closePillCtx, true);
    window.removeEventListener("resize", closePillCtx);
    if (microPill) scheduleMicroHide(null);   // the micro-pill may be waiting to hide
  }

  function onCtxOutside(e) {
    if (pillCtx && !pillCtx.contains(e.target)) closePillCtx();
  }

  function onCtxKey(e) {
    if (e.key === "Escape") closePillCtx();
  }

  function openPillCtx(x, y, rows) {
    closePillCtx();
    const m = document.createElement("div");
    m.id = "vg-pill-ctx";
    m.style.cssText = `
      all: initial; display: block; position: fixed; z-index: 2147483647;
      min-width: 200px; box-sizing: border-box; padding: 6px;
      background: ${TH.bg_elevated}f8; color: ${TH.text};
      border: 1px solid ${TH.border_hi}; border-radius: 10px;
      box-shadow: 0 6px 24px rgba(0,0,0,0.5);
      font: 12px/1.4 -apple-system, Segoe UI, Roboto, sans-serif;
    `;
    for (const row of rows) {
      if (row.sep) {
        const d = document.createElement("div");
        d.style.cssText = `height: 1px; margin: 4px 2px; background: ${TH.border_hi};`;
        m.appendChild(d);
        continue;
      }
      if (row.header) {
        const h = document.createElement("div");
        h.textContent = row.header;
        h.style.cssText = `padding: 4px 10px 2px; color: ${TH.text_muted}; font-size: 11px;`;
        m.appendChild(h);
        continue;
      }
      const el = document.createElement("div");
      el.textContent = row.checked ? "\u2713  " + row.label : row.label;
      el.style.cssText = `
        padding: 6px 10px ${row.indent ? "6px 22px" : ""}; border-radius: 6px;
        cursor: pointer; white-space: nowrap;
      `;
      el.addEventListener("mouseenter", () => { el.style.background = TH.border_hi; });
      el.addEventListener("mouseleave", () => { el.style.background = "transparent"; });
      el.addEventListener("click", (e) => {
        e.stopPropagation();
        closePillCtx();
        row.onClick();
      });
      m.appendChild(el);
    }
    document.documentElement.appendChild(m);
    // Keep it on screen.
    const w = m.offsetWidth, h = m.offsetHeight;
    m.style.left = Math.max(4, Math.min(x, innerWidth - w - 4)) + "px";
    m.style.top = Math.max(4, Math.min(y, innerHeight - h - 4)) + "px";
    pillCtx = m;
    // Listen after this event finishes so the opening click doesn't close it.
    setTimeout(() => {
      if (pillCtx === m) document.addEventListener("mousedown", onCtxOutside, true);
    }, 0);
    document.addEventListener("keydown", onCtxKey, true);
    window.addEventListener("scroll", closePillCtx, true);
    window.addEventListener("resize", closePillCtx);
  }

  function hideOnThisSiteNow() {
    try {
      chrome.storage.local.get("disabledSites").then((st) => {
        const list = Array.isArray(st.disabledSites) ? st.disabledSites : [];
        if (!list.includes(SITE)) chrome.storage.local.set({ disabledSites: [...list, SITE] });
      }).catch(() => {});
    } catch (e) { /* extension context gone */ }
  }

  function rememberQuality(f) {
    if (!f || !f.resolution) return;
    lastQuality[SITE] = { res: f.resolution };
    try { chrome.storage.local.set({ lastQuality }); } catch (e) {}
  }

  // Put the format matching this site's last pick first and star it.
  function orderFormats(formats) {
    const want = lastQuality[SITE] && lastQuality[SITE].res;
    if (!want) return { list: formats, starred: null };
    const i = formats.findIndex((f) => f.resolution === want);
    if (i <= 0) return { list: formats, starred: i === 0 ? formats[0] : null };
    const list = formats.slice();
    const [hit] = list.splice(i, 1);
    list.unshift(hit);
    return { list, starred: hit };
  }

  // Playlist support: a YouTube watch URL carrying &list=... can be sent
  // as a single video, or to the app's playlist picker (pick_playlist) where
  // the user chooses items; each becomes its own job. list= stays in the
  // URL on purpose.
  function isPlaylistUrl(url) {
    try {
      const u = new URL(url);
      return u.searchParams.has("list") &&
             /(^|\.)youtube\.com$/i.test(u.hostname);
    } catch (e) {
      return false;
    }
  }
  // Sites whose players use MSE in a way captureStream can't record — for these,
  // the only viable move is to hand the PAGE URL to yt-dlp (site extractor).
  const MSE_ONLY_HOSTS = [/twitch\.tv$/i];
  const isMseOnlySite = () => MSE_ONLY_HOSTS.some((rx) => rx.test(location.hostname));
  // Sites where yt-dlp's page extractor beats downloading the stream file.
  const EXTRACTOR_HOSTS = [
    /(^|\.)youtube\.com$/i, /(^|\.)youtu\.be$/i, /(^|\.)twitch\.tv$/i,
    /(^|\.)vimeo\.com$/i, /(^|\.)dailymotion\.com$/i, /(^|\.)twitter\.com$/i,
    /(^|\.)x\.com$/i, /(^|\.)reddit\.com$/i, /(^|\.)facebook\.com$/i,
    /(^|\.)instagram\.com$/i, /(^|\.)tiktok\.com$/i, /(^|\.)bilibili\.com$/i,
    /(^|\.)redgifs\.com$/i,
  ];
  const isExtractorSite = () => EXTRACTOR_HOSTS.some((rx) => rx.test(location.hostname));
  // Sites where the player is a blob and there is no format ladder worth a
  // menu: one click sends the page URL. YouTube/Twitch/Vimeo etc. are NOT
  // here on purpose, their menu offers formats and playlist items.
  const ONE_CLICK_HOSTS = [
    /(^|\.)reddit\.com$/i, /(^|\.)redgifs\.com$/i, /(^|\.)tiktok\.com$/i,
    /(^|\.)x\.com$/i, /(^|\.)twitter\.com$/i,
  ];
  const isOneClickSite = () => ONE_CLICK_HOSTS.some((rx) => rx.test(location.hostname));

  // ---- v6: sniffed-stream fallback for blob players -----------------------
  // On extractor sites whose players only expose a blob: URL (Reddit,
  // Redgifs, X/Twitter feeds, TikTok), background.js has usually ALREADY
  // captured the real media URL via webRequest sniffing — that is what the
  // popup's Download button uses. The pill asks for the same list when its
  // menu opens and, when a real media URL is found, offers "Download file"
  // with it instead of only "Send page URL" / recording.
  //
  // Only media hosts the sniffer can trust are considered, and Reddit's
  // tiny "dict-…" metadata blobs are filtered out — they are player
  // internals, not video.
  const SNIFF_MEDIA_HOSTS = [
    /(^|\.)v\.redd\.it$/i, /(^|\.)redgifs\.com$/i,
    /(^|\.)x\.com$/i, /(^|\.)twitter\.com$/i,
    /(^|\.)tiktok\.com$/i,
  ];
  const SNIFF_FILE_REGEX = /\.(mp4|webm|m4v|mov|mkv)(\?|#|$)/i;

  function looksLikeSniffedVideo(url) {
    try {
      const u = new URL(url);
      if (/dict-[0-9a-f]{4,}/i.test(u.href)) return false;
      // HLS/DASH manifests are trusted on any host: background.js already
      // reduced them to the top-level master, and they are the only usable
      // handle on a blob/MSE player outside the extractor sites.
      if (/\.(m3u8|mpd)(\?|#|$)/i.test(u.pathname + u.search)) return true;
      if (!SNIFF_MEDIA_HOSTS.some((rx) => rx.test(u.hostname))) return false;
      // Reddit serves small player-metadata blobs named dict-<hex>… — never
      // media. Real candidates end in a video extension or are HLS/DASH
      // manifests (m3u8 / mpd somewhere in the URL).
      return SNIFF_FILE_REGEX.test(u.href) || /m3u8|mpd/i.test(u.href);
    } catch (e) {
      return false;
    }
  }

  // GET_ALL_VIDEOS (not GET_VIDEOS): a content script can't read chrome.tabs
  // to learn its own tabId, and background.js must stay untouched. That
  // handler already resolves every entry's tab URL server-side, so the pill
  // matches its own address against the list. all_frames:true means an
  // iframe pill sees the TOP frame URL in tab.url, so an exact
  // hostname+pathname match is tried first and a unique origin match is
  // accepted as fallback; ambiguous matches are ignored.
  function sniffedItemsForThisTab(tabs) {
    const norm = (u) => {
      try {
        const x = new URL(u);
        return (x.hostname.replace(/^www\./, "") + x.pathname).toLowerCase();
      } catch (e) { return ""; }
    };
    const here = norm(location.href);
    const byPath = (tabs || []).find((t) => norm(t.url) === here);
    if (byPath) return byPath.items || [];
    let origin = "";
    try { origin = location.origin; } catch (e) {}
    const sameOrigin = (tabs || []).filter((t) => {
      try { return new URL(t.url).origin === origin; } catch (e) { return false; }
    });
    if (sameOrigin.length === 1) return sameOrigin[0].items || [];
    return null;
  }

  // Pick the best candidate: a manifest (m3u8/mpd) beats a single media file
  // (it describes the whole stream, like the popup's pick); otherwise the
  // largest sniffed file wins, then the most recent one.
  function bestSniffedVideo(items) {
    const vids = (items || []).filter((it) => it && it.url && looksLikeSniffedVideo(it.url));
    if (!vids.length) return null;
    const manifests = vids.filter((it) => /m3u8|mpd/i.test(it.url));
    if (manifests.length) return manifests[manifests.length - 1];
    const sized = vids.filter((it) => it.size > 0).sort((a, b) => (b.size || 0) - (a.size || 0));
    if (sized.length) return sized[0];
    return vids[vids.length - 1];
  }

  // chrome.runtime.sendMessage callback pattern (content scripts can't
  // reliably use async/await with it). cb receives the best item or null.
  function querySniffedVideos(cb) {
    try {
      chrome.runtime.sendMessage({ type: "GET_ALL_VIDEOS" }, (resp) => {
        if (chrome.runtime.lastError || !resp) return cb(null);
        const items = sniffedItemsForThisTab(resp.tabs || []);
        cb(items ? bestSniffedVideo(items) : null);
      });
    } catch (e) {
      cb(null);
    }
  }

  function isVisible(el) {
    const r = el.getBoundingClientRect();
    return r.width > 80 && r.height > 60;
  }

  // Single delegated drag handlers for every pill. Installing
  // mousemove/mouseup per wire() leaked a pair of window listeners each
  // time a player was rewired; a long SPA session would accumulate them.
  let _drag = null;
  window.addEventListener("mousemove", (e) => {
    if (!_drag) return;
    const { wrap, video, state, start } = _drag;
    const r = video.getBoundingClientRect();
    const nx = start.dx + (e.clientX - start.x);
    const ny = start.dy + (e.clientY - start.y);
    // Keep the pill inside the viewport.
    state.dx = Math.max(8 - r.left,
      Math.min(nx, innerWidth - r.left - wrap.offsetWidth - 8));
    state.dy = Math.max(8 - r.top,
      Math.min(ny, innerHeight - r.top - wrap.offsetHeight - 8));
    wrap.__vgPosition();
  });
  window.addEventListener("mouseup", () => {
    if (!_drag) return;
    const { wrap, persist } = _drag;
    _drag = null;
    wrap.style.cursor = "grab";
    persist();   // saved to chrome.storage.local (top frame only)
  });

  function makeOverlay(video) {
    const wrap = document.createElement("div");
    wrap.style.cssText = `
      position: fixed; z-index: 2147483647; display: flex; align-items: center;
      gap: 6px; font: 13px/1.2 -apple-system, Segoe UI, Roboto, sans-serif;
      background: ${TH.bg_base}e0; color: ${TH.text}; padding: 6px 8px;
      border: 1px solid ${TH.border_hi};
      border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.4);
      pointer-events: auto; user-select: none;
    `;

    wrap.style.transition = "opacity 150ms ease";
    wrap.style.opacity = "0";

    const btn = document.createElement("button");
    btn.className = "vg-pill-btn";
    btn.style.cssText = `
      display:flex; align-items:center; gap:6px; background:transparent;
      border:1px solid ${TH.border_hi}; color:${TH.text}; border-radius:6px;
      padding:4px 10px; font: inherit; cursor:pointer;
    `;
    const icon = document.createElement("span");
    icon.style.cssText = `color:${TH.accent}; font-size:11px;`;
    icon.textContent = "▶";
    const label = document.createElement("span");
    label.className = "vg-pill-label";
    btn.appendChild(icon);
    btn.appendChild(label);

    const closeBtn = document.createElement("button");
    closeBtn.textContent = "✕";
    closeBtn.title = "Hide";
    closeBtn.style.cssText = `
      background:transparent; border:none; color:${TH.text_muted}; cursor:pointer;
      font-size:12px; padding:2px 4px;
    `;
    closeBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      DISMISSED.add(video);
      // 12.4: fade out, then remove (close).
      wrap.style.opacity = "0";
      setTimeout(() => cleanup(), 150);
    });

    const menu = document.createElement("div");
    menu.style.cssText = `
      position: absolute; top: 100%; left: 0; margin-top: 6px; min-width: 250px;
      background: ${TH.bg_elevated}f8; color: ${TH.text};
      border: 1px solid ${TH.border_hi}; border-radius: 10px;
      box-shadow: 0 6px 24px rgba(0,0,0,0.5);
      display: none; flex-direction: column; padding: 6px;
      font: 12px/1.4 -apple-system, Segoe UI, Roboto, sans-serif;
    `;
    wrap.appendChild(btn);
    wrap.appendChild(closeBtn);
    wrap.appendChild(menu);
    document.documentElement.appendChild(wrap);
    // 12.4: fade the pill in (open); display can't transition, opacity can.
    requestAnimationFrame(() => { wrap.style.opacity = "1"; });

    // Pill state for this site (drag offset, snap corner, mini). Read from the
    // shared pillPrefs map; only the top frame writes it back, because an
    // iframe's pill would otherwise fight the parent's over the same key.
    const isTopFrame = window === window.top;
    const state = prefsFor(SITE);
    function persist() {
      if (isTopFrame) savePillPrefs(SITE, state);
    }
    function applyMini() {
      wrap.classList.toggle("vg-pill-mini", !!state.mini);
    }

    function position() {
      const r = video.getBoundingClientRect();
      const visible = isVisible(video)
        && r.bottom > 0 && r.top < innerHeight
        && r.right > 0 && r.left < innerWidth;
      wrap.style.display = visible ? "flex" : "none";
      // Measured after the display change so right/bottom anchors use the real size.
      let top, left;
      if (state.corner) {
        left = state.corner[1] === "l" ? r.left + 8 : r.right - wrap.offsetWidth - 8;
        top = state.corner[0] === "t" ? r.top + 8 : r.bottom - wrap.offsetHeight - 8;
      } else {
        left = r.left + state.dx;
        top = r.top + state.dy;
      }
      wrap.style.top = Math.max(8, top) + "px";
      wrap.style.left = Math.max(8, left) + "px";
    }
    ensurePillStyle();
    applyMini();
    position();
    wrap.__vgPosition = position;

    const reposition = () => requestAnimationFrame(position);
    window.addEventListener("scroll", reposition, true);
    window.addEventListener("resize", reposition);
    const ro = new ResizeObserver(reposition);
    ro.observe(video);

    // v4.1.1: full teardown — remove the pill's scroll/resize listeners,
    // disconnect its ResizeObserver, detach the DOM node, and unregister
    // it from HANDLED/LIVE. Invoked from the close button, removeAllPills,
    // and the detached-video sweep. Safe to call twice.
    function cleanup() {
      window.removeEventListener("scroll", reposition, true);
      window.removeEventListener("resize", reposition);
      try { ro.disconnect(); } catch (e) {}
      HANDLED.delete(video);
      const idx = LIVE.findIndex((entry) => entry.video === video);
      if (idx >= 0) LIVE.splice(idx, 1);
      wrap.remove();
    }
    // ---- drag to reposition (module-level delegated handlers; see _drag) ----
    wrap.style.cursor = "grab";
    wrap.addEventListener("mousedown", (e) => {
      if (e.button !== 0) return;   // right-click opens the menu, not a drag
      // Ignore drags starting on a button, the close X, or the open menu.
      if (e.target.closest("button") || menu.contains(e.target)) return;
      if (state.corner) {
        // Leaving a snapped corner: start the free drag where the pill actually is.
        const vr = video.getBoundingClientRect();
        const pr = wrap.getBoundingClientRect();
        state.dx = pr.left - vr.left;
        state.dy = pr.top - vr.top;
        state.corner = null;
      }
      _drag = { wrap, video, state, persist,
                start: { x: e.clientX, y: e.clientY, dx: state.dx, dy: state.dy } };
      wrap.style.cursor = "grabbing";
      e.preventDefault();
    });

    wrap.addEventListener("dblclick", (e) => {
      // Double-click the pill body (not a button) resets its position.
      if (e.target.closest("button") || menu.contains(e.target)) return;
      state.dx = PILL_DEFAULT.dx;
      state.dy = PILL_DEFAULT.dy;
      state.corner = null;
      persist();
      position();
    });

    // ---- v8: right-click menu (mini-pill, corner, hide on this site) --------
    wrap.addEventListener("contextmenu", (e) => {
      if (menu.contains(e.target)) return;   // the download menu keeps its own behavior
      e.preventDefault();
      e.stopPropagation();
      openPillCtx(e.clientX, e.clientY, [
        { label: state.mini ? "Switch to Full Pill" : "Switch to Mini-Pill",
          onClick: () => { state.mini = !state.mini; applyMini(); persist(); position(); } },
        { sep: true },
        { header: "Change Position" },
        ...POSITION_CHOICES.map(([id, name]) => ({
          label: name, indent: true, checked: state.corner === id,
          onClick: () => { state.corner = id; persist(); position(); },
        })),
        { sep: true },
        { label: "Hide on this site", onClick: () => hideOnThisSiteNow() },
      ]);
    });

    // Re-read the saved prefs (another tab or a fresh load changed them).
    function syncPrefs() {
      Object.assign(state, prefsFor(SITE));
      applyMini();
      position();
    }

    return { wrap, btn, label, icon, menu, position, cleanup, syncPrefs };
  }

  // v5: tell the user when a send was a repeat of something already queued.
  function sentLabel(resp, fallback) {
    return resp && resp.duplicate_of ? "Sent ✓ (already in your list)" : fallback;
  }

  // Maps a failed relay response (or a chrome.runtime.lastError message) to a
  // pill label that says what actually went wrong.
  function failLabel(resp, lastErrMsg) {
    if (lastErrMsg) {
      if (/could not establish connection|receiving end does not exist|context invalidated|message port closed/i.test(lastErrMsg)) {
        return "Extension reloading — try again";
      }
      return "Extension error";
    }
    const err = resp && resp.error ? String(resp.error) : "";
    if (!err) return "Failed — no response";
    if (/not paired/i.test(err)) return "Not paired — open extension Options";
    if (/isn't running|failed to fetch|networkerror|econnrefused|load failed|network request failed/i.test(err)) {
      return "App not running";
    }
    return err.length > 40 ? err.slice(0, 39) + "…" : err;
  }

  function setLabel(overlay, text, color) {
    overlay.label.textContent = text;
    if (color) overlay.icon.style.color = color;
  }

  // E1: put the pill back to its default label. wire() gives each overlay a
  // defaultLabel() that adds the cached best quality ("Download 1080p ▾").
  function restoreLabel(overlay) {
    setLabel(overlay, overlay.defaultLabel ? overlay.defaultLabel() : "Download ▾", TH.accent);
  }

  // ---------- format-picker helpers ----------

  function fmtBytes(n) {
    if (!n) return "";
    const units = ["B", "KB", "MB", "GB"];
    let i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return `${n.toFixed(n >= 100 || i === 0 ? 0 : 1)} ${units[i]}`;
  }

  function streamInfo(video) {
    const w = video.videoWidth, h = video.videoHeight;
    const dims = w && h ? `${w}×${h}` : "unknown size";
    let dur = "";
    if (video.duration && isFinite(video.duration)) {
      const m = Math.floor(video.duration / 60);
      const s = Math.floor(video.duration % 60);
      dur = ` · ${m}:${String(s).padStart(2, "0")}`;
    }
    return dims + dur;
  }

  function formatLabel(f) {
    const res = f.resolution || "";
    const ext = f.ext || "";
    const size = f.filesize ? fmtBytes(f.filesize) : "";
    // "none" is yt-dlp's explicit audio-only marker. An unknown vcodec with
    // a WxH resolution (silent Twitter MP4s) is video, not audio.
    const kind = (f.vcodec === "none" || (!f.vcodec && !/\d+x\d+/.test(res))) ? "audio only" : "";
    return [res, ext, kind, size].filter(Boolean).join(" · ");
  }

  // Probe results cached per URL so reopening the menu doesn't re-hit the app.
  const FORMAT_CACHE = new Map();
  // E1: the resolved (successful) probe response per URL, so the pill label
  // can read it synchronously. Filled by probeFormatsCached; never filled by
  // anything that starts a probe just for the label.
  const FORMAT_RESULTS = new Map();

  // Best quality in a cached probe: "1080p" for video, the extension in caps
  // ("MP3") when the list is audio-only, "" when nothing is cached.
  function qualityHint(url) {
    const data = url && FORMAT_RESULTS.get(url);
    const formats = data && data.formats;
    if (!formats || !formats.length) return "";
    let best = 0;
    for (const f of formats) {
      const m = /(\d+)\s*x\s*(\d+)/.exec(f.resolution || "");
      // Short side: a 1080x1920 portrait clip is still "1080p".
      if (m) best = Math.max(best, Math.min(+m[1], +m[2]));
    }
    if (best) return best + "p";
    const audio = formats.find((f) => f.ext);
    return audio ? String(audio.ext).toUpperCase() : "";
  }
  function probeFormatsCached(url, pageUrl) {
    if (!url) return Promise.resolve(null);
    if (!FORMAT_CACHE.has(url)) {
      FORMAT_CACHE.set(url, new Promise((resolve) => {
        try {
          chrome.runtime.sendMessage({ type: "PROBE_FORMATS", url, pageUrl: pageUrl || location.href }, (resp) => {
            // Don't cache failures — the app may just be starting up.
            if (chrome.runtime.lastError || !resp || resp.ok === false) {
              FORMAT_CACHE.delete(url);
            } else if (resp.formats && resp.formats.length) {
              FORMAT_RESULTS.set(url, resp);
            }
            resolve(chrome.runtime.lastError ? null : resp);
          });
        } catch (e) {
          FORMAT_CACHE.delete(url);
          resolve(null);
        }
      }));
    }
    return FORMAT_CACHE.get(url);
  }

  function addMenuItem(menu, text, opts = {}, onClick) {
    const el = document.createElement("div");
    el.textContent = text;
    el.style.cssText = "padding:6px 10px;border-radius:6px;cursor:pointer;white-space:nowrap;";
    if (opts.dim) el.style.color = TH.text_muted;
    if (opts.header) {
      el.style.cssText += `cursor:default;font-weight:600;color:${TH.text};`;
    }
    if (!opts.header) {
      el.addEventListener("mouseenter", () => { el.style.background = TH.accent + "30"; });
      el.addEventListener("mouseleave", () => { el.style.background = "transparent"; });
      el.addEventListener("click", (e) => { e.stopPropagation(); if (onClick) onClick(); });
    }
    menu.appendChild(el);
    return el;
  }

  function addDivider(menu) {
    const el = document.createElement("div");
    el.style.cssText = "margin:4px 6px;border-top:1px solid rgba(255,255,255,0.12);";
    menu.appendChild(el);
  }

  // On X/Twitter feeds location.href is x.com/home, not the tweet. Find the
  // permalink of the tweet that actually contains this <video>. Other sites:
  // location.href. On X with no tweet found: "" (callers show an error
  // instead of silently sending the feed URL).
  function pageUrlFor(video) {
    if (!/(^|\.)(x|twitter)\.com$/i.test(location.hostname)) return location.href;
    const norm = (href) => {
      try {
        const p = new URL(href, location.origin).pathname;
        const m = /^\/([^\/?#]+)\/status\/(\d+)/.exec(p);
        return m ? `${location.origin}/${m[1]}/status/${m[2]}` : "";
      } catch (e) { return ""; }
    };
    try {
      const article = video.closest("article");
      if (article) {
        // The last timestamp permalink BEFORE the video in document order:
        // for a quoted tweet's video that is the quoted tweet's own link.
        let best = "";
        for (const a of article.querySelectorAll('a[href*="/status/"]')) {
          if (!a.querySelector("time")) continue;
          if (!(a.compareDocumentPosition(video) & Node.DOCUMENT_POSITION_FOLLOWING)) continue;
          const u = norm(a.getAttribute("href") || "");
          if (u) best = u;
        }
        if (best) return best;
      }
    } catch (e) {}
    return norm(location.pathname); // tweet detail page
  }

  async function handleSendPageUrl(video, overlay, closeMenu, download_playlist = false) {
    if (closeMenu) closeMenu();
    const pu = pageUrlFor(video);
    if (!pu) {
      setLabel(overlay, "Couldn't find tweet link", TH.error);
      return;
    }
    setLabel(overlay, "Sending…");
    const guessName = (document.title || "video").replace(/[\\/:*?"<>|]/g, "_").slice(0, 80);
    chrome.runtime.sendMessage(
      {
        type: "RELAY_DOWNLOAD",
        url: pu,
        pageUrl: pu,
        filename: guessName,
        download_playlist: download_playlist,
      },
      (resp) => {
        if (chrome.runtime.lastError) {
          setLabel(overlay, failLabel(null, chrome.runtime.lastError.message), TH.error);
          return;
        }
        if (resp && resp.ok) {
          setLabel(overlay, sentLabel(resp, "Sent to app ✓"), TH.accent);
          setTimeout(() => setLabel(overlay, "Send page URL to Grabber", TH.info), 2500);
        } else {
          setLabel(overlay, failLabel(resp), TH.error);
        }
      }
    );
  }

  function saveBlob(blob, filenameBase) {
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    const base = (filenameBase || "recording").replace(/\.webm$/i, "");
  a.download = `${base}.webm`;
    document.documentElement.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 15000);
  }

  // Session R: capture quality presets -> bitsPerSecond. "Source" omits
  // the cap and lets MediaRecorder pick the default for the codec.
  const REC_QUALITIES = {
    Low:    { bps: 1_500_000 },
    Medium: { bps: 4_000_000 },
    High:   { bps: 8_000_000 },
    Source: { bps: 0 },
  };
  let recQuality = "High";

  const REC_MODES = [
    { key: "element",     label: "Record element (video+audio)" },
    { key: "tab",         label: "Record tab (video+audio)" },
    { key: "tab-audio",   label: "Record tab (audio only)" },
    { key: "screen",      label: "Record screen…" },
  ];

  function recPickMime() {
    // R.3: probe codec support, best first.
    const candidates = [
      "video/webm;codecs=vp9,opus",
      "video/webm;codecs=vp8,opus",
      "video/webm",
      "",
    ];
    for (const mt of candidates) {
      if (!mt) return "";
      if (window.MediaRecorder && MediaRecorder.isTypeSupported(mt)) return mt;
    }
    return "";
  }

  function recNotify(title, body) {
    // v4.0.2 Q1: route through chrome.notifications (background page).
    // Content scripts can't call chrome.notifications themselves, and the
    // Web Notification API depended on each site's permission — so R.6
    // notifications rarely appeared. Best-effort, fire-and-forget.
    try {
      chrome.runtime.sendMessage(
        { type: "SHOW_NOTIFICATION", title, message: body });
    } catch (e) {}
  }

  function recBaseName(mode) {
    // R.7: <title> <mode> <quality> YYYYMMDD HHMMSS (sanitized).
    const title = (document.title || "recording").replace(/[\\/:*?"<>|]/g, "_").slice(0, 60);
    const d = new Date();
    const pad = (n) => String(n).padStart(2, "0");
    const stamp = `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())} ` +
                  `${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}`;
    return `${title} ${mode} ${recQuality} ${stamp}`;
  }

  function makeRecIndicator(state, overlay) {
    // R.5: floating indicator — elapsed timer, pause/resume, stop.
    const el = document.createElement("div");
    el.style.cssText = `position: fixed; left: 12px; bottom: 12px; z-index: 2147483647;
      background: ${TH.bg_base}ee; color: ${TH.text}; font: 12px system-ui, sans-serif;
      padding: 6px 10px; border-radius: 6px; display: flex; gap: 8px; align-items: center;`;
    const t = document.createElement("span");
    const btnPause = document.createElement("button");
    const btnStop = document.createElement("button");
    btnPause.textContent = "⏸";
    btnStop.textContent = "⏹";
    for (const b of [btnPause, btnStop]) {
      b.style.cssText = "cursor:pointer;border:0;border-radius:4px;padding:2px 6px;";
    }
    el.append(t, btnPause, btnStop);
    // v4.0.1 Fix 5: the Fullscreen API top layer ignores ordinary-DOM
    // z-index, so host the indicator inside the fullscreen element while
    // one exists. Harmless when no element is fullscreen.
    const host = () => document.fullscreenElement || document.documentElement;
    host().appendChild(el);
    state.onFullscreen = () => host().appendChild(el);
    document.addEventListener("fullscreenchange", state.onFullscreen);
    state.indicator = el;
    state.startedAt = Date.now();
    state.pausedTotal = 0;
    state.pausedAt = 0;
    state.tick = setInterval(() => {
      const paused = state.recorder && state.recorder.state === "paused"
        ? Date.now() - state.pausedAt : 0;
      const secs = Math.floor((Date.now() - state.startedAt - state.pausedTotal - paused) / 1000);
      t.textContent = `${String(Math.floor(secs / 60)).padStart(2, "0")}:${String(secs % 60).padStart(2, "0")}`;
    }, 500);
    btnPause.onclick = () => {
      const r = state.recorder;
      if (!r) return;
      if (r.state === "recording") { r.pause(); state.pausedAt = Date.now(); btnPause.textContent = "⏵"; }
      else if (r.state === "paused") { r.resume(); state.pausedTotal += Date.now() - state.pausedAt; btnPause.textContent = "⏸"; }
    };
    btnStop.onclick = () => { if (state.recorder) state.recorder.stop(); };
    return el;
  }

  async function startRecording(video, overlay, mode) {
    // R.1/R.2/R.4: mode-based capture with element -> tab auto-fallback.
    let stream = null;
    let usedMode = mode;
    if (mode === "element") {
      try {
        stream = video.captureStream ? video.captureStream() : video.mozCaptureStream();
      } catch (e) { stream = null; console.debug("[VG] captureStream threw:", e); }
      console.debug("[VG] captureStream tracks:",
                    stream ? stream.getVideoTracks().length : "null");
      if (stream && stream.getVideoTracks().length === 0) {
        stream.getTracks().forEach((t) => t.stop());
        stream = null;
      }
      if (!stream) {
        // R.2: element capture unavailable (DRM / no tracks) -> offer tab.
        console.debug("[VG] falling back to tab: element capture gave no usable stream;",
                      "captureStream supported:", !!(video.captureStream || video.mozCaptureStream),
                      "readyState:", video.readyState, "src:", String(video.currentSrc).slice(0, 60));
        recNotify("Video Grabber", "Element capture failed — falling back to tab capture.");
        return startRecording(video, overlay, "tab");
      }
    } else if (mode === "tab" || mode === "tab-audio") {
      try {
        stream = await navigator.mediaDevices.getDisplayMedia({
          video: mode === "tab"
            ? { frameRate: { ideal: 30, max: 60 } } : false,
          audio: {
            echoCancellation: false, noiseSuppression: false,
            autoGainControl: false,
          },
          // R.9: surface picker defaults to the current tab.
          preferCurrentTab: true,
          selfBrowserSurface: "include",
        });
      } catch (e) {
        setLabel(overlay, "Capture cancelled", TH.error);
        return;
      }
    } else if (mode === "screen") {
      try {
        stream = await navigator.mediaDevices.getDisplayMedia({
          video: { frameRate: { ideal: 30, max: 60 } },
          audio: true,
        });
      } catch (e) {
        setLabel(overlay, "Capture cancelled", TH.error);
        return;
      }
    }

    const chunks = [];
    const mime = recPickMime();
    const opts = {};
    if (mime) opts.mimeType = mime;
    const q = REC_QUALITIES[recQuality] || REC_QUALITIES.High;
    if (q.bps) opts.videoBitsPerSecond = q.bps;
    let recorder;
    try {
      recorder = new MediaRecorder(stream, opts);
    } catch (e) {
      try { recorder = new MediaRecorder(stream); }
      catch (e2) {
        stream.getTracks().forEach((t) => t.stop());
        setLabel(overlay, "Recorder unsupported", TH.error);
        return;
      }
    }
    const state = { recorder, stream, mode: usedMode };
    recorder.ondataavailable = (e) => {
      if (e.data && e.data.size > 0) chunks.push(e.data);
    };
    recorder.onstop = () => {
      clearInterval(state.tick);
      if (state.onFullscreen) document.removeEventListener("fullscreenchange", state.onFullscreen);
      if (state.indicator) state.indicator.remove();
      stream.getTracks().forEach((t) => t.stop());
      const blob = new Blob(chunks, { type: mime || "video/webm" });
      const guessName = recBaseName(usedMode);
      recNotify("Video Grabber", `Recording saved — ${usedMode} (${recQuality}).`);
      chrome.runtime.sendMessage({ type: "GET_UPLOAD_NONCE" }, async (nresp) => {
        if (chrome.runtime.lastError || !nresp || !nresp.ok || !nresp.nonce) {
          saveBlob(blob, guessName);
          setLabel(overlay, "Saved ✓ (browser) — Record again", TH.accent);
          RECORDERS.delete(video);
          return;
        }
        try {
          const form = new FormData();
          form.append("file", blob, guessName + ".webm");
          const res = await fetch("http://127.0.0.1:5757/upload", {
            method: "POST",
            headers: { "X-Upload-Nonce": nresp.nonce },
            body: form,
          });
          if (res.ok) {
            setLabel(overlay, "Sent to app ✓ — Record again", TH.accent);
          } else {
            const data = await res.json().catch(() => ({}));
            setLabel(overlay, (data.error || "Upload rejected").slice(0, 40), TH.error);
          }
        } catch (e) {
          saveBlob(blob, guessName);
          setLabel(overlay, "Saved ✓ (browser) — Record again", TH.accent);
        }
        RECORDERS.delete(video);
      });
    };
    recorder.start(1000);
    RECORDERS.set(video, state);
    makeRecIndicator(state, overlay);
    recNotify("Video Grabber", `Recording started — ${usedMode} (${recQuality}).`);
    setLabel(overlay, "● Recording — see indicator", TH.error);
  }

  function handleRecordToggle(video, overlay) {
    // Keep the pill button working: toggles an active recording, else
    // starts an element capture (with tab fallback inside startRecording).
    const state = RECORDERS.get(video);
    if (state && state.recorder && state.recorder.state !== "inactive") {
      state.recorder.stop();
      return;
    }
    startRecording(video, overlay, "element");
  }

  function addRecordItems(menu, video, overlay, closeMenu) {
    // R.1/R.3: quality row (cycles) + the four capture modes.
    addMenuItem(menu, `Quality: ${recQuality} (click to cycle)`, { dim: true }, () => {
      const order = ["Low", "Medium", "High", "Source"];
      recQuality = order[(order.indexOf(recQuality) + 1) % order.length];
      // Re-render the menu so the row shows the new quality.
      menu.querySelectorAll("div").forEach(() => {});
      const hdr = menu.firstChild;
      if (hdr) hdr.textContent = `Quality: ${recQuality} (click to cycle)`;
    });
    for (const m of REC_MODES) {
      addMenuItem(menu, m.label, {}, () => {
        closeMenu();
        startRecording(video, overlay, m.key);
      });
    }
    addDivider(menu);
  }
  function wire(video) {
    if (siteDisabled) return;
    if (HANDLED.has(video) || DISMISSED.has(video)) return;
    HANDLED.add(video);

    const overlay = makeOverlay(video);
    LIVE.push({ video, overlay });
    const isBlob = () => (video.currentSrc || "").startsWith("blob:");
    // E1: "Download 1080p ▾" once a format probe for this source is cached.
    // Read-only: this never starts a probe to fill the label.
    overlay.defaultLabel = () => {
      const hint = (isBlob() || isMseOnlySite()) ? "" : qualityHint(video.currentSrc);
      return hint ? `Download ${hint} ▾` : "Download ▾";
    };
    let menuOpen = false;
    let menuSeq = 0;

    function closeMenu() {
      if (!menuOpen) return;
      menuOpen = false;
      overlay.menu.style.display = "none";
      document.removeEventListener("click", onDocClick, true);
    }

    function onDocClick(e) {
      if (!overlay.wrap.contains(e.target)) closeMenu();
    }

    // Keep the menu on-screen: flip above the pill when there's no room
    // below, and hug the right edge when the pill is near the viewport edge.
    function placeMenu() {
      const m = overlay.menu;
      const r = overlay.wrap.getBoundingClientRect();
      const below = innerHeight - r.bottom;
      const above = r.top;
      if (m.offsetHeight > below - 8 && above > below) {
        m.style.top = "auto"; m.style.bottom = "100%";
        m.style.marginTop = "0"; m.style.marginBottom = "6px";
      } else {
        m.style.top = "100%"; m.style.bottom = "auto";
        m.style.marginTop = "6px"; m.style.marginBottom = "0";
      }
      if (r.left + m.offsetWidth > innerWidth - 8) {
        m.style.left = "auto"; m.style.right = "0";
      } else {
        m.style.left = "0"; m.style.right = "auto";
      }
    }

    function sendChoice({ format_id = null, target_format = null,
                         bypass_dialog = false, download_playlist = false,
                         force_page_url = false, pick_playlist = false,
                         url: directUrl = null } = {}) {
      closeMenu();
      const pu = pageUrlFor(video);
      if (force_page_url && !pu) {
        setLabel(overlay, "Couldn't find tweet link", TH.error);
        return;
      }
      setLabel(overlay, "Sending…");
      let base = (document.title || "video").replace(/[\\/:*?"<>|]/g, "_").slice(0, 80);
      // Strip an extension the page title already carries so we never
      // produce "video.mp4.mp4".
      base = base.replace(/\.(mp4|m4v|mov|webm|mkv|mp3|m4a|aac|wav|flac)$/i, "");
      // document.title has no extension — borrow it from the stream URL;
      // if there is none, omit filename so the backend guesses it.
      let ext = "";
      try {
        ext = (new URL(directUrl || video.currentSrc).pathname.match(/(\.[A-Za-z0-9]{1,5})$/) || [""])[0];
      } catch (e) {}
      const payload = {
        // Normal picks open the app's confirmation dialog (IDM-style);
        // bypass_dialog queues directly.
        type: bypass_dialog ? "RELAY_DOWNLOAD" : "SHOW_ADD_DIALOG",
        url: force_page_url ? pu : (directUrl || video.currentSrc),
        pageUrl: pu || location.href,
        filename: ext ? base + ext : undefined,
        format_id: format_id,
        target_format: target_format,
        download_playlist: download_playlist,
        pick_playlist: pick_playlist,
      };
      chrome.runtime.sendMessage(payload, (resp) => {
        if (chrome.runtime.lastError) {
          setLabel(overlay, failLabel(null, chrome.runtime.lastError.message), TH.error);
          return;
        }
        if (resp && resp.ok) {
          setLabel(overlay, sentLabel(resp, resp.auto_queued ? "Queued ✓" : "Sent to app ✓"), TH.accent);
          setTimeout(() => restoreLabel(overlay), 2500);
        } else {
          setLabel(overlay, failLabel(resp), TH.error);
        }
      });
    }

    // E2: copy to the clipboard, falling back to execCommand where a page's
    // permissions policy blocks navigator.clipboard.
    function copyText(text) {
      const legacy = () => {
        try {
          const ta = document.createElement("textarea");
          ta.value = text;
          ta.style.cssText = "position:fixed;left:-9999px;top:0;opacity:0;";
          document.documentElement.appendChild(ta);
          ta.select();
          const ok = document.execCommand("copy");
          ta.remove();
          return Promise.resolve(ok);
        } catch (e) {
          return Promise.resolve(false);
        }
      };
      try {
        return navigator.clipboard.writeText(text).then(() => true, legacy);
      } catch (e) {
        return legacy();
      }
    }

    function toggleMenu() {
      if (menuOpen) { closeMenu(); return; }
      menuOpen = true;
      const seq = ++menuSeq;
      const menu = overlay.menu;
      menu.innerHTML = "";

      const blob = isBlob();
      const mse = isMseOnlySite();
      addMenuItem(menu, streamInfo(video), { header: true });

      // E2: "Copy direct URL" appears only when the sniffer has a candidate
      // for this tab. Added right under the stream info once it answers.
      querySniffedVideos((best) => {
        if (!best || !best.url || seq !== menuSeq || !menuOpen) return;
        const copyItem = addMenuItem(menu, "Copy direct URL", {}, () => {
          copyText(best.url).then((ok) => {
            copyItem.textContent = ok ? "Copied ✓" : "Copy failed";
            setTimeout(() => { copyItem.textContent = "Copy direct URL"; }, 2000);
          });
        });
        menu.insertBefore(copyItem, menu.children[1] || null);
        placeMenu();
      });

    if (isPlaylistUrl(location.href)) {
      // On YouTube (and any extractor site), video.currentSrc is a blob:
      // URL that yt-dlp can't fetch. The playlist entries are always the
      // PAGE URL. The picker lists the playlist and queues one job per item.
      addMenuItem(menu, "Download this video only", {},
        () => sendChoice({ download_playlist: false, force_page_url: true }));
      addMenuItem(menu, "Pick playlist items\u2026", {},
        () => sendChoice({ pick_playlist: true, force_page_url: true }));
      addDivider(menu);
    }

      // Mode picker: the menu offers every mode that makes sense for this
      // source, and the click handler always opens it.
      if (blob || mse) {
        // Blob/MSE sources: no direct file. On extractor sites, ask the
        // backend to resolve the PAGE URL — that gives us the same quality
        // ladder IDM shows. On non-extractor MSE sites, only recording.
        if (isExtractorSite()) {
          // v6: blob player, but the sniffer may already know the real URL.
          // Ask in the background; if a candidate comes back, put
          // "Download file" at the very top of the menu. Otherwise the
          // placeholder is removed and the menu falls back to its current
          // behavior (Send page URL, formats, recording).
          if (blob || !video.currentSrc) {
            const sniffPh = addMenuItem(menu, "Looking for sniffed stream…",
              { dim: true, header: true });
            querySniffedVideos((best) => {
              if (seq !== menuSeq || !menuOpen) return;
              if (!best) { sniffPh.remove(); return; }
              const item = addMenuItem(menu,
                "Download file" + (best.size ? " · " + fmtBytes(best.size) : ""), {},
                () => {
                  // Same choice the direct-URL branch makes: a real file is
                  // sent as-is; anything else is normalized to mp4.
                  const directFile = /\.(mp4|webm|mkv|mov|m4v|avi)(\?|#|$)/i.test(best.url);
                  sendChoice(directFile
                    ? { url: best.url }
                    : { url: best.url, target_format: "mp4" });
                });
              menu.insertBefore(item, menu.firstChild);
              sniffPh.remove();
            });
          }
          addMenuItem(menu, "Download with yt-dlp", {},
            () => handleSendPageUrl(video, overlay, closeMenu));
          addDivider(menu);
          const fmtBox = document.createElement("div");
          menu.appendChild(fmtBox);
          const loadingEl = addMenuItem(fmtBox, "Loading formats…",
            { dim: true, header: true });
          menu.style.display = "flex";
          placeMenu();
          document.addEventListener("click", onDocClick, true);
          const pu = pageUrlFor(video);
          probeFormatsCached(pu, pu).then((data) => {
            if (seq !== menuSeq || !menuOpen) return;
            loadingEl.remove();
            const formats = (data && data.formats) || [];
            if (!formats.length) {
              const msg = !pu ? "Couldn't find tweet link" : (data && data.error)
                ? `No formats (${data.error})`
                : "No format list available";
              addMenuItem(fmtBox, msg, { dim: true, header: true });
            } else {
              // Task 2: no client-side truncation — the backend already
              // returns a sorted, sanity-capped list (best quality first).
              const ordered = orderFormats(formats);
              for (const f of ordered.list) {
                const tf = f.ext === "webm" ? "webm" : null;
                addMenuItem(fmtBox, (f === ordered.starred ? "★ " : "") + formatLabel(f), {},
                  () => {
                    rememberQuality(f);
                    closeMenu();
                    setLabel(overlay, "Sending…");
                    chrome.runtime.sendMessage({
                      type: "SHOW_ADD_DIALOG",
                      url: pu,
                      pageUrl: pu,
                      filename: (document.title || "video")
                        .replace(/[\\/:*?"<>|]/g, "_").slice(0, 80),
                      format_id: f.format_id,
                      target_format: tf,
                    }, (resp) => {
                      if (chrome.runtime.lastError) {
                        setLabel(overlay, failLabel(null, chrome.runtime.lastError.message), TH.error);
                        return;
                      }
                      if (resp && resp.ok) {
                        setLabel(overlay,
                          sentLabel(resp, resp.auto_queued ? "Queued ✓" : "Sent to app ✓"),
                          TH.accent);
                        setTimeout(() => restoreLabel(overlay), 2500);
                      } else {
                        setLabel(overlay, failLabel(resp), TH.error);
                      }
                    });
                  });
              }
            }
          });
          addDivider(menu);
          addRecordItems(menu, video, overlay, closeMenu);
          return;
        }

        // Non-extractor MSE/blob player: prefer the sniffed HLS/DASH manifest
        // over MediaRecorder. Recording stays below as the last resort.
        const sniffPh2 = addMenuItem(menu, "Looking for sniffed stream…",
          { dim: true, header: true });
        querySniffedVideos((best) => {
          if (seq !== menuSeq || !menuOpen) return;
          if (!best) { sniffPh2.remove(); return; }
          const item = addMenuItem(menu, "Download stream (HLS/DASH)", {},
            () => sendChoice({ url: best.url, target_format: "mp4" }));
          menu.insertBefore(item, menu.children[1] || null);
          sniffPh2.remove();
          placeMenu();
        });
        addRecordItems(menu, video, overlay, closeMenu);
        menu.style.display = "flex";
        placeMenu();
        document.addEventListener("click", onDocClick, true);
        return;
      }

      // Direct URL: download is primary, record + extractor as alternates.
      addMenuItem(menu, "Download file", {}, () => {
        const directFile = /\.(mp4|webm|mkv|mov|m4v|avi)(\?|#|$)/i.test(video.currentSrc || "");
        sendChoice(directFile ? {} : { target_format: "mp4" });
      });
      addRecordItems(menu, video, overlay, closeMenu);
      if (isExtractorSite()) {
        addMenuItem(menu, "Send page URL to yt-dlp", {},
          () => handleSendPageUrl(video, overlay, closeMenu));
      }
      addDivider(menu);
      const fmtBox = document.createElement("div");
      menu.appendChild(fmtBox);
      const loadingEl = addMenuItem(fmtBox, "Loading formats…",
        { dim: true, header: true });
      addDivider(menu);
      addMenuItem(menu, "Extract audio → MP3", {},
        () => sendChoice({ target_format: "mp3" }));
      addMenuItem(menu, "Extract audio → FLAC", {},
        () => sendChoice({ target_format: "flac" }));
      addMenuItem(menu, "Extract audio → Opus", {},
        () => sendChoice({ target_format: "opus" }));

      menu.style.display = "flex";
      placeMenu();
      document.addEventListener("click", onDocClick, true);

      probeFormatsCached(video.currentSrc).then((data) => {
        // E1: the result is cached now, so the pill can show the best quality
        // (only if the label is still the default one, not mid-send).
        if (/^Download/.test(overlay.label.textContent)) restoreLabel(overlay);
        if (seq !== menuSeq || !menuOpen) return; // stale probe or menu closed
        loadingEl.remove();
        // Best quality first: direct files stay as-is, page URLs default mp4.
        addMenuItem(fmtBox, "Best quality", {}, () => {
          const directFile = /\.(mp4|webm|mkv|mov|m4v|avi)(\?|#|$)/i.test(video.currentSrc || "");
          sendChoice(directFile ? {} : { target_format: "mp4" });
        });
        const formats = (data && data.formats) || [];
        if (!formats.length) {
          addMenuItem(fmtBox, "No format list — Download file will be used",
            { dim: true, header: true });
          return;
        }
        // Task 2: no client-side truncation — see note above.
        const ordered = orderFormats(formats);
        for (const f of ordered.list) {
          const tf = f.ext === "webm" ? "webm" : null;
          addMenuItem(fmtBox, (f === ordered.starred ? "★ " : "") + formatLabel(f), {},
            () => { rememberQuality(f); sendChoice({ format_id: f.format_id, target_format: tf }); });
        }
      });
    }

    const refresh = () => {
      if (RECORDERS.has(video)) return; // don't clobber label mid-recording
      if (isMseOnlySite()) {
        setLabel(overlay, "Send page URL ▾", TH.info);
        return;
      }
      if (!video.currentSrc) {
        setLabel(overlay, "Waiting for video…", "#999");
        return;
      }
      if (isBlob()) {
        setLabel(overlay, "Record this video ▾", TH.error);
      } else {
        restoreLabel(overlay);
      }
    };
    refresh();
    video.addEventListener("loadedmetadata", refresh);
    video.addEventListener("emptied", refresh);

    overlay.btn.addEventListener("click", (e) => {
      e.stopPropagation();
      e.preventDefault();
      if (RECORDERS.has(video)) { handleRecordToggle(video, overlay); return; }
      // On extractor sites where the player only exposes a blob (Reddit,
      // Redgifs, TikTok, X in-feed), there is no direct file to hand over.
      // The page URL is the download, so make the pill a one-click send
      // instead of forcing the user into the menu. This sits BEFORE the
      // "nothing to do" bail below so an empty currentSrc still sends.
      if (isOneClickSite() && (isBlob() || !video.currentSrc)) {
        handleSendPageUrl(video, overlay, null);
        return;
      }
      // MSE-only sites (Twitch) have no currentSrc yet but still need the menu —
      // it offers "Send page URL to yt-dlp". Only bail when there's truly nothing.
      if (!video.currentSrc && !isBlob() && !isMseOnlySite()) return;
      toggleMenu();
    });
  }

  // v4.1.1: light scan — <video> elements only, no shadow-root walk.
  // The interval calls this; the MutationObserver calls the full scan().
  function scanLight(root = document) {
    root.querySelectorAll("video").forEach(wire);
    // SPA players (X/Twitter, Reddit feeds) rebuild <video> elements on
    // every clip transition: drop pills whose video left the DOM so their
    // listeners and ResizeObservers don't accumulate forever.
    for (const entry of LIVE.slice()) {
      if (!entry.video.isConnected) {
        try { entry.overlay.cleanup(); } catch (e) {}
      }
    }
  }

  function scan(root = document) {
    root.querySelectorAll("video").forEach(wire);
    // Reddit (and an increasing number of SPA players) render <video>
    // inside shadow roots, which document.querySelectorAll can't see.
    // Walk every element's shadow root recursively and scan inside it.
    // Only OPEN roots are reachable (el.shadowRoot is null for closed
    // ones) — those are invisible by design and out of scope. wire() is
    // guarded by HANDLED, so re-walking on the 5s poll is cheap no-op.
    root.querySelectorAll("*").forEach((el) => {
      if (el.shadowRoot) scan(el.shadowRoot);
    });
  }

  scan();
  const mo = new MutationObserver((mutations) => {
    // v4.1.1: new nodes may bring a player — snap the poll back to fast.
    if (scanInterval !== SCAN_FAST) {
      scanInterval = SCAN_FAST;
      emptyScans = 0;
      scheduleScan();
    }
    for (const m of mutations) {
      m.addedNodes.forEach((node) => {
        if (!(node instanceof Element)) return;
        // Scan the added subtree — which now also descends into any
        // shadow roots it carries, so SPA players that mount <video>
        // inside shadow DOM (Reddit) get picked up without waiting for
        // the 5s poll. wire() dedupes via HANDLED.
        if (node.tagName === "VIDEO") wire(node);
        scan(node);
      });
    }
  });
  mo.observe(document.documentElement, { childList: true, subtree: true });

  // v4.1.1: adaptive scan interval. A frame with no <video> at all doesn't
  // need a 5s DOM walk: after five consecutive empty light scans, back off
  // to 30s. Any DOM mutation or user interaction snaps it back to 5s. This
  // is what stops hundreds of ad/analytics iframes from pinning a core.
  const SCAN_FAST = 5000, SCAN_SLOW = 30000, EMPTY_LIMIT = 5;
  let scanInterval = SCAN_FAST;
  let emptyScans = 0;
  let scanTimer = null;

  function scheduleScan() {
    if (scanTimer) clearInterval(scanTimer);
    scanTimer = setInterval(runScanTick, scanInterval);
  }

  function runScanTick() {
    const hasVideo = document.querySelector("video") !== null;
    if (hasVideo) {
      emptyScans = 0;
      if (scanInterval !== SCAN_FAST) {
        scanInterval = SCAN_FAST;
        scheduleScan();
      }
    } else {
      emptyScans++;
      if (emptyScans >= EMPTY_LIMIT && scanInterval !== SCAN_SLOW) {
        scanInterval = SCAN_SLOW;
        scheduleScan();
      }
    }
    scanLight();
  }

  // User interaction can reveal a player without a mutation we care about
  // (lazy loaders on click/hover) — wake the poll back up.
  for (const ev of ["pointerdown", "keydown", "scroll"]) {
    window.addEventListener(ev, () => {
      if (scanInterval !== SCAN_FAST) {
        scanInterval = SCAN_FAST;
        emptyScans = 0;
        scheduleScan();
      }
    }, { passive: true, capture: true });
  }

  // Some SPA players swap currentSrc without any mutation nearby — poll lightly.
  scheduleScan();

  // ---- v7: image micro-pill (IDM style) ------------------------------------
  // One tiny round button that jumps onto any <img>, or any element with an
  // inline background-image, larger than 150x150 while the mouse is over it.
  // Click queues the best URL directly (RELAY_DOWNLOAD never opens the Add
  // dialog) and shows a small toast. Delegated capture listeners mean one pill
  // per frame instead of one per image, and images added later just work.
  const MICRO_MIN = 150;
  const MICRO_SIZE = 24;
  const MICRO_W = 46;   // v8: download half (24px) + ▾ menu half
  const MICRO_IMG_EXT_RE = /\.(jpe?g|png|gif|webp|avif|bmp|svg|tiff?|heic|heif|jxl|ico)$/i;
  let microPill = null;
  let microToast = null;
  let microTarget = null;
  let microHideTimer = 0;
  let microToastTimer = 0;
  let microPlaceQueued = false;

  function microAbs(u, base) {
    try {
      const x = new URL(u, base || document.baseURI);
      return /^https?:$/i.test(x.protocol) ? x.href : "";
    } catch (e) { return ""; }
  }

  // Spec-style srcset parser. A plain split(",") breaks on CDN URLs that
  // contain commas (w_300,h_200), so walk the string instead: a URL is the
  // next run of non-space characters, descriptors run to the next comma that
  // is not inside parentheses.
  function parseSrcset(str) {
    const out = [];
    if (!str) return out;
    const s = String(str);
    const n = s.length;
    let i = 0;
    while (i < n) {
      while (i < n && /[\s,]/.test(s[i])) i++;
      if (i >= n) break;
      let j = i;
      while (j < n && !/\s/.test(s[j])) j++;
      let url = s.slice(i, j);
      i = j;
      let desc = "";
      if (/,+$/.test(url)) {
        url = url.replace(/,+$/, "");
      } else {
        let k = i, depth = 0;
        while (k < n) {
          const ch = s[k];
          if (ch === "(") depth++;
          else if (ch === ")") depth = Math.max(0, depth - 1);
          else if (ch === "," && depth === 0) break;
          k++;
        }
        desc = s.slice(i, k).trim();
        i = k + 1;
      }
      if (url) out.push({ url, desc });
    }
    return out;
  }

  // Fold one srcset string into `best`. "2000w" beats "500w"; "2x" beats "1x"
  // (density is scaled by the rendered width so both kinds stay comparable);
  // no descriptor means 1x.
  function srcsetBest(str, base, renderedW, best) {
    for (const cand of parseSrcset(str)) {
      const abs = microAbs(cand.url, base);
      if (!abs) continue;
      let score = renderedW;
      for (const tok of cand.desc.split(/\s+/)) {
        const m = /^(\d+(?:\.\d+)?)([wx])$/i.exec(tok);
        if (!m) continue;
        score = m[2].toLowerCase() === "w" ? parseFloat(m[1]) : parseFloat(m[1]) * renderedW;
      }
      if (!best || score > best.score) best = { url: abs, score };
    }
    return best;
  }

  // Highest-resolution URL for an <img>: its own srcset / data-srcset plus the
  // srcset of every <source> in a parent <picture> whose media query matches.
  // Falls back to currentSrc / src / common lazy-load attributes.
  function microImageUrl(img) {
    const base = img.baseURI || document.baseURI;
    const rw = img.getBoundingClientRect().width || 1;
    let best = null;
    try {
      best = srcsetBest(img.getAttribute("srcset"), base, rw, best);
      best = srcsetBest(img.getAttribute("data-srcset"), base, rw, best);
      const pic = img.parentElement;
      if (pic && pic.tagName === "PICTURE") {
        for (const src of pic.querySelectorAll("source")) {
          const media = src.getAttribute("media");
          if (media) {
            try { if (!window.matchMedia(media).matches) continue; } catch (e) {}
          }
          best = srcsetBest(src.getAttribute("srcset"), base, rw, best);
        }
      }
    } catch (e) {}
    if (best) return best.url;
    const fallbacks = [img.currentSrc, img.src,
                       img.getAttribute("data-src"), img.getAttribute("data-original")];
    for (const cand of fallbacks) {
      const abs = cand ? microAbs(cand, base) : "";
      if (abs) return abs;
    }
    return "";
  }

  // url(...) out of an INLINE background-image style (first one if several).
  function microBgUrl(el) {
    let v = "";
    try { v = (el.style && el.style.backgroundImage) || ""; } catch (e) {}
    if (!v || v === "none") return "";
    const m = /url\(\s*(['"]?)(.*?)\1\s*\)/i.exec(v);
    return m && m[2] ? microAbs(m[2].replace(/\\(["'])/g, "$1"), el.baseURI) : "";
  }

  function microUrlFor(el) {
    return el.tagName === "IMG" ? microImageUrl(el) : microBgUrl(el);
  }

  function microEligible(el) {
    if (!el || el.nodeType !== 1) return false;
    if (el === microPill || el === microToast) return false;
    if (el.tagName !== "IMG" && !(el.hasAttribute("style") && microBgUrl(el))) return false;
    const r = el.getBoundingClientRect();
    if (!(r.width > MICRO_MIN && r.height > MICRO_MIN)) return false;
    return !!microUrlFor(el);
  }

  function microToastShow(text, isErr) {
    if (!microToast || !microToast.isConnected) {
      const t = document.createElement("div");
      t.id = "vg-micro-toast";
      t.style.cssText = `
        all: initial; position: fixed; right: 16px; bottom: 16px; z-index: 2147483647;
        max-width: 260px; box-sizing: border-box; padding: 8px 12px; border-radius: 8px;
        font: 13px/1.3 -apple-system, Segoe UI, Roboto, sans-serif;
        background: ${TH.bg_elevated}f2; color: ${TH.text};
        box-shadow: 0 4px 16px rgba(0,0,0,0.5); pointer-events: none;
        opacity: 0; transition: opacity 150ms ease; display: none;
      `;
      document.documentElement.appendChild(t);
      microToast = t;
    }
    microToast.textContent = text;
    microToast.style.border = `1px solid ${isErr ? TH.error : TH.accent}`;
    microToast.style.display = "block";
    requestAnimationFrame(() => { if (microToast) microToast.style.opacity = "1"; });
    clearTimeout(microToastTimer);
    microToastTimer = setTimeout(() => {
      if (!microToast) return;
      microToast.style.opacity = "0";
      setTimeout(() => { if (microToast) microToast.style.display = "none"; }, 180);
    }, 2200);
  }

  function ensureMicroPill() {
    if (microPill && microPill.isConnected) return microPill;
    ensurePillStyle();
    const p = document.createElement("div");
    p.id = "vg-micro-pill";
    p.className = "vg-micro-pill";
    p.title = "Video Grabber";
    p.style.cssText = `
      all: initial; position: absolute; z-index: 2147483647; display: none;
      align-items: stretch; box-sizing: border-box; overflow: hidden;
      width: ${MICRO_W}px; height: ${MICRO_SIZE}px; border-radius: ${MICRO_SIZE / 2}px;
      background: ${TH.bg_base}e6; color: ${TH.accent};
      border: 1px solid ${TH.accent}; box-shadow: 0 1px 6px rgba(0,0,0,0.5);
      cursor: pointer; user-select: none;
    `;
    // Left half: the one-click download (unchanged from v7).
    const dl = document.createElement("span");
    dl.title = "Download Image";
    dl.style.cssText = `
      all: initial; display: flex; align-items: center; justify-content: center;
      width: ${MICRO_SIZE}px; height: ${MICRO_SIZE}px; box-sizing: border-box; cursor: pointer;
    `;
    const NS = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(NS, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("width", "14");
    svg.setAttribute("height", "14");
    svg.setAttribute("fill", "none");
    svg.setAttribute("stroke", "currentColor");
    svg.setAttribute("stroke-width", "2.6");
    svg.setAttribute("stroke-linecap", "round");
    svg.setAttribute("stroke-linejoin", "round");
    svg.style.pointerEvents = "none";
    for (const d of ["M12 4v11", "M7 11l5 5 5-5", "M5 20h14"]) {
      const path = document.createElementNS(NS, "path");
      path.setAttribute("d", d);
      svg.appendChild(path);
    }
    dl.appendChild(svg);
    // Right half: a tiny dropdown arrow that opens the image menu.
    const arrow = document.createElement("span");
    arrow.title = "More image options";
    arrow.textContent = "\u25be";
    arrow.style.cssText = `
      all: initial; display: flex; align-items: center; justify-content: center;
      flex: 1 1 auto; height: ${MICRO_SIZE}px; box-sizing: border-box; cursor: pointer;
      font: 10px/1 -apple-system, Segoe UI, Roboto, sans-serif; color: ${TH.accent};
      border-left: 1px solid ${TH.accent};
    `;
    p.appendChild(dl);
    p.appendChild(arrow);

    // Keep the site's lightbox / drag handlers from seeing our clicks.
    for (const ev of ["mousedown", "mouseup", "pointerdown", "pointerup", "dblclick"]) {
      p.addEventListener(ev, (e) => e.stopPropagation());
    }
    dl.addEventListener("click", (e) => {
      e.stopPropagation();
      e.preventDefault();
      sendMicro(microTarget);
    });
    arrow.addEventListener("click", (e) => {
      e.stopPropagation();
      e.preventDefault();
      openMicroMenu(e.clientX, e.clientY, microTarget);
    });
    p.addEventListener("mouseenter", () => {
      clearTimeout(microHideTimer);
      p.style.background = TH.accent;
      p.style.color = TH.bg_base;
    });
    p.addEventListener("mouseleave", (e) => {
      p.style.background = `${TH.bg_base}e6`;
      p.style.color = TH.accent;
      if (pillCtx) return;   // the menu is open: keep the target until it closes
      scheduleMicroHide(e.relatedTarget);
    });
    document.documentElement.appendChild(p);
    microPill = p;
    return p;
  }

  // Top-right corner of the part of the element that is on screen, in page
  // coordinates (the pill is position:absolute on the root element).
  function placeMicro() {
    microPlaceQueued = false;
    const el = microTarget;
    if (!el || !microPill) return;
    if (!el.isConnected) { hideMicro(); return; }
    const r = el.getBoundingClientRect();
    if (!(r.width > MICRO_MIN && r.height > MICRO_MIN)
        || r.bottom < 0 || r.top > innerHeight || r.right < 0 || r.left > innerWidth) {
      microPill.style.display = "none";
      return;
    }
    const vw = document.documentElement.clientWidth || innerWidth;
    const right = Math.min(r.right, vw);
    const top = Math.max(r.top, 0);
    microPill.style.left = (right + scrollX - MICRO_W - 6) + "px";
    microPill.style.top = (top + scrollY + 6) + "px";
    microPill.style.display = "flex";
  }

  function showMicro(el) {
    clearTimeout(microHideTimer);
    ensureMicroPill();
    microTarget = el;
    placeMicro();
  }

  function hideMicro() {
    clearTimeout(microHideTimer);
    microTarget = null;
    if (microPill) microPill.style.display = "none";
  }

  function scheduleMicroHide(related) {
    if (related && related.nodeType === 1) {
      if (related === microPill || (microPill && microPill.contains(related))) return;
      if (microTarget && microTarget.contains(related)) return;
    }
    clearTimeout(microHideTimer);
    microHideTimer = setTimeout(hideMicro, 150);
  }

  function microFilenameFor(url) {
    try {
      const last = decodeURIComponent(new URL(url).pathname.split("/").pop() || "");
      if (MICRO_IMG_EXT_RE.test(last)) return last;
    } catch (e) {}
    return undefined;
  }

  function sendMicro(el) {
    if (!el) return;
    const url = microUrlFor(el);   // re-read now: srcset may have changed since hover
    if (!url) { microToastShow("No image URL found", true); return; }
    try {
      chrome.runtime.sendMessage(
        { type: "RELAY_DOWNLOAD", url, pageUrl: location.href,
          filename: microFilenameFor(url), skipList: true },
        (resp) => {
          if (chrome.runtime.lastError) {
            microToastShow(failLabel(null, chrome.runtime.lastError.message), true);
            return;
          }
          if (resp && resp.ok) {
            microToastShow(resp.duplicate_of ? "Image already in your list" : "Image sent to queue!");
          } else {
            microToastShow(failLabel(resp), true);
          }
        });
    } catch (e) {
      microToastShow("Extension reloading, try again", true);
    }
  }

  // Clipboard write from a content script. Falls back to execCommand, which
  // still works on pages where navigator.clipboard is refused.
  function copyToClipboard(text) {
    const fallback = () => {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.cssText = "position: fixed; opacity: 0;";
      document.documentElement.appendChild(ta);
      ta.select();
      let ok = false;
      try { ok = document.execCommand("copy"); } catch (e) {}
      ta.remove();
      return ok;
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text).then(() => true, () => fallback());
    }
    return Promise.resolve(fallback());
  }

  function openMicroMenu(x, y, el) {
    if (!el) return;
    openPillCtx(x, y, [
      { label: "Download Image", onClick: () => sendMicro(el) },
      { label: "Download ALL Images", onClick: () => downloadAllPageImages() },
      { sep: true },
      { label: "Copy Image URL", onClick: () => {
          const url = microUrlFor(el);
          if (!url) { microToastShow("No image URL found", true); return; }
          copyToClipboard(url).then((ok) =>
            microToastShow(ok ? "Image URL copied" : "Couldn't copy the URL", !ok));
        } },
    ]);
  }

  // "Download ALL Images": every <img> on the page that is a real picture
  // (not an icon) with an http(s) URL, de-duplicated. Blob and data URLs are
  // skipped because the desktop app can't fetch them.
  function collectPageImageUrls() {
    const seen = new Set();
    const urls = [];
    for (const img of document.querySelectorAll("img")) {
      const w = img.naturalWidth || img.width || 0;
      const h = img.naturalHeight || img.height || 0;
      if (Math.max(w, h) < 100) continue;
      const url = microImageUrl(img);
      if (!url || !/^https?:/i.test(url) || seen.has(url)) continue;
      seen.add(url);
      urls.push(url);
    }
    return urls;
  }

  function downloadAllPageImages() {
    const urls = collectPageImageUrls().slice(0, 200);
    if (!urls.length) { microToastShow("No images found on this page", true); return; }
    let sent = 0, dupes = 0, failed = 0, pending = urls.length;
    const finish = () => {
      if (--pending > 0) return;
      const parts = [`Queued ${sent} image(s)`];
      if (dupes) parts.push(`${dupes} already listed`);
      if (failed) parts.push(`${failed} failed`);
      microToastShow(parts.join(", "), failed > 0 && sent === 0);
    };
    microToastShow(`Sending ${urls.length} image(s)\u2026`);
    for (const url of urls) {
      try {
        chrome.runtime.sendMessage(
          { type: "RELAY_DOWNLOAD", url, pageUrl: location.href,
            filename: microFilenameFor(url), skipList: true },
          (resp) => {
            if (chrome.runtime.lastError || !(resp && resp.ok)) failed++;
            else if (resp.duplicate_of) dupes++;
            else sent++;
            finish();
          });
      } catch (e) {
        failed++;
        finish();
      }
    }
  }

  // mouseenter does not bubble but does pass through the capture phase, so one
  // listener on the document sees every element the pointer enters.
  document.addEventListener("mouseenter", (e) => {
    if (siteDisabled) return;
    const t = e.target;
    if (!(t instanceof Element)) return;
    if (t.tagName !== "IMG" && !t.hasAttribute("style")) return;
    if (!microEligible(t)) return;
    showMicro(t);
  }, true);
  document.addEventListener("mouseleave", (e) => {
    if (e.target === microTarget) scheduleMicroHide(e.relatedTarget);
  }, true);
  const microReplace = () => {
    if (!microTarget || microPlaceQueued) return;
    microPlaceQueued = true;
    requestAnimationFrame(placeMicro);
  };
  window.addEventListener("scroll", microReplace, { capture: true, passive: true });
  window.addEventListener("resize", microReplace, { passive: true });

  // ---- v6: blob-anchor download bridge (pairs with mainhook.js) -----------
  // mainhook.js (MAIN world, claude.ai only) swallows <a download href=blob:>
  // clicks while armed and posts the Blob here by reference. We upload it to
  // the app's /upload endpoint (same nonce pattern as the recorder). Any
  // failure posts vg-blob-replay so the page re-fires the original click and
  // the browser saves the file as usual. The channel is untrusted: only real
  // Blob objects are accepted, never URLs. The secret is a nuisance barrier,
  // not a security boundary (page scripts can read window messages too).
  (() => {
    const HOOK_HOSTS = ["claude.ai"];   // keep in sync with manifest.json
    if (!HOOK_HOSTS.includes(location.hostname.toLowerCase())) return;

    const BLOB_DEBUG = false;
    const bytes = new Uint8Array(16);
    crypto.getRandomValues(bytes);
    const SECRET = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");

    let hookEnabled = true;    // blobHookEnabled option, default on
    let lastSync = 0;
    let syncing = false;

    const dbg = (...a) => { if (BLOB_DEBUG) console.debug("[VG] blob bridge:", ...a); };

    function post(msg) {
      try { window.postMessage({ ...msg, source: SECRET }, "*"); } catch (e) {}
    }

    function queryState() {
      return new Promise((resolve) => {
        try {
          chrome.runtime.sendMessage({ type: "VG_STATE" }, (r) => {
            resolve(chrome.runtime.lastError || !r ? null : r);
          });
        } catch (e) { resolve(null); }
      });
    }

    async function syncArmed(force) {
      const now = Date.now();
      if (syncing || (!force && now - lastSync < 2000)) return;
      syncing = true;
      lastSync = now;
      try {
        const st = await queryState();
        const armed = !!(hookEnabled && st && st.alive && st.paired && st.intercept);
        post({ type: "vg-armed", armed, debug: BLOB_DEBUG });
      } finally {
        syncing = false;
      }
    }

    function cleanName(name) {
      const n = String(name || "").replace(/[\\/:*?"<>|\u0000-\u001f]/g, "_").trim().slice(0, 200);
      return n || "download";
    }

    async function uploadBlob(blob, filename) {
      const st = await queryState();
      if (!hookEnabled || !st || !st.alive || !st.paired || !st.intercept) {
        dbg("not ready", st);
        return false;
      }
      const nresp = await new Promise((resolve) => {
        try {
          chrome.runtime.sendMessage({ type: "GET_UPLOAD_NONCE" }, (r) => {
            resolve(chrome.runtime.lastError ? null : r);
          });
        } catch (e) { resolve(null); }
      });
      if (!nresp || !nresp.ok || !nresp.nonce) { dbg("no nonce", nresp); return false; }
      const form = new FormData();
      form.append("file", blob, cleanName(filename));
      const res = await fetch("http://127.0.0.1:5757/upload", {
        method: "POST",
        headers: { "X-Upload-Nonce": nresp.nonce },
        body: form,
      });
      if (!res.ok) { dbg("upload status", res.status); return null; }
      // E7: the app says which category folder it filed the upload under.
      let category = "";
      try {
        const j = await res.json();
        category = typeof j.category === "string" ? j.category : "";
      } catch (e) {}
      return { category };
    }

    window.addEventListener("message", async (e) => {
      if (e.source !== window) return;
      let type, src, id, blob, filename;
      try {
        const d = e.data;
        if (!d || typeof d !== "object") return;
        type = d.type; src = d.source; id = d.id; blob = d.blob; filename = d.filename;
      } catch (err) { return; }
      if (type !== "vg-blob-download" || src !== SECRET) return;
      if (typeof id !== "string" || !(blob instanceof Blob)) return;

      // Ack first, before any async work: stops the page-side watchdog.
      post({ type: "vg-blob-ack", id });
      let ok = false;
      try {
        ok = await uploadBlob(blob, typeof filename === "string" ? filename : "");
      } catch (err) {
        dbg("upload failed", err);
        ok = false;
      }
      if (ok) {
        post({ type: "vg-blob-done", id });
        recNotify("Video Grabber", ok.category ? `Sent to ${ok.category} ✓` : "Sent to app ✓");
      } else {
        post({ type: "vg-blob-replay", id });
      }
    });

    // Armed state can't be observed (the app can start or stop at any time),
    // so refresh on user input, tab focus, option changes and a slow timer.
    document.addEventListener("pointerdown", () => syncArmed(false), true);
    document.addEventListener("keydown", () => syncArmed(false), true);
    document.addEventListener("visibilitychange", () => { if (!document.hidden) syncArmed(true); });
    setInterval(() => { if (!document.hidden) syncArmed(true); }, 15000);

    try {
      chrome.storage.onChanged.addListener((c, area) => {
        if (area !== "local") return;
        if (c.blobHookEnabled) hookEnabled = c.blobHookEnabled.newValue !== false;
        if (c.blobHookEnabled || c.interceptDownloads || c.apiToken) syncArmed(true);
      });
      chrome.storage.local.get("blobHookEnabled").then((st) => {
        hookEnabled = st.blobHookEnabled !== false;
      }).catch(() => {}).finally(() => syncArmed(true));
    } catch (e) { syncArmed(true); }
  })();
})();