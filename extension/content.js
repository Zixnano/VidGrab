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
      wrap.remove();
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

  async function handleSendPageUrl(video, overlay, closeMenu) {
    if (closeMenu) closeMenu();
    setLabel(overlay, "Sending…");
    const guessName = (document.title || "video").replace(/[\\/:*?"<>|]/g, "_").slice(0, 80);
    chrome.runtime.sendMessage(
      {
        type: "RELAY_DOWNLOAD",
        url: location.href,
        pageUrl: location.href,
        filename: guessName,
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

  function handleRecordToggle(video, overlay) {
    const state = RECORDERS.get(video);
    if (state && state.recorder && state.recorder.state === "recording") {
      state.recorder.stop();
      return;
    }
    let stream;
    try {
      stream = video.captureStream ? video.captureStream() : video.mozCaptureStream();
    } catch (e) {
      setLabel(overlay, "Can't capture (DRM?)", "#e53935");
      return;
    }
    if (!stream) {
      setLabel(overlay, "Can't capture (DRM?)", "#e53935");
      return;
    }
    if (stream.getVideoTracks().length === 0) {
      // Audio-only capture is useless for video — release the stream.
      stream.getTracks().forEach((t) => t.stop());
      setLabel(overlay, "Can't record on this site", "#e53935");
      return;
    }
    const chunks = [];
    let recorder;
    try {
      recorder = new MediaRecorder(stream, { mimeType: "video/webm" });
    } catch (e) {
      setLabel(overlay, "Recorder unsupported", "#e53935");
      return;
    }
    recorder.ondataavailable = (e) => {
      if (e.data && e.data.size > 0) chunks.push(e.data);
    };
    recorder.onstop = () => {
      const blob = new Blob(chunks, { type: "video/webm" });
      const guessName = (document.title || "recording").replace(/[\\/:*?"<>|]/g, "_").slice(0, 80);
      // Hand the recording to the desktop app. The blob is POSTed directly
      // from here — chrome.runtime.sendMessage caps messages around 32MB,
      // and truncated payloads were producing corrupt files. Only a tiny
      // one-time nonce crosses the message channel.
      chrome.runtime.sendMessage({ type: "GET_UPLOAD_NONCE" }, async (nresp) => {
        if (chrome.runtime.lastError || !nresp || !nresp.ok || !nresp.nonce) {
          // App unreachable / unpaired — browser download so the take
          // isn't lost.
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
            // The app received the file but rejected it (e.g. corrupt
            // recording). Surface the error — do NOT silently drop a
            // broken file into the browser's Downloads folder.
            const data = await res.json().catch(() => ({}));
            setLabel(overlay, (data.error || "Upload rejected").slice(0, 40), "#e53935");
          }
        } catch (e) {
          // Network-level failure mid-upload (app killed, etc.) — the
          // take only exists in this blob, so save it locally.
          saveBlob(blob, guessName);
          setLabel(overlay, "Saved ✓ (browser) — Record again", "#4caf50");
        }
        RECORDERS.delete(video);
      });
    };
    recorder.start(1000);
    RECORDERS.set(video, { recorder });
    setLabel(overlay, "● Recording — click to stop", "#e53935");
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
                         bypass_dialog = false } = {}) {
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
        url: video.currentSrc,
        pageUrl: location.href,
        filename: ext ? base + ext : undefined,
        format_id: format_id,
        target_format: target_format,
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
              for (const f of formats.slice(0, 12)) {
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
          addMenuItem(menu, "Record live now", {},
            () => { closeMenu(); handleRecordToggle(video, overlay); });
          return;
        }

        // Non-extractor MSE: only recording is possible.
        addMenuItem(menu, "Record live now", {},
          () => { closeMenu(); handleRecordToggle(video, overlay); });
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
      addMenuItem(menu, "Record live instead", {},
        () => { closeMenu(); handleRecordToggle(video, overlay); });
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
        for (const f of formats.slice(0, 12)) {
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
