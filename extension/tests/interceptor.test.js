// interceptor.test.js: drives the real background.js inside a mocked Chrome and
// checks what chrome.downloads.onCreated does with a download.
// Run:  node extension/tests/interceptor.test.js        (exit code 1 on failure)
// VG_EXT_DIR can point at another extension folder (default: the folder above).
"use strict";
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const EXT = process.env.VG_EXT_DIR || path.join(__dirname, "..");
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Loads background.js into a fresh context with a recording chrome mock.
async function boot(opts = {}) {
  const store = {
    local: { vgSettings: { interceptDownloads: true, interceptNeedsGesture: opts.needsGesture !== false,
                           interceptIgnore: opts.ignore || "" }, apiToken: "tok" },
    session: { vgSessionStart: opts.sessionStart != null ? opts.sessionStart : Date.now() - 10 * 60000 },
  };
  const listeners = {}, calls = [], fetchCalls = [];
  const cancelFails = () => opts.cancelFails === true;
  const pick = (area, keys) => {
    const out = {};
    const list = keys == null ? Object.keys(store[area]) : (typeof keys === "string" ? [keys] : (Array.isArray(keys) ? keys : Object.keys(keys)));
    for (const k of list) if (k in store[area]) out[k] = store[area][k];
    return out;
  };
  const handlers = {
    "chrome.storage.local.get": (k) => pick("local", k),
    "chrome.storage.session.get": (k) => pick("session", k),
    "chrome.storage.local.set": (o) => { Object.assign(store.local, o); return {}; },
    "chrome.storage.session.set": (o) => { Object.assign(store.session, o); return {}; },
    "chrome.runtime.getManifest": () => ({ version: "test", manifest_version: 3 }),
    "chrome.runtime.getURL": (p) => "chrome-extension://test/" + p,
    "chrome.tabs.query": () => [],
    "chrome.cookies.getAll": () => [],
    "chrome.downloads.cancel": (id) => { calls.push(["cancel", id]); if (cancelFails()) throw new Error("Download must be in progress"); return {}; },
    "chrome.downloads.erase": (q) => { calls.push(["erase", q.id]); return []; },
    "chrome.downloads.download": (o) => { calls.push(["download", o.url]); return 1; },
    "chrome.downloads.search": (q) => [{ id: q.id, filename: "C:\\Users\\me\\Downloads\\file.bin" }],
  };
  const values = { "chrome.runtime.lastError": undefined, "chrome.runtime.id": "test" };
  function deep(p) {
    return new Proxy(function () {}, {
      get(_t, prop) {
        if (prop === "then" || typeof prop === "symbol") return undefined;
        const full = p + "." + String(prop);
        if (full in values) return values[full];
        if (prop === "addListener") return (cb) => { (listeners[p] = listeners[p] || []).push(cb); };
        if (prop === "removeListener" || prop === "hasListener") return () => false;
        return deep(full);
      },
      apply(_t, _this, args) {
        const h = handlers[p];
        try { return Promise.resolve(h ? h(...args) : {}); } catch (e) { return Promise.reject(e); }
      },
    });
  }
  const sandbox = {
    console: { debug() {}, log() {}, info() {}, warn() {}, error() {} },
    setTimeout, clearTimeout, setInterval, clearInterval, URL, URLSearchParams, AbortController, TextEncoder, TextDecoder,
    Promise, Date, JSON, Map, Set, navigator: { userAgent: "VG-Test-UA" },
    chrome: deep("chrome"),
    fetch: async (url, init) => {
      fetchCalls.push({ url: String(url), init });
      const ok = !(opts.dialogFails && String(url).endsWith("/show-add-dialog"));
      return { ok, status: ok ? 200 : 500, json: async () => ({}), headers: { get: () => null } };
    },
    importScripts: (...names) => {
      for (const n of names) vm.runInContext(fs.readFileSync(path.join(EXT, n), "utf8"), ctx, { filename: n });
    },
  };
  sandbox.self = sandbox;
  const ctx = vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(path.join(EXT, "background.js"), "utf8"), ctx, { filename: "background.js" });
  await sleep(30);   // let the startup reads land
  const msgs = listeners["chrome.runtime.onMessage"];
  const created = listeners["chrome.downloads.onCreated"];
  assert.ok(msgs && msgs.length && created && created.length, "background.js registered its listeners");
  return {
    calls, fetchCalls, store, ctx,
    gesture: () => msgs[0]({ type: "VG_GESTURE" }, { tab: { id: 1 } }, () => {}),
    created: (item) => created[0](item),
    dialogPosts: () => fetchCalls.filter((c) => c.url.endsWith("/show-add-dialog")).map((c) => JSON.parse(c.init.body)),
    cancelled: () => calls.filter((c) => c[0] === "cancel").map((c) => c[1]),
    erased: () => calls.filter((c) => c[0] === "erase").map((c) => c[1]),
    reissued: () => calls.filter((c) => c[0] === "download"),
  };
}

const fresh = (extra) => Object.assign({
  id: 7, url: "https://files.example.org/clip.bin", finalUrl: "https://files.example.org/clip.bin",
  referrer: "https://example.org/page", state: "in_progress", startTime: new Date().toISOString(),
}, extra || {});

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

test("<a download> click: gesture, then onCreated -> cancelled and forwarded to the app", async () => {
  const t = await boot();
  t.gesture();                                  // content.js reports the trusted click
  await t.created(fresh());                     // the browser creates the download
  assert.deepStrictEqual(t.cancelled(), [7]);
  const posts = t.dialogPosts();
  assert.strictEqual(posts.length, 1);
  assert.strictEqual(posts[0].url, "https://files.example.org/clip.bin");
  assert.strictEqual(posts[0].referer, "https://example.org/page");
  assert.strictEqual(posts[0].user_agent, "VG-Test-UA");
  assert.deepStrictEqual(t.erased(), [7]);      // browser list tidied
});

test("a gesture 45 s ago is still inside the window", async () => {
  const t = await boot();
  t.store.session.vgLastGesture = Date.now() - 45000;
  await t.created(fresh());
  assert.deepStrictEqual(t.cancelled(), [7]);
});

test("tiny file already complete: forwarded, browser copy kept, nothing re-issued", async () => {
  const t = await boot({ cancelFails: true });
  t.gesture();
  await t.created(fresh({ state: "complete" }));
  assert.strictEqual(t.dialogPosts().length, 1, "still handed to the app");
  assert.deepStrictEqual(t.erased(), [], "a finished file stays in the browser list");
  assert.strictEqual(t.reissued().length, 0);
});

test("interrupted download is forwarded too", async () => {
  const t = await boot();
  t.gesture();
  await t.created(fresh({ state: "interrupted" }));
  assert.strictEqual(t.dialogPosts().length, 1);
});

test("a download from before this browser session is left alone", async () => {
  const t = await boot();
  t.gesture();
  await t.created(fresh({ startTime: new Date(Date.now() - 3600000).toISOString() }));
  assert.deepStrictEqual(t.cancelled(), []);
  assert.strictEqual(t.dialogPosts().length, 0);
});

test("cold wake: the gesture message lands 300 ms after onCreated, still intercepted", async () => {
  const t = await boot();
  const p = t.created(fresh());
  setTimeout(() => t.gesture(), 300);
  await p;
  assert.deepStrictEqual(t.cancelled(), [7]);
});

test("no user action at all: not cancelled, but mirrored once to the app", async () => {
  const t = await boot();
  await t.created(fresh());
  assert.deepStrictEqual(t.cancelled(), [], "the browser keeps its download");
  assert.deepStrictEqual(t.erased(), []);
  assert.strictEqual(t.dialogPosts().length, 1, "the URL still reaches the app");
  await t.created(fresh({ id: 8 }));            // same URL again: deduplicated
  assert.strictEqual(t.dialogPosts().length, 1);
});

test("mirror is quiet right after browser start-up", async () => {
  const t = await boot({ sessionStart: Date.now() });
  await t.created(fresh({ startTime: new Date(Date.now() + 1000).toISOString() }));
  assert.strictEqual(t.dialogPosts().length, 0);
  assert.deepStrictEqual(t.cancelled(), []);
});

test("mirror is capped (3 per 30 s)", async () => {
  const t = await boot();
  for (let i = 0; i < 5; i++) await t.created(fresh({ id: 20 + i, url: "https://files.example.org/f" + i + ".bin", finalUrl: "https://files.example.org/f" + i + ".bin" }));
  assert.strictEqual(t.dialogPosts().length, 3);
});

test("with the gesture requirement off, everything fresh is intercepted", async () => {
  const t = await boot({ needsGesture: false });
  await t.created(fresh());
  assert.deepStrictEqual(t.cancelled(), [7]);
});

test("blob:, extension-made and localhost downloads are never touched", async () => {
  const t = await boot();
  t.gesture();
  await t.created(fresh({ url: "blob:https://example.org/abc" }));
  await t.created(fresh({ id: 8, byExtensionId: "someoneelse" }));
  await t.created(fresh({ id: 9, url: "http://127.0.0.1:5757/x.bin" }));
  assert.deepStrictEqual(t.cancelled(), []);
  assert.strictEqual(t.dialogPosts().length, 0);
});

test("ignore list: hosts, extensions and bare words", async () => {
  const t = await boot({ ignore: "example.com .exe iso com" });
  const run = (item) => vm.runInContext("isIgnoredDownload(" + JSON.stringify(item) + ")", t.ctx);
  await t.created(fresh());                     // lets VGCFG pick up the ignore text
  assert.strictEqual(run({ url: "https://cdn.example.com/f.bin" }), true, "subdomain of a listed host");
  assert.strictEqual(run({ url: "https://notexample.com/f.bin" }), false, "look-alike host");
  assert.strictEqual(run({ url: "https://a.test/setup.exe" }), true, ".exe");
  assert.strictEqual(run({ url: "https://exe.example.org/f.bin" }), false, "host that merely contains exe");
  assert.strictEqual(run({ url: "https://a.test/disc.iso" }), true, "bare word that is an extension");
  assert.strictEqual(run({ url: "https://iso.test/f.bin" }), false, "bare extension word is not a host");
  assert.strictEqual(run({ url: "https://files.example.org/f.bin" }), false, "bare 'com' must not match .com/.org hosts");
  assert.strictEqual(run({ url: "https://shop.example.net/f.com" }), false, "'com' is not an extension either");
});

(async () => {
  let failed = 0;
  for (const [name, fn] of tests) {
    try { await fn(); console.log("ok   " + name); }
    catch (e) { failed++; console.log("FAIL " + name + "\n       " + String(e && e.message || e).split("\n")[0]); }
  }
  console.log(failed ? `\n${failed} of ${tests.length} failed` : `\nall ${tests.length} passed`);
  process.exitCode = failed ? 1 : 0;
})();
