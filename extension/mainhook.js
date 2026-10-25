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
  const GESTURE_MS = 15000;  // how long a click / key press keeps the hook willing

  let armed = false;
  let secret = null;     // adopted from the first vg-armed message (nuisance barrier only)
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
      if (p) clearTimeout(p.timer);           // isolated side has it; stop the watchdog
    } else if (type === "vg-blob-done") {
      const p = PENDING.get(id);
      if (p) { clearTimeout(p.timer); PENDING.delete(id); }
    } else if (type === "vg-blob-replay") {
      replay(id);
    }
  }, false);
})();
