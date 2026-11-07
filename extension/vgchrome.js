// vgchrome.js: Chrome / Vivaldi API reliability layer. Every helper resolves,
// never throws, and never leaves chrome.runtime.lastError unread. Features that
// are missing in a given browser (side panel, notifications, favicons) are
// detected once and callers branch on VGCHROME.caps().
(function (root) {
  "use strict";
  const log = () => root.VGLOG || { debug() {}, info() {}, warn() {}, error() {} };
  const api = () => root.chrome;

  function has(path) {
    try {
      let o = api();
      for (const p of String(path).split(".")) { if (o == null) return false; o = o[p]; }
      return o != null;
    } catch (e) { return false; }
  }
  let _caps = null;
  function caps(force) {
    if (_caps && !force) return _caps;
    _caps = {
      sidePanel: has("sidePanel.setOptions") && has("sidePanel.open"),
      sidePanelBehavior: has("sidePanel.setPanelBehavior"),
      notifications: has("notifications.create"),
      contextMenus: has("contextMenus.create"),
      downloads: has("downloads.onCreated"),
      cookies: has("cookies.getAll"),
      commands: has("commands.onCommand"),
      alarms: has("alarms.create"),
      storageSession: has("storage.session.set"),
      scripting: has("scripting.executeScript"),
      favicon: has("runtime.getURL"),  // the _favicon URL is probed at use time
      webRequest: has("webRequest.onHeadersReceived"),
      desktopApp: null,                // filled by VGCHROME.detectDesktop()
    };
    return _caps;
  }

  // Promise wrapper for callback or promise style APIs. Never rejects.
  function call(path, ...args) {
    return new Promise((resolve) => {
      try {
        const parts = String(path).split(".");
        let ctx = api(), fn = api();
        for (const p of parts) { ctx = fn; fn = fn == null ? undefined : fn[p]; }
        if (typeof fn !== "function") { resolve({ ok: false, error: "unsupported: " + path }); return; }
        let settled = false;
        const done = (v) => { if (!settled) { settled = true; resolve(v); } };
        const cb = (r) => {
          const le = api().runtime && api().runtime.lastError;
          if (le) { log().debug("chrome", path + " lastError: " + le.message); done({ ok: false, error: le.message }); }
          else done({ ok: true, value: r });
        };
        const ret = fn.apply(ctx, args.concat([cb]));
        // Sweep (v4.5.1): a synchronous API (runtime.getURL ...) returns its value
        // and never calls the callback, so call() used to hang forever.
        if (ret !== undefined && !(ret && typeof ret.then === "function")) { done({ ok: true, value: ret }); return; }
        if (ret && typeof ret.then === "function") {
          ret.then((v) => done({ ok: true, value: v }), (e) => done({ ok: false, error: String(e && e.message ? e.message : e) }));
        }
      } catch (e) {
        resolve({ ok: false, error: String(e && e.message ? e.message : e) });
      }
    });
  }

  const TRANSIENT = /receiving end does not exist|message port closed|extension context invalidated|could not establish connection/i;
  // runtime.sendMessage with timeout and one retry for a sleeping service worker.
  async function send(msg, opts) {
    opts = opts || {};
    const timeout = opts.timeout || 25000;
    const tries = opts.retries == null ? 1 : opts.retries;
    for (let attempt = 0; attempt <= tries; attempt++) {
      const r = await Promise.race([
        call("runtime.sendMessage", msg),
        new Promise((res) => setTimeout(() => res({ ok: false, error: "timeout" }), timeout)),
      ]);
      if (r.ok) return r.value == null ? { ok: false, error: "No answer from the background worker." } : r.value;
      if (attempt < tries && (TRANSIENT.test(r.error || "") || r.error === "timeout")) {
        await new Promise((res) => setTimeout(res, 250 * (attempt + 1)));
        continue;
      }
      log().warn("chrome", "sendMessage failed: " + r.error, { type: msg && msg.type });
      return { ok: false, error: r.error };
    }
    return { ok: false, error: "unreachable" };
  }

  async function storageGet(keys, area) {
    const r = await call("storage." + (area || "local") + ".get", keys);
    return r.ok && r.value ? r.value : {};
  }
  async function storageSet(obj, area) { return call("storage." + (area || "local") + ".set", obj); }

  async function notify(message, title) {
    if (!caps().notifications) return { ok: false, error: "unsupported" };
    let icon = "";
    try { icon = api().runtime.getURL("icons/icon128.png"); } catch (e) {}
    return call("notifications.create", "", { type: "basic", iconUrl: icon, title: title || "Video Grabber", message: String(message || "") });
  }

  async function activeTab() {
    const r = await call("tabs.query", { active: true, lastFocusedWindow: true });
    return r.ok && r.value && r.value[0] ? r.value[0] : null;
  }

  // Ask the backend (through background) whether the desktop app is reachable and
  // what it supports. Result is cached by the caller.
  async function detectDesktop() {
    const r = await send({ type: "VG_DESKTOP_INFO" }, { timeout: 6000, retries: 1 });
    caps().desktopApp = !!(r && r.ok && r.alive);
    return r;
  }

  root.VGCHROME = { has, caps, call, send, storageGet, storageSet, notify, activeTab, detectDesktop, TRANSIENT };
  if (typeof module !== "undefined" && module.exports) module.exports = root.VGCHROME;
})(typeof self !== "undefined" ? self : globalThis);
