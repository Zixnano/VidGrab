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

  function isVisible(el) {
    const r = el.getBoundingClientRect();
    return r.width > 80 && r.height > 60;
  }

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

    wrap.appendChild(btn);
    wrap.appendChild(closeBtn);
    document.documentElement.appendChild(wrap);

    function position() {
      const r = video.getBoundingClientRect();
      wrap.style.top = Math.max(8, r.top + 8) + "px";
      wrap.style.left = Math.max(8, r.left + 8) + "px";
      wrap.style.display = isVisible(video)
        && r.bottom > 0 && r.top < innerHeight
        && r.right > 0 && r.left < innerWidth
        ? "flex" : "none";
    }
    position();
    const reposition = () => requestAnimationFrame(position);
    window.addEventListener("scroll", reposition, true);
    window.addEventListener("resize", reposition);
    const ro = new ResizeObserver(reposition);
    ro.observe(video);

    return { wrap, btn, label, icon, position };
  }

  function setLabel(overlay, text, color) {
    overlay.label.textContent = text;
    if (color) overlay.icon.style.color = color;
  }

  async function handleDirectDownload(video, overlay) {
    setLabel(overlay, "Sending…");
    const base = (document.title || "video").replace(/[\\/:*?"<>|]/g, "_").slice(0, 80);
    // document.title never carries a file extension, which made the app save
    // files with no extension. Borrow the real one from the stream URL's
    // path; if the URL has none either, omit filename entirely and let the
    // backend guess it from the URL / Content-Type (server-side fix).
    let ext = "";
    try {
      ext = (new URL(video.currentSrc).pathname.match(/(\.[A-Za-z0-9]{1,5})$/) || [""])[0];
    } catch (e) {}
    const payload = {
      type: "RELAY_DOWNLOAD",
      url: video.currentSrc,
      pageUrl: location.href,
    };
    if (ext) payload.filename = base + ext;
    chrome.runtime.sendMessage(
      payload,
      (resp) => {
        if (chrome.runtime.lastError) {
          setLabel(overlay, "Extension error", "#e53935");
          return;
        }
        if (resp && resp.ok) {
          setLabel(overlay, "Sent to app ✓", "#4caf50");
          setTimeout(() => setLabel(overlay, "Download this video", "#4caf50"), 2500);
        } else {
          setLabel(overlay, (resp && resp.error) || "Failed — is the app running?", "#e53935");
        }
      }
    );
  }

  async function handleSendPageUrl(video, overlay) {
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
    a.download = `${filenameBase || "recording"}.webm`;
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
      // Hand the recording to the desktop app instead of <a download> (which
      // drops it in the browser's Downloads folder the app never watches).
      // FormData can't cross chrome.runtime.sendMessage, so we pass the raw
      // ArrayBuffer (structured-cloneable) and the service worker builds the
      // multipart request. Tradeoff vs base64: no +33% bloat, but the whole
      // buffer is held in memory — if the app is down or the upload fails,
      // the <a download> fallback below still saves the recording.
      blob.arrayBuffer().then((buffer) => {
        chrome.runtime.sendMessage(
          { type: "UPLOAD_RECORDING", buffer, filename: guessName + ".webm" },
          (resp) => {
            if (chrome.runtime.lastError || !resp || !resp.ok) {
              saveBlob(blob, guessName);
              setLabel(overlay, "Saved ✓ (browser) — Record again", "#4caf50");
            } else {
              setLabel(overlay, "Sent to app ✓ — Record again", "#4caf50");
            }
            RECORDERS.delete(video);
          }
        );
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

    const refresh = () => {
      if (RECORDERS.has(video)) return; // don't clobber label mid-recording
      if (isMseOnlySite()) {
        setLabel(overlay, "Send page URL to Grabber", "#b388ff");
        return;
      }
      if (!video.currentSrc) {
        setLabel(overlay, "Waiting for video…", "#999");
        return;
      }
      if (isBlob()) {
        setLabel(overlay, "Record this video", "#e53935");
      } else {
        setLabel(overlay, "Download this video", "#4caf50");
      }
    };
    refresh();
    video.addEventListener("loadedmetadata", refresh);
    video.addEventListener("emptied", refresh);

    overlay.btn.addEventListener("click", (e) => {
      e.stopPropagation();
      e.preventDefault();
      if (isMseOnlySite()) {
        handleSendPageUrl(video, overlay);
      } else if (isBlob()) {
        handleRecordToggle(video, overlay);
      } else if (video.currentSrc) {
        handleDirectDownload(video, overlay);
      }
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
