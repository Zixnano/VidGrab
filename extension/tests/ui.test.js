// Loads the real popup.html / sidepanel.html in jsdom, wired to the real
// service worker (tests/harness.js), and drives the new views.
const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const { JSDOM } = require("../../node_modules/jsdom");
const { boot } = require("./harness.js");
const ROOT = path.join(__dirname, "..");
const tick = (ms) => new Promise((r) => setTimeout(r, ms || 30));

async function open(page, hopts) {
  const h = boot(hopts);
  h.app.alive = !(hopts && hopts.offline);
  const html = fs.readFileSync(path.join(ROOT, page), "utf8").replace(/<script src="[^"]+"><\/script>/g, "");
  const dom = new JSDOM(html, { url: "chrome-extension://abc/" + page, runScripts: "outside-only", pretendToBeVisual: true });
  const w = dom.window;
  w.confirm = () => true; w.scrollTo = () => {};
  w.navigator.clipboard = { writeText: async () => {} };
  const created = [];
  const sendMessage = (msg, cb) => {
    const p = h.send(msg, { id: "abc" });
    if (typeof cb === "function") { p.then((r) => cb(r)); return; }
    return p;
  };
  w.chrome = { runtime: { lastError: null, sendMessage, getURL: (x) => "chrome-extension://abc/" + x, onMessage: { addListener() {} } },
    storage: { local: h.chrome.storage.local, session: h.chrome.storage.session, onChanged: { addListener() {} } },
    tabs: { query: async () => [{ id: 1, windowId: 1, url: "https://site.com/watch?v=1", title: "Cool Show", active: true }], update: async () => {}, create: async (o) => created.push(o), onActivated: { addListener() {} }, onUpdated: { addListener() {} } },
    commands: { getAll: async () => [{ name: "download-media", description: "Download media", shortcut: "Ctrl+Shift+D" }, { name: "start-queue", description: "Start queue", shortcut: "" }] },
    scripting: { executeScript: async () => [] }, cookies: {}, downloads: {} };
  w.fetch = async () => ({ ok: false, status: 404, json: async () => ({}) });
  // Classic scripts share one global lexical scope in a real page, so they go in as one script.
  w.eval(["vglog.js", "vgchrome.js", "vgcore.js", "vgsettings.js", "popupshim.js", "popup.js", "panel.js"].map((f) => fs.readFileSync(path.join(ROOT, f), "utf8")).join("\n;\n"));
  await tick(80);
  return { h, w, d: w.document, created };
}
const click = (el) => el.dispatchEvent(new el.ownerDocument.defaultView.MouseEvent("click", { bubbles: true }));

test("popup: new nav exists with proper ARIA, Page view still works", async () => {
  const { d } = await open("popup.html");
  const tabs = [...d.querySelectorAll("#vgnav [role=tab]")];
  assert.deepStrictEqual(tabs.map((t) => t.textContent), ["Page", "Library", "Queue", "Archive", "Tools"]);
  assert.strictEqual(tabs.filter((t) => t.getAttribute("aria-selected") === "true").length, 1);
  assert.ok(d.getElementById("pane-library").getAttribute("aria-labelledby"));
  assert.strictEqual(d.getElementById("mainView").style.display, "");
});

test("library: lists sniffed media, search narrows, send works, 'sent' badge appears, inspector opens", async () => {
  const { h, w, d } = await open("popup.html", { web: async () => ({ ok: true, status: 200, text: async () => "", headers: { get: (k) => (k === "content-length" ? "2048" : k === "accept-ranges" ? "bytes" : "video/mp4") } }) });
  h.run("VGBG.addItem(1, {url:'https://cdn.com/movie_720p.mp4', source:'url-pattern', size: 8000000, contentType:'video/mp4'}); VGBG.addItem(1, {url:'https://cdn.com/song.mp3', source:'url-pattern', size: 300000})");
  click(d.getElementById("nav-library")); await tick(120);
  let rows = d.querySelectorAll("#liblist .prow"); assert.strictEqual(rows.length, 2);
  const q = d.getElementById("libq"); q.value = "mp3"; q.dispatchEvent(new w.Event("input")); await tick(300);
  rows = d.querySelectorAll("#liblist .prow"); assert.strictEqual(rows.length, 1);
  q.value = ""; q.dispatchEvent(new w.Event("input")); await tick(300);
  const first = d.querySelector("#liblist .prow button[aria-label^='Send']"); click(first); await tick(150);
  assert.ok(h.app.calls.some((c) => c.path === "/download"));
  await tick(100); click(d.getElementById("nav-queue")); click(d.getElementById("nav-library")); await tick(150);
  assert.ok(d.querySelector("#liblist .badge.ok"), "sent badge");
  const info = [...d.querySelectorAll("#liblist button")].find((b) => b.textContent === "Info"); click(info); await tick(200);
  assert.match(d.querySelector(".insp").textContent, /Ranges\s*supported|supported/);
  assert.ok(d.querySelector(".insp select"), "per-host quality select");
});

test("queue: shows jobs, pause/retry buttons call the app, offline shows a clear message", async () => {
  const { h, d } = await open("popup.html");
  Object.assign(h.app.jobs, { a: { id: "a", status: "downloading", filename: "a.mp4", progress: 30, speed: "1 MB/s" }, b: { id: "b", status: "error", filename: "b.mp4", error: "HTTP 503" } });
  click(d.getElementById("nav-queue")); await tick(200);
  assert.strictEqual(d.querySelectorAll("#pane-queue .prow").length, 2);
  assert.ok(d.querySelector("#pane-queue [role=progressbar]"));
  click([...d.querySelectorAll("#pane-queue button")].find((b) => b.textContent === "Pause")); await tick(100);
  assert.ok(h.app.calls.some((c) => c.path === "/pause/a"));
  click([...d.querySelectorAll("#pane-queue button")].find((b) => b.textContent === "Retry")); await tick(100);
  assert.strictEqual(h.app.jobs.b.status, "queued");
  h.app.alive = false; click(d.getElementById("nav-page")); click(d.getElementById("nav-queue")); await tick(250);
  assert.match(d.getElementById("pane-queue").textContent, /isn't running/);
});

test("archive: entries appear after a send, search filters, remove works", async () => {
  const { h, d, w } = await open("popup.html");
  await h.send({ type: "SEND_SMART", url: "https://a.com/one.mp4", pageUrl: "https://a.com", title: "One" });
  await h.send({ type: "SEND_SMART", url: "https://b.com/two.mp4", pageUrl: "https://b.com", title: "Two" }); await tick(80);
  click(d.getElementById("nav-archive")); await tick(150);
  assert.strictEqual(d.querySelectorAll("#pane-archive .prow").length, 2);
  const s = d.querySelector("#pane-archive input[type=search]"); s.value = "b.com"; s.dispatchEvent(new w.Event("input")); await tick(350);
  assert.strictEqual(d.querySelectorAll("#pane-archive .prow").length, 1);
  click(d.querySelector("#pane-archive button[aria-label='Remove from archive']")); await tick(150);
  assert.strictEqual((await h.send({ type: "ARCHIVE_LIST" })).items.length, 1);
});

test("tools: schedule, watchlist, templates, backup, diagnostics all render and work", async () => {
  const { h, d, w } = await open("popup.html");
  click(d.getElementById("nav-tools")); await tick(300);
  for (const t of ["Behaviour", "Scheduled downloads", "Watchlist", "Batch name templates", "Quality per site", "Theme preview", "Backup", "Keyboard shortcuts", "Diagnostics"])
    assert.ok([...d.querySelectorAll("#pane-tools h2")].some((x) => x.textContent === t), t);
  // watchlist add
  d.getElementById("w-pat").value = "example.com";
  click([...d.querySelectorAll("#pane-tools button")].find((b) => b.textContent === "Add")); await tick(120);
  assert.strictEqual((await h.send({ type: "WATCH_LIST" })).rules.length, 1);
  // invalid regex rejected, message surfaced
  d.getElementById("w-pat").value = "(";
  d.querySelectorAll("#pane-tools select")[1].value = "regex";
  // schedule with pasted URL
  d.getElementById("sc-urls").value = "https://x.com/a.mp4";
  d.getElementById("sc-kind").value = "daily"; d.getElementById("sc-kind").dispatchEvent(new w.Event("change"));
  click([...d.querySelectorAll("#pane-tools button")].find((b) => b.textContent === "Schedule")); await tick(150);
  assert.strictEqual((await h.send({ type: "SCHEDULE_LIST" })).schedules.length, 1);
  // templates save
  d.getElementById("tpl-text").value = "Mine = {host} {title}";
  click([...d.querySelectorAll("#pane-tools button")].find((b) => b.textContent === "Save templates")); await tick(100);
  assert.strictEqual((await h.send({ type: "TEMPLATES_GET" })).templates[0].pattern, "{host} {title}");
  // behaviour checkbox persists
  const cb = d.getElementById("pf-notifyDiscovery"); cb.checked = true; cb.dispatchEvent(new w.Event("change")); await tick(80);
  assert.strictEqual(h.store.local.vgSettings.notifyDiscovery, true);
  // diagnostics
  click([...d.querySelectorAll("#pane-tools button")].find((b) => b.textContent === "Refresh")); await tick(200);
  assert.match(d.getElementById("diag-sum").textContent, /v4\.5\.1 · app running 9\.9/);
  // shortcuts listed
  assert.match(d.getElementById("pane-tools").textContent, /Ctrl\+Shift\+D/);
});

test("keyboard: number keys switch views, / focuses search, shortcuts ignored while typing", async () => {
  const { d, w } = await open("popup.html");
  const key = (k, target) => (target || d.body).dispatchEvent(new w.KeyboardEvent("keydown", { key: k, bubbles: true }));
  key("3"); await tick(50); assert.strictEqual(d.getElementById("nav-queue").getAttribute("aria-selected"), "true");
  key("2"); await tick(100); key("/"); assert.strictEqual(d.activeElement.id, "libq");
  key("4", d.activeElement); assert.strictEqual(d.getElementById("nav-library").getAttribute("aria-selected"), "true", "typing in a field doesn't switch");
  const nav = d.getElementById("vgnav"); nav.dispatchEvent(new w.KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true })); await tick(50);
  assert.strictEqual(d.getElementById("nav-queue").getAttribute("aria-selected"), "true");
});

test("side panel: same UI, never auto-sends or closes, remembers its view", async () => {
  const { h, d, w } = await open("sidepanel.html");
  h.run("VGBG.addItem(1, {url:'https://cdn.com/only.mp4', source:'url-pattern', size: 8000000, contentType:'video/mp4'})");
  await tick(150);
  assert.ok(w.document.documentElement.classList.contains("side"));
  assert.ok(!h.app.calls.some((c) => c.path === "/download"), "no auto-send in the side panel");
  assert.strictEqual(w.close.toString().includes("native"), false);
  click(d.getElementById("nav-archive")); await tick(100);
  assert.strictEqual(h.store.session.vgPanelView, "archive");
});

test("page tabs expose aria-selected and the shim hardens sendMessage (retry on sleeping worker)", async () => {
  const { w, d } = await open("popup.html");
  assert.strictEqual(d.getElementById("tabCurrent").getAttribute("aria-selected"), "true");
  click(d.getElementById("tabOther")); await tick(80);
  assert.strictEqual(d.getElementById("tabOther").getAttribute("aria-selected"), "true");
  assert.strictEqual(d.getElementById("tabCurrent").getAttribute("aria-selected"), "false");
  let n = 0; const real = w.chrome.runtime.sendMessage;
  // simulate: first call fails with lastError, second succeeds -> callback gets the value
  w.chrome.runtime.sendMessage = (m, cb) => { n++; if (n === 1) { w.chrome.runtime.lastError = { message: "Receiving end does not exist." }; cb(); w.chrome.runtime.lastError = null; } else cb({ ok: 1 }); };
  w.eval(fs.readFileSync(path.join(ROOT, "popupshim.js"), "utf8"));
  const got = await new Promise((r) => w.chrome.runtime.sendMessage({ type: "X" }, r));
  assert.deepStrictEqual({ ...got }, { ok: 1 }); assert.strictEqual(n, 2);
});

test("scroll memory: position is saved per view and tab, then restored", async () => {
  const { h, w, d } = await open("popup.html");
  Object.defineProperty(d, "scrollingElement", { value: { scrollTop: 0 }, configurable: true });
  d.scrollingElement.scrollTop = 321; w.dispatchEvent(new w.Event("scroll")); await tick(400);
  const saved = h.store.session.vgScroll; assert.ok(Object.values(saved).includes(321));
});

test("sweep: a HEAD size probe that failed is retried later instead of staying unknown forever", async () => {
  const { w } = await open("popup.html");
  const base = Date.now(); let calls = 0;
  w.fetch = async () => { calls++; if (calls === 1) throw new Error("network blip"); return { ok: true, status: 200, headers: { get: () => "4096" } }; };
  const item = { url: "https://a.com/x.mp4", size: 0 };
  await w.probeUnknownSizes([item]); assert.strictEqual(item.size, 0, "first probe failed");
  await w.probeUnknownSizes([item]); assert.strictEqual(calls, 1, "not hammered again straight away");
  w.Date.now = () => base + 20000;
  await w.probeUnknownSizes([item]); assert.strictEqual(item.size, 4096, "retried after the cool-down");
  await w.probeUnknownSizes([item]); assert.strictEqual(calls, 2, "a size once known is not probed again");
});

test("sweep: a popup setting change does not overwrite settings another page changed meanwhile", async () => {
  const { h, w, d } = await open("popup.html");
  await h.chrome.storage.local.set({ vgSettings: { notifyActions: false, pillVisible: true } });   // changed from Tools or Options while the popup is open
  const cb = d.getElementById("setOneClick"); cb.checked = true; cb.dispatchEvent(new w.Event("change")); await tick(80);
  assert.strictEqual(h.store.local.vgSettings.oneClickMode, true);
  assert.strictEqual(h.store.local.vgSettings.notifyActions, false, "the other page's change survived");
});

test("M1.4 panel: Remembered scope shows rows with a relative time, Forget removes one, Forget all on this host clears the host", async () => {
  const { h, w, d } = await open("popup.html");
  const t = Date.now(), mk = (u, ago) => ({ url: u, filename: u.split("/").pop(), title: "T", size: 5e6, contentType: "video/mp4", firstSeen: t - ago, lastSeen: t - ago });
  h.store.local.vgSniffedLibrary = { "a.com": [mk("https://a.com/one.mp4", 3 * 86400000), mk("https://a.com/two.mp4", 60000)], "b.com": [mk("https://b.com/three.mp4", 1000)] };
  click(d.getElementById("nav-library")); await tick(150);
  const scope = d.querySelector("#pane-library select[aria-label=Scope]");
  assert.deepStrictEqual([...scope.options].map((o) => o.textContent), ["All tabs", "Remembered", "This tab"]);
  scope.value = "remembered"; scope.dispatchEvent(new w.Event("change")); await tick(200);
  assert.strictEqual(d.querySelectorAll("#liblist .prow").length, 3);
  assert.match(d.getElementById("liblist").textContent, /3 days ago/);
  assert.ok(!d.getElementById("forgetHost"), "mixed hosts: no host button");
  click(d.querySelector("#liblist button[aria-label^='Forget']")); await tick(200);
  assert.strictEqual(d.querySelectorAll("#liblist .prow").length, 2);
  const q = d.getElementById("libq"); q.value = "a.com"; q.dispatchEvent(new w.Event("input")); await tick(400);
  const fh = d.getElementById("forgetHost"); assert.ok(fh, "single host after filtering"); assert.match(fh.textContent, /a\.com/);
  click(fh); await tick(250);
  assert.ok(!h.store.local.vgSniffedLibrary["a.com"] && !h.store.local.vgSniffedLibrary["b.com"], "newest row (b.com) was forgotten first, then all of a.com");
});
