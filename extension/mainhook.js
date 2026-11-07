// mainhook.js — Universal Video Grabber
// Runs in the page's MAIN world at document_start (manifest content_scripts
// entry with "world": "MAIN", claude.ai only). It catches blob-anchor
// downloads (<a download href="blob:...">.click()), which never reach the
// app through chrome.downloads, and hands the Blob to content.js (isolated
// world) by reference via window.postMessage. content.js uploads it to the
// app. If anything goes wrong the original click is replayed on a fresh
// object URL, so the browser still saves the file.
//
// v4.4: it also only intercepts a click that follows real user input (a
// trusted click / key press / touch in the last 15 s, or the browser's own
// transient user activation). SPA code that builds a blob and clicks an anchor
// by itself, with nobody asking for a download (state exports, cache
// writes, startup work in restored tabs), now falls straight through to the
// browser untouched. No size floor on purpose: a real download from the page
// (a 200-byte code file, say) must still be caught.
//
// The hook only intercepts while "armed". content.js decides that (app
// running + paired + intercept option on + blobHookEnabled on) and pushes it
// in. Not armed means every click falls straight through, unchanged.
(() => {
  const ACK_MS = 1500;   // isolated side must ack within this or we replay natively
  const LRU_CAP = 50;
  const HOLD_MS = 5 * 60 * 1000;   // sweep: after an ack, drop a hand-off that never reports done or replay
  const GESTURE_MS = 15000;  // how long a click / key press keeps the hook willing

  let armed = false;
  // TODO(v4.5.1): a page script that posts a valid vg-armed first still wins the secret (no out-of-band channel between worlds).
  // B7 (v4.5.1): adopted from the first vg-armed message ONLY. The message
  // handler below returns before touching `secret` for every other type, so a
  // page script cannot plant one with a vg-blob-* message; tests/mainhook.test.js
  // pins that. A page script that posts a valid vg-armed first still wins:
  // this is a nuisance barrier, not a security boundary (see content.js).
  let secret = null;
  let debug = false;

  // ---- real user input tracking -----------------------------------------
  // Capture phase on window runs before any page handler, and isTrusted is
  // false for events a page script dispatches itself.
  let lastGesture = 0;
  const markGesture = (e) => { try { if (e && e.isTrusted) lastGesture = Date.now(); } catch (err) {} };
  for (const ev of ["pointerdown", "mousedown", "keydown", "touchstart", "click"]) {
    try { window.addEventListener(ev, markGesture, true); } catch (e) {}
  }
  const hasGesture = () => {
    try {
      if (navigator.userActivation && navigator.userActivation.isActive) return true;
    } catch (e) {}
    return Date.now() - lastGesture < GESTURE_MS;
  };

  const dbg = (...a) => { if (debug) { try { console.log("[VG hook]", ...a); } catch (e) {} } };

  // Originals, captured before anything is patched. click and dispatchEvent
  // are inherited (HTMLElement / EventTarget), so these resolve upward.
  const origCOU = URL.createObjectURL;
  const origRevoke = URL.revokeObjectURL;
  const origClick = HTMLAnchorElement.prototype.click;
  const origDispatch = HTMLAnchorElement.prototype.dispatchEvent;
  const origCreateEl = Document.prototype.createElement;

  const isBlob = (o) => {
    const t = Object.prototype.toString.call(o);
    return t === "[object Blob]" || t === "[object File]";
  };

  // ---- blob-url -> Blob, small LRU --------------------------------------
  const BLOBS = new Map();
  URL.createObjectURL = new Proxy(origCOU, {
    apply(target, thisArg, args) {
      const url = Reflect.apply(target, thisArg, args);
      try {
        if (isBlob(args[0])) {
          BLOBS.delete(url);
          BLOBS.set(url, args[0]);
          while (BLOBS.size > LRU_CAP) BLOBS.delete(BLOBS.keys().next().value);
        }
      } catch (e) {}
      return url;
    },
  });
  // Drop revoked entries so big blobs aren't pinned for the life of the tab.
  // The click happens before the revoke, and pending downloads hold their own
  // Blob reference, so this is safe.
  URL.revokeObjectURL = new Proxy(origRevoke, {
    apply(target, thisArg, args) {
      try { BLOBS.delete(args[0]); } catch (e) {}
      return Reflect.apply(target, thisArg, args);
    },
  });

  // ---- pending hand-offs: id -> {blob, filename, timer} -----------------
  const PENDING = new Map();

  function newId() {
    return Math.random().toString(36).slice(2) + Date.now().toString(36);
  }

  function replay(id) {
    const p = PENDING.get(id);
    if (!p) return;
    PENDING.delete(id);
    clearTimeout(p.timer);
    dbg("replay", id);
    let url = null;
    try {
      url = Reflect.apply(origCOU, URL, [p.blob]);
      const a = Reflect.apply(origCreateEl, document, ["a"]);
      a.href = url;
      a.download = p.filename;
      Reflect.apply(origClick, a, []);   // original click: never re-enters our hook
    } catch (e) {}
    if (url) {
      setTimeout(() => { try { Reflect.apply(origRevoke, URL, [url]); } catch (e) {} }, 60000);
    }
  }

  // download="" (no name) is common for generated files: build a name from the
  // Blob's MIME type so the file lands with a usable extension.
  const TYPE_EXT = {
    "application/pdf": ".pdf", "application/zip": ".zip", "application/json": ".json",
    "text/plain": ".txt", "text/markdown": ".md", "text/csv": ".csv", "text/html": ".html",
    "text/css": ".css", "text/javascript": ".js", "application/javascript": ".js",
    "image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp",
    "image/svg+xml": ".svg", "video/mp4": ".mp4", "audio/mpeg": ".mp3",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
  };
  function nameFor(name, blob) {
    if (name) return name;
    let ext = "";
    try { ext = TYPE_EXT[String(blob.type || "").split(";")[0].trim().toLowerCase()] || ""; } catch (e) {}
    return "download" + ext;
  }

  function blobAnchorInfo(a) {
    try {
      if (!a || typeof a.hasAttribute !== "function" || !a.hasAttribute("download")) return null;
      const href = a.href;
      if (typeof href !== "string" || !href.startsWith("blob:")) return null;
      const blob = BLOBS.get(href);
      if (!blob) return null;
      return { blob, filename: nameFor(a.getAttribute("download") || "", blob) };
    } catch (e) {
      return null;
    }
  }

  // True when the click was taken over (caller must NOT run the native click).
  function tryIntercept(a) {
    if (!armed || secret === null) return false;
    const info = blobAnchorInfo(a);
    if (!info) return false;
    if (!hasGesture()) { dbg("no user input, left to the browser"); return false; }
    try { if (info.blob.size === 0) return false; } catch (e) {}   // nothing to upload
    const id = newId();
    const timer = setTimeout(() => replay(id), ACK_MS);
    PENDING.set(id, { blob: info.blob, filename: info.filename, timer });
    try {
      window.postMessage(
        { type: "vg-blob-download", source: secret, id, blob: info.blob, filename: info.filename },
        "*");
    } catch (e) {
      clearTimeout(timer);
      PENDING.delete(id);
      return false;
    }
    dbg("intercepted", id, info.filename);
    return true;
  }

  HTMLAnchorElement.prototype.click = new Proxy(origClick, {
    apply(target, thisArg, args) {
      if (tryIntercept(thisArg)) return undefined;
      return Reflect.apply(target, thisArg, args);
    },
  });

  HTMLAnchorElement.prototype.dispatchEvent = new Proxy(origDispatch, {
    apply(target, thisArg, args) {
      let isClick = false;
      try { isClick = !!args[0] && args[0].type === "click"; } catch (e) {}
      if (isClick && tryIntercept(thisArg)) return true;
      return Reflect.apply(target, thisArg, args);
    },
  });

  // A person physically clicking a visible <a download href="blob:..."> never
  // calls .click() from script, so the patches above cannot see it. Catch it
  // here, in the capture phase on window, before the browser starts the save.
  // Only trusted clicks (real input), only plain left clicks, and the same
  // armed / gesture / replay rules as every other path.
  window.addEventListener("click", (e) => {
    try {
      if (!e.isTrusted || e.defaultPrevented || e.button !== 0) return;
      if (e.ctrlKey || e.shiftKey || e.altKey || e.metaKey) return;   // browser shortcuts stay native
      const path = typeof e.composedPath === "function" ? e.composedPath() : [];
      let a = null;
      for (const n of path) {
        if (n && n.tagName === "A") { a = n; break; }
      }
      if (!a || !tryIntercept(a)) return;
      e.preventDefault();
      e.stopImmediatePropagation();
    } catch (err) {}
  }, true);

  // ---- messages from content.js (isolated world) -------------------------
  window.addEventListener("message", (e) => {
    if (e.source !== window) return;
    let d, type, src, id, isArmed, wantDebug;
    try {
      d = e.data;
      if (!d || typeof d !== "object") return;
      type = d.type; src = d.source; id = d.id;
      isArmed = d.armed; wantDebug = d.debug;
    } catch (err) {
      return;
    }
    if (type === "vg-armed") {
      if (secret === null) {
        if (typeof src !== "string" || src.length < 16) return;
        secret = src;                         // first message wins
      } else if (src !== secret) {
        return;
      }
      armed = isArmed === true;
      debug = wantDebug === true;
      return;
    }
    if (secret === null || src !== secret || typeof id !== "string") return;
    if (type === "vg-blob-ack") {
      const p = PENDING.get(id);
      if (p) {
        clearTimeout(p.timer);                // isolated side has it; stop the watchdog
        // Sweep (v4.5.1): but do not pin the Blob forever if it never says done/replay.
        p.timer = setTimeout(() => { PENDING.delete(id); }, HOLD_MS);
      } else {
        // The watchdog already replayed this click natively, so an upload now would
        // be a second copy. Tell the isolated side to skip it.
        try { window.postMessage({ type: "vg-blob-cancel", source: secret, id }, "*"); } catch (e) {}
      }
    } else if (type === "vg-blob-done") {
      const p = PENDING.get(id);
      if (p) { clearTimeout(p.timer); PENDING.delete(id); }
    } else if (type === "vg-blob-replay") {
      replay(id);
    }
  }, false);
})();
// Sweep (v4.5.1): a rejected storage read used to abort this whole async block at
// the first await, leaving Save and Test dead. Reads now fall back to {} (and
// blobHookEnabled undefined means ON, matching content.js).
async function safeGet(keys) {
  try { return (await chrome.storage.local.get(keys)) || {}; } catch (e) { return {}; }
}
(async () => {
  const tokenInput = document.getElementById("token");
  const statusEl = document.getElementById("status");
  const interceptCheckbox = document.getElementById("interceptDownloads");
  const gestureCheckbox = document.getElementById("interceptNeedsGesture");
  const ignoreInput = document.getElementById("interceptIgnore");

  // B9 (v4.5.1): the change listener is registered BEFORE the first await so no
  // change can slip past it. If vgSettings changes while the initial read is
  // still in flight, that read holds an older snapshot: changedSinceLoad makes
  // the initial reads below skip their checkbox updates and their migration write.
  let changedSinceLoad = false;
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area === "local" && changes[VG_SETTINGS_KEY]) {
      changedSinceLoad = true;
      const nv = vgNormalize(changes[VG_SETTINGS_KEY].newValue);
      interceptCheckbox.checked = !!nv.interceptDownloads;
      gestureCheckbox.checked = nv.interceptNeedsGesture;
      if (document.activeElement !== ignoreInput) ignoreInput.value = nv.interceptIgnore;
    }
  });

  const { apiToken } = await safeGet("apiToken");
  if (apiToken) tokenInput.value = apiToken;
  // Batch 3: the switch lives in vgSettings (vgsettings.js) — the same value
  // the popup's Downloads toggle edits, and the one background.js reads.
  // The old standalone "interceptDownloads" key is only a migration
  // fallback: if vgSettings has never carried the switch, adopt the legacy
  // value once (mirrors the migration in background.js).
  {
    const stored = await safeGet([VG_SETTINGS_KEY, "interceptDownloads"]);
    const rawVg = stored[VG_SETTINGS_KEY];
    if (changedSinceLoad) {
      // B9: a newer value already reached the checkbox through the listener.
    } else if (!(rawVg && typeof rawVg.interceptDownloads === "boolean")
        && typeof stored.interceptDownloads === "boolean") {
      const adopted = vgNormalize({ ...(rawVg || {}), interceptDownloads: stored.interceptDownloads });
      try { await chrome.storage.local.set({ [VG_SETTINGS_KEY]: adopted }); } catch (e) {}
      interceptCheckbox.checked = adopted.interceptDownloads;
    } else {
      interceptCheckbox.checked = !!vgNormalize(rawVg).interceptDownloads;
    }
  }
  interceptCheckbox.addEventListener("change", async () => {
    const cur = vgNormalize((await chrome.storage.local.get(VG_SETTINGS_KEY))[VG_SETTINGS_KEY]);
    await chrome.storage.local.set({
      [VG_SETTINGS_KEY]: { ...cur, interceptDownloads: interceptCheckbox.checked },
    });
  });
  // v4.4: the click-required switch and the ignore list live in vgSettings too.
  {
    const cur = vgNormalize((await safeGet(VG_SETTINGS_KEY))[VG_SETTINGS_KEY]);
    if (!changedSinceLoad) {   // B9: never paint a stale snapshot over a newer value
      gestureCheckbox.checked = cur.interceptNeedsGesture;
      ignoreInput.value = cur.interceptIgnore;
    }
  }
  async function saveVg(patch) {
    // Write path: a failed read must throw, not fall back to defaults and overwrite every setting.
    const cur = vgNormalize((await chrome.storage.local.get(VG_SETTINGS_KEY))[VG_SETTINGS_KEY]);
    await chrome.storage.local.set({ [VG_SETTINGS_KEY]: vgNormalize({ ...cur, ...patch }) });
  }
  gestureCheckbox.addEventListener("change", () => saveVg({ interceptNeedsGesture: gestureCheckbox.checked }));
  ignoreInput.addEventListener("change", () => saveVg({ interceptIgnore: ignoreInput.value }));
  // Keeping this page honest when the popup toggle flips a switch meanwhile is
  // the onChanged listener registered at the top of this file (B9).

  const blobHookCheckbox = document.getElementById("blobHookEnabled");
  const { blobHookEnabled } = await safeGet("blobHookEnabled");
  blobHookCheckbox.checked = blobHookEnabled !== false;
  blobHookCheckbox.addEventListener("change", async () => {
    await chrome.storage.local.set({ blobHookEnabled: blobHookCheckbox.checked });
  });

  document.getElementById("test").addEventListener("click", async () => {
    statusEl.textContent = "Contacting app…";
    statusEl.className = "";
    try {
      const ping = await fetch("http://127.0.0.1:5757/ping");
      if (ping.status === 404) {
        statusEl.textContent = "App reachable but outdated — update Video Grabber, then retry.";
        statusEl.className = "bad";
        return;
      }
      if (!ping.ok) throw new Error("no response");
      const state = await ping.json().catch(() => ({}));
      if (state.paired) {
        if (!tokenInput.value.trim()) {
          statusEl.textContent = "App is paired, but this extension has no token — copy it from the app's Tools menu.";
          statusEl.className = "bad";
          return;
        }
        statusEl.textContent = "App reachable ✓ — already paired this session.";
        statusEl.className = "ok";
        return;
      }
      const res = await fetch("http://127.0.0.1:5757/pair", { method: "POST" });
      if (res.ok) {
        const data = await res.json();
        if (data.token) {
          await chrome.storage.local.set({ apiToken: data.token });
          tokenInput.value = data.token;
          statusEl.textContent = "Paired ✓";
          statusEl.className = "ok";
          return;
        }
      }
      statusEl.textContent = "App reachable but pairing refused — restart the app to re-pair.";
      statusEl.className = "bad";
    } catch (e) {
      statusEl.textContent = "App unreachable — is Video Grabber running?";
      statusEl.className = "bad";
    }
  });

  // v5: show the connection state as soon as the page opens (read-only:
  // /ping never pairs, so this can't use up the once-per-session pairing).
  try {
    const ping = await fetch("http://127.0.0.1:5757/ping");
    if (ping.ok) {
      const state = await ping.json().catch(() => ({}));
      const paired = state.paired || !!tokenInput.value.trim();
      statusEl.textContent = paired
        ? "App running" + (state.version ? ` (v${state.version})` : "") + (tokenInput.value.trim() ? " ✓" : " but this extension has no token. Press Test connection.")
        : "App running, not paired yet. Press Test connection.";
      statusEl.className = paired && tokenInput.value.trim() ? "ok" : "bad";
    }
  } catch (e) {
    statusEl.textContent = "App not running. Launch Video Grabber, then press Test connection.";
    statusEl.className = "bad";
  }

  document.getElementById("save").addEventListener("click", async () => {
    const value = tokenInput.value.trim();
    if (!value) {
      statusEl.textContent = "Paste a token first.";
      statusEl.className = "bad";
      return;
    }
    await chrome.storage.local.set({ apiToken: value });
    statusEl.textContent = "Saved ✓ — try downloading something.";
    statusEl.className = "ok";
  });
})();
