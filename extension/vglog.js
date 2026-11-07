// vglog.js: small ring-buffer logger shared by the service worker, popup, side
// panel and content scripts. Entries stay in memory and are mirrored (debounced)
// to chrome.storage.session so the Diagnostics view can show what happened even
// after the service worker was restarted. Logging never throws.
(function (root) {
  "use strict";
  const KEY = "vgLog";
  const MAX = 300;
  const LEVELS = { debug: 0, info: 1, warn: 2, error: 3 };
  let buf = [];
  let minLevel = LEVELS.info;
  let timer = null;
  let ctxName = "ext";

  function store() {
    try { return root.chrome && root.chrome.storage && root.chrome.storage.session; } catch (e) { return null; }
  }
  function flush() {
    timer = null;
    const s = store();
    if (!s) return Promise.resolve();
    try { return Promise.resolve(s.set({ [KEY]: buf.slice(-MAX) })).catch(() => {}); } catch (e) { return Promise.resolve(); }
  }
  function schedule() { if (!timer) timer = setTimeout(flush, 500); }

  function safe(v) {
    if (v === undefined) return undefined;
    if (v instanceof Error) return { name: v.name, message: v.message };
    try { const j = JSON.stringify(v); return j && j.length > 600 ? j.slice(0, 600) + "…" : JSON.parse(j); } catch (e) { return String(v).slice(0, 200); }
  }
  function write(level, scope, msg, data) {
    try {
      const lv = LEVELS[level] != null ? level : "info";
      const entry = { t: Date.now(), l: lv, c: ctxName, s: String(scope || ""), m: String(msg == null ? "" : msg).slice(0, 400) };
      const d = safe(data); if (d !== undefined) entry.d = d;
      buf.push(entry);
      if (buf.length > MAX) buf.splice(0, buf.length - MAX);
      if (LEVELS[lv] >= minLevel && root.console) {
        const fn = lv === "debug" ? "debug" : lv;
        root.console[fn]("[VG:" + entry.s + "]", entry.m, d !== undefined ? d : "");
      }
      if (LEVELS[lv] >= LEVELS.warn) schedule();
      else if (LEVELS[lv] >= minLevel) schedule();
    } catch (e) { /* logging must never break the caller */ }
  }
  async function hydrate() {
    const s = store();
    if (!s) return;
    try {
      const st = await s.get(KEY);
      const saved = Array.isArray(st && st[KEY]) ? st[KEY] : [];
      const have = new Set(buf.map((e) => e.t + e.m));
      buf = saved.filter((e) => !have.has(e.t + e.m)).concat(buf).sort((a, b) => a.t - b.t).slice(-MAX);
    } catch (e) { /* session storage unavailable */ }
  }
  function entries(filter) {
    let out = buf.slice();
    if (filter && filter.level) out = out.filter((e) => LEVELS[e.l] >= LEVELS[filter.level]);
    if (filter && filter.scope) out = out.filter((e) => e.s === filter.scope);
    return out;
  }
  function dump(filter) {
    return entries(filter).map((e) => {
      const d = new Date(e.t).toISOString().slice(11, 23);
      return d + " " + e.l.toUpperCase().padEnd(5) + " [" + e.c + ":" + e.s + "] " + e.m + (e.d !== undefined ? " " + JSON.stringify(e.d) : "");
    }).join("\n");
  }
  function clear() { buf = []; const s = store(); if (s) { try { s.remove(KEY); } catch (e) {} } }
  // Wrap an async function so a rejection is logged and turned into {ok:false,error}.
  function guard(scope, fn) {
    return async function (...args) {
      try { return await fn.apply(this, args); }
      catch (e) { write("error", scope, "uncaught: " + (e && e.message ? e.message : e)); return { ok: false, error: String(e && e.message ? e.message : e) }; }
    };
  }
  root.VGLOG = {
    debug: (s, m, d) => write("debug", s, m, d), info: (s, m, d) => write("info", s, m, d),
    warn: (s, m, d) => write("warn", s, m, d), error: (s, m, d) => write("error", s, m, d),
    entries, dump, clear, hydrate, flush, guard,
    setContext(n) { ctxName = String(n || "ext"); }, setLevel(l) { if (LEVELS[l] != null) minLevel = LEVELS[l]; },
    _reset() { buf = []; }, KEY,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = root.VGLOG;
})(typeof self !== "undefined" ? self : globalThis);
