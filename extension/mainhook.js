// mainhook.js — Universal Video Grabber
// Runs in the page's MAIN world at document_start (manifest content_scripts
// entry with "world": "MAIN", claude.ai only). It catches blob-anchor
// downloads (<a download href="blob:...">.click()), which never reach the
// app through chrome.downloads, and hands the Blob to content.js (isolated
// world) by reference via window.postMessage. content.js uploads it to the
// app. If anything goes wrong the original click is replayed on a fresh
// object URL, so the browser still saves the file.
//
// The hook only intercepts while "armed". content.js decides that (app
// running + paired + intercept option on + blobHookEnabled on) and pushes it
// in. Not armed means every click falls straight through, unchanged.
(() => {
  const ACK_MS = 1500;   // isolated side must ack within this or we replay natively
  const LRU_CAP = 50;

  let armed = false;
  let secret = null;     // adopted from the first vg-armed message (nuisance barrier only)
  let debug = false;

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

  function blobAnchorInfo(a) {
    try {
      if (!a || typeof a.hasAttribute !== "function" || !a.hasAttribute("download")) return null;
      const href = a.href;
      if (typeof href !== "string" || !href.startsWith("blob:")) return null;
      const blob = BLOBS.get(href);
      if (!blob) return null;
      return { blob, filename: a.getAttribute("download") || "" };
    } catch (e) {
      return null;
    }
  }

  // True when the click was taken over (caller must NOT run the native click).
  function tryIntercept(a) {
    if (!armed || secret === null) return false;
    const info = blobAnchorInfo(a);
    if (!info) return false;
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
