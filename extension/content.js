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
  const HANDLED = new WeakSet();
  const DISMISSED = new WeakSet();
  const RECORDERS = new WeakMap();

  // Playlist support: a YouTube watch URL carrying &list=... can be sent
  // as a single video or as the whole playlist (the backend decides via
  // the download_playlist flag; list= stays in the URL on purpose).
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
  ];
  const isExtractorSite = () => EXTRACTOR_HOSTS.some((rx) => rx.test(location.hostname));

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
    const { wrap, video, offset, start } = _drag;
    const r = video.getBoundingClientRect();
    const nx = start.dx + (e.clientX - start.x);
    const ny = start.dy + (e.clientY - start.y);
    // Keep the pill inside the viewport.
    offset.dx = Math.max(8 - r.left,
      Math.min(nx, innerWidth - r.left - wrap.offsetWidth - 8));
    offset.dy = Math.max(8 - r.top,
      Math.min(ny, innerHeight - r.top - wrap.offsetHeight - 8));
    wrap.__vgPosition();
  });
  window.addEventListener("mouseup", () => {
    if (!_drag) return;
    const { wrap, offset, isTopFrame } = _drag;
    _drag = null;
    wrap.style.cursor = "grab";
    if (isTopFrame) {
      try {
        localStorage.setItem(`vg_pill_offset_${location.hostname}`,
                             JSON.stringify(offset));
      } catch (e) {}
    }
  });

  function makeOverlay(video) {
    const wrap = document.createElement("div");
    wrap.style.cssText = `
      position: fixed; z-index: 2147483647; display: flex; align-items: center;
      gap: 6px; font: 13px/1.2 -apple-system, Segoe UI, Roboto, sans-serif;
      background: rgba(20,20,24,0.88); color: #fff; padding: 6px 8px;
      border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.4);
      pointer-events: auto; user-select: none;
    `;

    wrap.style.transition = "opacity 150ms ease";
    wrap.style.opacity = "0";

    const btn = document.createElement("button");
    btn.style.cssText = `
      display:flex; align-items:center; gap:6px; background:transparent;
      border:1px solid rgba(255,255,255,0.35); color:#fff; border-radius:6px;
      padding:4px 10px; font: inherit; cursor:pointer;
    `;
    const icon = document.createElement("span");
    icon.style.cssText = "color:#4caf50; font-size:11px;";
    icon.textContent = "▶";
    const label = document.createElement("span");
    btn.appendChild(icon);
    btn.appendChild(label);

    const closeBtn = document.createElement("button");
    closeBtn.textContent = "✕";
    closeBtn.title = "Hide";
    closeBtn.style.cssText = `
      background:transparent; border:none; color:#bbb; cursor:pointer;
      font-size:12px; padding:2px 4px;
    `;
    closeBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      DISMISSED.add(video);
      // 12.4: fade out, then remove (close).
      wrap.style.opacity = "0";
      setTimeout(() => wrap.remove(), 150);
    });

    const menu = document.createElement("div");
    menu.style.cssText = `
      position: absolute; top: 100%; left: 0; margin-top: 6px; min-width: 250px;
      background: rgba(18,22,28,0.97); color: #fff;
      border: 1px solid rgba(255,255,255,0.12); border-radius: 10px;
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

    // Per-site drag offset, persisted in localStorage (top frame only —
    // an iframe's location.hostname is the parent's, so sub-frame pills
    // would share and fight over the same key).
    const isTopFrame = window === window.top;
    let offset = { dx: 8, dy: 8 };
    if (isTopFrame) {
      try {
        const saved = localStorage.getItem(`vg_pill_offset_${location.hostname}`);
        if (saved) offset = JSON.parse(saved);
      } catch (e) {}
    }

    function position() {
      const r = video.getBoundingClientRect();
      wrap.style.top = Math.max(8, r.top + offset.dy) + "px";
      wrap.style.left = Math.max(8, r.left + offset.dx) + "px";
      wrap.style.display = isVisible(video)
        && r.bottom > 0 && r.top < innerHeight
        && r.right > 0 && r.left < innerWidth
        ? "flex" : "none";
    }
    position();
    wrap.__vgPosition = position;
    const reposition = () => requestAnimationFrame(position);
    window.addEventListener("scroll", reposition, true);
    window.addEventListener("resize", reposition);
    const ro = new ResizeObserver(reposition);
    ro.observe(video);

    // ---- drag to reposition (module-level delegated handlers; see _drag) ----
    wrap.style.cursor = "grab";
    wrap.addEventListener("mousedown", (e) => {
      // Ignore drags starting on a button, the close X, or the open menu.
      if (e.target.closest("button") || menu.contains(e.target)) return;
      _drag = { wrap, video, offset,
                start: { x: e.clientX, y: e.clientY, dx: offset.dx, dy: offset.dy },
                isTopFrame };
      wrap.style.cursor = "grabbing";
      e.preventDefault();
    });

    wrap.addEventListener("dblclick", (e) => {
      // Double-click the pill body (not a button) resets its position.
      if (e.target.closest("button") || menu.contains(e.target)) return;
      offset = { dx: 8, dy: 8 };
      if (isTopFrame) {
        try { localStorage.removeItem(`vg_pill_offset_${location.hostname}`); } catch (e) {}
      }
      position();
    });

    return { wrap, btn, label, icon, menu, position };
  }

  function setLabel(overlay, text, color) {
    overlay.label.textContent = text;
    if (color) overlay.icon.style.color = color;
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
    const kind = !f.vcodec || f.vcodec === "none" ? "audio only" : "";
    return [res, ext, kind, size].filter(Boolean).join(" · ");
  }

  // Probe results cached per URL so reopening the menu doesn't re-hit the app.
  const FORMAT_CACHE = new Map();
  function probeFormatsCached(url) {
    if (!url) return Promise.resolve(null);
    if (!FORMAT_CACHE.has(url)) {
      FORMAT_CACHE.set(url, new Promise((resolve) => {
        try {
          chrome.runtime.sendMessage({ type: "PROBE_FORMATS", url }, (resp) => {
            // Don't cache failures — the app may just be starting up.
            if (chrome.runtime.lastError || !resp || resp.ok === false) {
              FORMAT_CACHE.delete(url);
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
    if (opts.dim) el.style.color = "#9aa7b4";
    if (opts.header) {
      el.style.cssText += "cursor:default;font-weight:600;color:#e6edf3;";
    }
    if (!opts.header) {
      el.addEventListener("mouseenter", () => { el.style.background = "rgba(76,175,80,0.18)"; });
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

  async function handleSendPageUrl(video, overlay, closeMenu, download_playlist = false) {
    if (closeMenu) closeMenu();
    setLabel(overlay, "Sending…");
    const guessName = (document.title || "video").replace(/[\\/:*?"<>|]/g, "_").slice(0, 80);
    chrome.runtime.sendMessage(
      {
        type: "RELAY_DOWNLOAD",
        url: location.href,
        pageUrl: location.href,
        filename: guessName,
        download_playlist: download_playlist,
      },
      (resp) => {
        if (chrome.runtime.lastError) {
          setLabel(overlay, "Extension error", "#e53935");
          return;
        }
        if (resp && resp.ok) {
          setLabel(overlay, "Sent to app ✓", "#4caf50");
          setTimeout(() => setLabel(overlay, "Send page URL to Grabber", "#b388ff"), 2500);
        } else {
          setLabel(overlay, (resp && resp.error) || "Failed — is the app running?", "#e53935");
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
      background: rgba(20,20,20,.92); color: #fff; font: 12px system-ui, sans-serif;
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
      } catch (e) { stream = null; }
      if (stream && stream.getVideoTracks().length === 0) {
        stream.getTracks().forEach((t) => t.stop());
        stream = null;
      }
      if (!stream) {
        // R.2: element capture unavailable (DRM / no tracks) -> offer tab.
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
        setLabel(overlay, "Capture cancelled", "#e53935");
        return;
      }
    } else if (mode === "screen") {
      try {
        stream = await navigator.mediaDevices.getDisplayMedia({
          video: { frameRate: { ideal: 30, max: 60 } },
          audio: true,
        });
      } catch (e) {
        setLabel(overlay, "Capture cancelled", "#e53935");
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
        setLabel(overlay, "Recorder unsupported", "#e53935");
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
          setLabel(overlay, "Saved ✓ (browser) — Record again", "#4caf50");
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
            setLabel(overlay, "Sent to app ✓ — Record again", "#4caf50");
          } else {
            const data = await res.json().catch(() => ({}));
            setLabel(overlay, (data.error || "Upload rejected").slice(0, 40), "#e53935");
          }
        } catch (e) {
          saveBlob(blob, guessName);
          setLabel(overlay, "Saved ✓ (browser) — Record again", "#4caf50");
        }
        RECORDERS.delete(video);
      });
    };
    recorder.start(1000);
    RECORDERS.set(video, state);
    makeRecIndicator(state, overlay);
    recNotify("Video Grabber", `Recording started — ${usedMode} (${recQuality}).`);
    setLabel(overlay, "● Recording — see indicator", "#e53935");
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
    if (HANDLED.has(video) || DISMISSED.has(video)) return;
    HANDLED.add(video);

    const overlay = makeOverlay(video);
    const isBlob = () => (video.currentSrc || "").startsWith("blob:");
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
                         force_page_url = false } = {}) {
      closeMenu();
      setLabel(overlay, "Sending…");
      let base = (document.title || "video").replace(/[\\/:*?"<>|]/g, "_").slice(0, 80);
      // Strip an extension the page title already carries so we never
      // produce "video.mp4.mp4".
      base = base.replace(/\.(mp4|m4v|mov|webm|mkv|mp3|m4a|aac|wav|flac)$/i, "");
      // document.title has no extension — borrow it from the stream URL;
      // if there is none, omit filename so the backend guesses it.
      let ext = "";
      try {
        ext = (new URL(video.currentSrc).pathname.match(/(\.[A-Za-z0-9]{1,5})$/) || [""])[0];
      } catch (e) {}
      const payload = {
        // Normal picks open the app's confirmation dialog (IDM-style);
        // bypass_dialog queues directly.
        type: bypass_dialog ? "RELAY_DOWNLOAD" : "SHOW_ADD_DIALOG",
        url: force_page_url ? location.href : video.currentSrc,
        pageUrl: location.href,
        filename: ext ? base + ext : undefined,
        format_id: format_id,
        target_format: target_format,
        download_playlist: download_playlist,
      };
      chrome.runtime.sendMessage(payload, (resp) => {
        if (chrome.runtime.lastError) {
          setLabel(overlay, "Extension error", "#e53935");
          return;
        }
        if (resp && resp.ok) {
          setLabel(overlay, resp.auto_queued ? "Queued ✓" : "Sent to app ✓", "#4caf50");
          setTimeout(() => setLabel(overlay, "Download ▾", "#4caf50"), 2500);
        } else {
          setLabel(overlay, (resp && resp.error) || "Failed — is the app running?", "#e53935");
        }
      });
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

    if (isPlaylistUrl(location.href)) {
      // On YouTube (and any extractor site), video.currentSrc is a blob:
      // URL that yt-dlp can't fetch. The playlist entries are always the
      // PAGE URL — yt-dlp walks the &list= param itself.
      addMenuItem(menu, "Download this video only", {},
        () => sendChoice({ download_playlist: false, force_page_url: true }));
      addMenuItem(menu, "Download entire playlist", {},
        () => sendChoice({ download_playlist: true, force_page_url: true }));
      addDivider(menu);
    }

      // Mode picker: the menu offers every mode that makes sense for this
      // source, and the click handler always opens it.
      if (blob || mse) {
        // Blob/MSE sources: no direct file. On extractor sites, ask the
        // backend to resolve the PAGE URL — that gives us the same quality
        // ladder IDM shows. On non-extractor MSE sites, only recording.
        if (isExtractorSite()) {
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
          probeFormatsCached(location.href).then((data) => {
            if (seq !== menuSeq || !menuOpen) return;
            loadingEl.remove();
            const formats = (data && data.formats) || [];
            if (!formats.length) {
              const msg = (data && data.error)
                ? `No formats (${data.error})`
                : "No format list available";
              addMenuItem(fmtBox, msg, { dim: true, header: true });
            } else {
              // Task 2: no client-side truncation — the backend already
              // returns a sorted, sanity-capped list (best quality first).
              for (const f of formats) {
                const tf = f.ext === "webm" ? "webm" : null;
                addMenuItem(fmtBox, formatLabel(f), {},
                  () => {
                    closeMenu();
                    setLabel(overlay, "Sending…");
                    chrome.runtime.sendMessage({
                      type: "SHOW_ADD_DIALOG",
                      url: location.href,
                      pageUrl: location.href,
                      filename: (document.title || "video")
                        .replace(/[\\/:*?"<>|]/g, "_").slice(0, 80),
                      format_id: f.format_id,
                      target_format: tf,
                    }, (resp) => {
                      if (chrome.runtime.lastError) {
                        setLabel(overlay, "Extension error", "#e53935");
                        return;
                      }
                      if (resp && resp.ok) {
                        setLabel(overlay,
                          resp.auto_queued ? "Queued ✓" : "Sent to app ✓",
                          "#4caf50");
                        setTimeout(() => setLabel(overlay, "Download ▾", "#4caf50"), 2500);
                      } else {
                        setLabel(overlay,
                          (resp && resp.error) || "Failed", "#e53935");
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

        // Non-extractor MSE: only recording is possible.
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
        for (const f of formats) {
          const tf = f.ext === "webm" ? "webm" : null;
          addMenuItem(fmtBox, formatLabel(f), {},
            () => sendChoice({ format_id: f.format_id, target_format: tf }));
        }
      });
    }

    const refresh = () => {
      if (RECORDERS.has(video)) return; // don't clobber label mid-recording
      if (isMseOnlySite()) {
        setLabel(overlay, "Send page URL ▾", "#b388ff");
        return;
      }
      if (!video.currentSrc) {
        setLabel(overlay, "Waiting for video…", "#999");
        return;
      }
      if (isBlob()) {
        setLabel(overlay, "Record this video ▾", "#e53935");
      } else {
        setLabel(overlay, "Download ▾", "#4caf50");
      }
    };
    refresh();
    video.addEventListener("loadedmetadata", refresh);
    video.addEventListener("emptied", refresh);

    overlay.btn.addEventListener("click", (e) => {
      e.stopPropagation();
      e.preventDefault();
      if (RECORDERS.has(video)) { handleRecordToggle(video, overlay); return; }
      // MSE-only sites (Twitch) have no currentSrc yet but still need the menu —
      // it offers "Send page URL to yt-dlp". Only bail when there's truly nothing.
      if (!video.currentSrc && !isBlob() && !isMseOnlySite()) return;
      toggleMenu();
    });
  }

  function scan(root = document) {
    root.querySelectorAll("video").forEach(wire);
  }

  scan();
  const mo = new MutationObserver((mutations) => {
    for (const m of mutations) {
      m.addedNodes.forEach((node) => {
        if (!(node instanceof Element)) return;
        if (node.tagName === "VIDEO") wire(node);
        else node.querySelectorAll && node.querySelectorAll("video").forEach(wire);
      });
    }
  });
  mo.observe(document.documentElement, { childList: true, subtree: true });

  // Some SPA players swap currentSrc without any mutation nearby — poll lightly.
  setInterval(scan, 2000);
})();
