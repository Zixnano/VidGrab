// Boots the real background.js (+ bg-features.js) in a vm sandbox with a fake
// chrome.* and a fake desktop app. Nothing is mocked inside the extension code.
const vm = require("node:vm");
const fs = require("node:fs");
const path = require("node:path");
const ROOT = path.join(__dirname, "..");

function makeApp(opts) {
  const app = { alive: true, token: "TOK", jobs: {}, calls: [], routes: Array.isArray(opts) ? opts : undefined, queue: { running: false }, nextId: 1, failSend: null };
  app.handle = (url, init) => {
    const u = new URL(url); const method = (init && init.method) || "GET"; const p = u.pathname;
    const body = init && init.body ? JSON.parse(init.body) : {};
    const hdr = (init && init.headers) || {};
    const ok = (data, status) => ({ ok: (status || 200) < 400, status: status || 200, json: async () => data, text: async () => JSON.stringify(data), headers: { get: () => null }, blob: async () => ({}) });
    if (!app.alive) throw new TypeError("Failed to fetch");
    app.calls.push({ method, path: p, body });
    if (p === "/health") return ok({ ok: true, app: "videograbber", version: "9.9" });
    if (method === "OPTIONS") return ok({}, (app.routes || ["/jobs", "/queue-status", "/queue-running", "/probe-formats", "/probe-head", "/redownload/x", "/clear-failed", "/reorder/x", "/dup-check", "/open-folder", "/diskspace", "/stats"]).includes(p) ? 200 : 404);
    if (p === "/pair") return ok({ token: app.newToken || app.token });
    if (hdr["X-API-Token"] !== app.token) return ok({ error: "unauthorized" }, 401);
    if (p === "/download") { if (app.failSend) return ok({ error: app.failSend }, 500); const id = "j" + app.nextId++; app.jobs[id] = { id, status: "queued", url: body.url, filename: body.filename, created_ts: app.nextId }; return ok({ job_id: id }); }
    if (p === "/batch") { const ids = (body.items || []).map(() => "j" + app.nextId++); return ok({ job_ids: ids }); }
    if (p === "/jobs") return ok(app.jobs);
    if (p === "/queue-status") return ok({ running: app.queue.running, queued: 1 });
    if (p === "/queue-running") { app.queue.running = !!body.running; return ok({ ok: true, queue_running: app.queue.running }); }
    if (p.startsWith("/redownload/")) { const id = p.split("/")[2]; if (!app.jobs[id]) return ok({ error: "unknown job" }, 404); app.jobs[id].status = "queued"; return ok({ ok: true }); }
    if (p.startsWith("/pause/") || p.startsWith("/stop/") || p.startsWith("/resume/") || p.startsWith("/delete/")) return ok({ ok: true });
    if (p === "/clear-failed") { let n = 0; for (const k of Object.keys(app.jobs)) if (app.jobs[k].status === "error") { delete app.jobs[k]; n++; } return ok({ removed: n }); }
    if (p === "/show-add-dialog") { const id = "j" + app.nextId++; return ok({ ok: true, job_id: id, auto_queued: true }); }
    if (p.startsWith("/reorder/")) return ok({ ok: true, position: 1 });
    if (p === "/open-folder") return ok({ ok: true });
    if (p === "/theme") return ok({});
    return ok({ error: "no route " + p }, 404);
  };
  return app;
}

function boot(opts) {
  opts = opts || {};
  const app = makeApp(opts.app);
  const store = { local: Object.assign({ apiToken: "TOK" }, opts.local || {}), session: {} };
  const listeners = { runtimeMsg: [], alarms: [], menus: [], cmds: [], notifClick: [], notifBtn: [], webReq: [], webBefore: [], webHeaders: [], navBefore: [], navHist: [], tabRemoved: [], onCreated: [] };
  const created = { menus: [], notifications: [], downloads: [], cleared: [] };
  const area = (name) => ({
    get: async (k) => { if (k == null) return { ...store[name] }; const ks = Array.isArray(k) ? k : [k]; const o = {}; for (const x of ks) if (x in store[name]) o[x] = store[name][x]; return o; },
    set: async (o) => { Object.assign(store[name], JSON.parse(JSON.stringify(o))); (listeners.storageChanged || []).forEach((f) => f(Object.fromEntries(Object.keys(o).map((k) => [k, { newValue: o[k] }])), name)); },
    remove: async (k) => { delete store[name][k]; },
    getBytesInUse: async () => JSON.stringify(store[name]).length,
  });
  listeners.storageChanged = [];
  const tabs = opts.tabs || { 1: { id: 1, windowId: 1, url: "https://site.com/watch?v=1", title: "Cool Show" } };
  const ev = (...arrs) => ({ addListener: (f) => arrs.forEach((a) => a.push(f)) });
  const chrome = {
    runtime: { lastError: null, getURL: (p) => "chrome-extension://abc/" + p, getManifest: () => JSON.parse(fs.readFileSync(path.join(ROOT, "manifest.json"), "utf8")),
      onMessage: ev(listeners.runtimeMsg), onInstalled: ev([]), onStartup: ev([]) },
    storage: { local: area("local"), session: area("session"), onChanged: ev(listeners.storageChanged) },
    tabs: { query: async () => Object.values(tabs).map((t) => ({ ...t, active: true })), get: async (id) => { if (!tabs[id]) throw new Error("No tab"); return tabs[id]; },
      onActivated: ev([]), onRemoved: ev(listeners.tabRemoved) },
    windows: { onRemoved: ev([]) },
    action: { setBadgeText: async () => {}, setBadgeBackgroundColor: async () => {} },
    webRequest: { onBeforeRequest: ev(listeners.webReq, listeners.webBefore), onHeadersReceived: ev(listeners.webReq, listeners.webHeaders) },
    webNavigation: { onBeforeNavigate: ev(listeners.navBefore), onHistoryStateUpdated: ev(listeners.navHist) },
    cookies: { getAll: async () => [{ name: "a", value: "b" }] },
    downloads: { onCreated: ev(listeners.onCreated), download: async (o) => { created.downloads.push(o); return 42; }, cancel: async () => {}, erase: async () => {}, search: async () => [] },
    notifications: { create: (id, o) => { created.notifications.push({ id, ...o }); }, clear: (id) => { created.cleared.push(id); }, onClicked: ev(listeners.notifClick), onButtonClicked: ev(listeners.notifBtn) },
    contextMenus: { removeAll: (cb) => { created.menus = []; cb && cb(); }, create: (d, cb) => { created.menus.push(d); cb && cb(); }, onClicked: ev(listeners.menus) },
    commands: { onCommand: ev(listeners.cmds) },
    alarms: { get: (n, cb) => cb(undefined), create: () => {}, onAlarm: ev(listeners.alarms) },
  };
  if (opts.sidePanel) chrome.sidePanel = { setOptions: async () => {}, open: async () => { created.sidePanelOpened = true; } };
  const fetchImpl = async (url, init) => {
    if (/^https?:\/\/(127\.0\.0\.1|localhost)/.test(url)) return app.handle(url, init);
    if (opts.web) return opts.web(url, init);
    throw new Error("unexpected external fetch " + url);
  };
  const ctx = { chrome, console: { debug() {}, info() {}, warn() {}, error() {}, log() {} }, fetch: fetchImpl, setTimeout, clearTimeout, setInterval, URL, URLSearchParams, AbortController, Date, JSON, Promise, Map, Set, Math, RegExp,
    navigator: { userAgent: "TestUA" }, FileReader: class {}, TextEncoder, TextDecoder, Error, Array, Object, String, Number, Boolean, parseInt, parseFloat, isNaN, Infinity, Symbol, decodeURIComponent, encodeURIComponent };
  ctx.self = ctx; ctx.globalThis = ctx;
  ctx.importScripts = (...files) => { for (const f of files) vm.runInContext(fs.readFileSync(path.join(ROOT, f), "utf8"), ctx, { filename: f }); };
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(path.join(ROOT, "background.js"), "utf8"), ctx, { filename: "background.js" });
  // message helper: runs the registered onMessage listeners like Chrome would
  const send = (msg, sender) => new Promise((resolve) => {
    let answered = false;
    for (const l of listeners.runtimeMsg) {
      const r = l(msg, sender || { id: "abc" }, (v) => { answered = true; resolve(v); });
      if (r === true) return;
    }
    setTimeout(() => { if (!answered) resolve(undefined); }, 5);
  });
  return { ctx, chrome, app, store, listeners, created, send, tabs, run: (code) => vm.runInContext(code, ctx) };
}
module.exports = { boot };
