const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const { JSDOM } = require("../../node_modules/jsdom");
const { boot } = require("./harness.js");
const ROOT = path.join(__dirname, "..");
const tick = (ms) => new Promise((r) => setTimeout(r, ms || 30));

async function page(hopts, settings, url) {
  const h = boot(hopts);
  const dom = new JSDOM("<!doctype html><title>Great Talk</title><body><video id=v></video></body>", { url: url || "https://site.com/watch/1", runScripts: "outside-only", pretendToBeVisual: true });
  const w = dom.window;
  const store = h.chrome.storage.local;
  if (settings) await store.set({ vgSettings: settings });
  w.chrome = { runtime: { lastError: null, onMessage: { addListener() {} }, sendMessage: (m, cb) => { h.send(m, { tab: { id: 1 } }).then((r) => cb && cb(r)); }, getURL: (x) => x },
    storage: { local: { get: (k) => store.get(k), set: (o) => store.set(o) }, onChanged: { addListener() {} } } };
  w.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} };
  w.IntersectionObserver = class { observe() {} unobserve() {} disconnect() {} };
  w.requestIdleCallback = (f) => setTimeout(f, 0); w.matchMedia = () => ({ matches: false, addListener() {}, addEventListener() {} });
  w.innerHeight = 800; w.innerWidth = 1200;
  const v = w.document.getElementById("v");
  Object.defineProperty(v, "currentSrc", { value: "https://cdn.com/a/AbC123xYz987QwErTy45.mp4", configurable: true });
  Object.defineProperty(v, "videoWidth", { value: 1920 }); Object.defineProperty(v, "videoHeight", { value: 1080 });
  v.getBoundingClientRect = () => ({ left: 10, top: 10, right: 650, bottom: 370, width: 640, height: 360, x: 10, y: 10 });
  w.HTMLElement.prototype.getBoundingClientRect = function () { return this.id === "v" ? v.getBoundingClientRect() : { left: 0, top: 0, right: 100, bottom: 30, width: 100, height: 30, x: 0, y: 0 }; };
  // Recording support: a fake MediaRecorder, a capturable video, and a recorder for anchor downloads.
  w.__downloads = []; w.__recorders = [];
  w.URL.createObjectURL = () => "blob:test/" + Math.random().toString(36).slice(2);
  w.URL.revokeObjectURL = () => {};
  w.HTMLAnchorElement.prototype.click = function () { w.__downloads.push({ download: this.download, href: this.href }); };
  w.MediaRecorder = class { constructor(stream, o) { this.state = "inactive"; this.stream = stream; w.__recorders.push(this); }
    start() { this.state = "recording"; } pause() { this.state = "paused"; } resume() { this.state = "recording"; }
    stop() { if (this.state === "inactive") return; this.state = "inactive"; if (this.ondataavailable) this.ondataavailable({ data: new w.Blob(["abc"]) }); setTimeout(() => this.onstop && this.onstop(), 0); }
    static isTypeSupported() { return true; } };
  v.captureStream = () => ({ getVideoTracks: () => [{}], getTracks: () => [{ stop() {}, addEventListener() {} }] });
  w.eval(["vglog.js", "vgsettings.js", "vgcore.js", "content.js"].map((f) => fs.readFileSync(path.join(ROOT, f), "utf8")).join("\n;\n"));
  await tick(200);
  return { h, w, d: w.document, v };
}
const click = (el) => el.dispatchEvent(new el.ownerDocument.defaultView.MouseEvent("click", { bubbles: true }));

test("content script loads, makes a pill with accessible names", async () => {
  const { d } = await page();
  const btn = [...d.querySelectorAll("button")].find((b) => /Download this video/.test(b.getAttribute("aria-label") || ""));
  assert.ok(btn, "pill button with aria-label");
  assert.ok([...d.querySelectorAll("button")].some((b) => b.getAttribute("aria-haspopup") === "menu"));
});

test("smart one-click: pill click sends SEND_SMART with a title-based name (no dialog)", async () => {
  const { h, d } = await page({}, { oneClickMode: true, smartOneClick: true });
  const btn = [...d.querySelectorAll("button")].find((b) => /Download this video/.test(b.getAttribute("aria-label") || ""));
  click(btn); await tick(300);
  const call = h.app.calls.find((c) => c.path === "/download");
  assert.ok(call, "download reached the app");
  assert.strictEqual(call.body.filename, "Great Talk.mp4");
  assert.ok(!h.app.calls.some((c) => c.path === "/show-add-dialog"));
});

test("smart one-click off: legacy path is unchanged (RELAY_DOWNLOAD with the original-style name)", async () => {
  const { h, d } = await page({}, { oneClickMode: true, smartOneClick: false });
  const btn = [...d.querySelectorAll("button")].find((b) => /Download this video/.test(b.getAttribute("aria-label") || ""));
  click(btn); await tick(300);
  assert.ok(h.app.calls.some((c) => c.path === "/download"));
});

test("sent twice: second click asks before resending", async () => {
  const { h, d } = await page({}, { oneClickMode: true, smartOneClick: true });
  const btn = [...d.querySelectorAll("button")].find((b) => /Download this video/.test(b.getAttribute("aria-label") || ""));
  click(btn); await tick(300); await tick(200);
  click(btn); await tick(300);
  assert.strictEqual(h.app.calls.filter((c) => c.path === "/download").length, 1, "not re-sent silently");
  assert.match(d.documentElement.textContent, /Sent before/);
  click(btn); await tick(300);
  assert.strictEqual(h.app.calls.filter((c) => c.path === "/download").length, 2, "resent after confirmation click");
});

// ---- v4.5.1 ----------------------------------------------------------------
const byText = (d, re) => [...d.querySelectorAll("div,button,span")].find((e) => e.children.length === 0 && re.test(e.textContent));
const openMenu = async (d) => { click(d.querySelector("button[aria-haspopup=menu]")); await tick(60); return d.querySelector("[role=menu]"); };

test("B4: the Quality row cycles and shows the new value at once; the stream-info header is left alone", async () => {
  const { d } = await page();
  const menu = await openMenu(d);
  const header = menu.firstChild.textContent;
  assert.doesNotMatch(header, /Quality/);
  click(byText(d, /^Quality: High/)); await tick(20);
  assert.match(byText(d, /^Quality:/).textContent, /^Quality: Source/);
  assert.strictEqual(menu.firstChild.textContent, header, "header untouched");
  click(byText(d, /^Quality:/)); await tick(20);
  assert.match(byText(d, /^Quality:/).textContent, /^Quality: Low/);
});

test("B5: on an X feed with no tweet link around the video the pill says 'Open a tweet first' and sends nothing", async () => {
  const { h, d } = await page({}, {}, "https://x.com/home");
  await openMenu(d);
  click(byText(d, /^Send page URL to yt-dlp/)); await tick(150);
  assert.strictEqual(d.querySelector(".vg-pill-label").textContent, "Open a tweet first");
  assert.ok(!h.app.calls.some((c) => c.path === "/download"), "nothing reached the app");
});

test("B5 (scope check): a feed video inside a tweet still sends that tweet's own permalink", async () => {
  const { h, d, v } = await page({}, {}, "https://x.com/home");
  const art = d.createElement("article"); const a = d.createElement("a"); a.setAttribute("href", "/someone/status/999"); a.appendChild(d.createElement("time"));
  d.body.appendChild(art); art.appendChild(a); art.appendChild(v);
  await openMenu(d);
  click(byText(d, /^Send page URL to yt-dlp/)); await tick(200);
  const call = h.app.calls.find((c) => c.path === "/download");
  assert.ok(call, "permalink was sent");
  assert.strictEqual(call.body.url, "https://x.com/someone/status/999");
});

const XTWEET = "https://x.com/user/status/123";
test("B8: one-click on X does not fall back to the sniffed file when the tweet is missing or private", async () => {
  for (const msg of ["ERROR: [twitter] 123: No video could be found in this tweet", "This tweet is protected, log in required"]) {
    const { h, d } = await page({}, { oneClickMode: true, smartOneClick: true }, XTWEET);
    h.app.failSend = msg;
    click(d.querySelector("button[aria-label^='Download this video']")); await tick(400);
    assert.strictEqual(h.app.calls.filter((c) => c.path === "/download").length, 1, "only the tweet attempt: " + msg);
  }
});

test("B8: a network-type failure still falls back to the sniffed file", async () => {
  const { h, d } = await page({}, { oneClickMode: true, smartOneClick: true }, XTWEET);
  h.app.failSend = "HTTP 503 service unavailable";
  click(d.querySelector("button[aria-label^='Download this video']")); await tick(500);
  assert.strictEqual(h.app.calls.filter((c) => c.path === "/download").length, 2, "tweet attempt, then the direct file");
});

test("B6: a recording is named from the title at START, not the title at stop", async () => {
  const { w, d } = await page();
  await openMenu(d);
  click(byText(d, /^Record element/)); await tick(100);
  assert.strictEqual(w.__recorders.length, 1, "recording started");
  d.title = "Home / X";   // SPA navigation moved the title on while recording
  w.__recorders[0].stop(); await tick(300);
  assert.strictEqual(w.__downloads.length, 1, "saved by the browser (app has no upload route in the fake)");
  assert.match(w.__downloads[0].download, /^Great Talk element High \d{8} \d{6}\.webm$/);
});

test("sweep: two tabs on the same path with different queries do not borrow each other's sniffed media", async () => {
  const tabs = { 1: { id: 1, windowId: 1, url: "https://site.com/watch/1?v=1", title: "One" }, 2: { id: 2, windowId: 1, url: "https://site.com/watch/1?v=2", title: "Two" } };
  const { h, w, d } = await page({ tabs }, {}, "https://site.com/watch/1?v=2");
  Object.defineProperty(w.navigator, "clipboard", { value: { writeText: async (t) => { w.__copied = t; } }, configurable: true });
  h.run("VGBG.addItem(1, {url:'https://cdn.com/one/master.m3u8', source:'url-pattern', size: 900000}); VGBG.addItem(2, {url:'https://cdn.com/two/master.m3u8', source:'url-pattern', size: 800000});");
  await openMenu(d); await tick(150);
  click(byText(d, /^Copy direct URL/)); await tick(60);
  assert.strictEqual(w.__copied, "https://cdn.com/two/master.m3u8");
});

test("sweep: when the same address is open in two tabs the pill refuses to guess", async () => {
  const tabs = { 1: { id: 1, windowId: 1, url: "https://site.com/watch/1", title: "One" }, 2: { id: 2, windowId: 1, url: "https://site.com/watch/1", title: "Two" } };
  const { h, d } = await page({ tabs }, {}, "https://site.com/watch/1");
  h.run("VGBG.addItem(1, {url:'https://cdn.com/one/master.m3u8', source:'url-pattern', size: 900000}); VGBG.addItem(2, {url:'https://cdn.com/two/master.m3u8', source:'url-pattern', size: 800000});");
  await openMenu(d); await tick(150);
  assert.ok(!byText(d, /^Copy direct URL/), "ambiguous: no direct URL offered");
});

test("sweep: screen-capture failures are told apart: cancelled, blocked by the system, or failed", async () => {
  for (const [name, msg, want] of [["NotAllowedError", "Permission denied", "Capture cancelled"], ["NotAllowedError", "Permission denied by system", "Capture blocked: permission denied"], ["AbortError", "Starting video failed", "Capture failed, try again"]]) {
    const { w, d } = await page();
    w.navigator.mediaDevices = { getDisplayMedia: async () => { const e = new Error(msg); e.name = name; throw e; } };
    await openMenu(d); click(byText(d, /^Record screen/)); await tick(80);
    assert.strictEqual(d.querySelector(".vg-pill-label").textContent, want, name + " " + msg);
  }
});

// ---- M6: auto-save a recording when the page goes away ------------------------
const hide = (w, hidden) => { for (const [k, v] of [["visibilityState", hidden ? "hidden" : "visible"], ["hidden", hidden]]) Object.defineProperty(w.document, k, { value: v, configurable: true }); };
const startRec = async (d) => { await openMenu(d); click(byText(d, /^Record element/)); await tick(100); };

test("M6: pagehide stops the recording and saves it with the browser (no upload attempt)", async () => {
  const { h, w, d } = await page();
  await startRec(d); assert.strictEqual(w.__recorders[0].state, "recording");
  hide(w, true);
  w.dispatchEvent(new w.Event("pagehide")); await tick(200);
  assert.strictEqual(w.__recorders[0].state, "inactive");
  assert.strictEqual(w.__downloads.length, 1, "saveBlob ran");
  assert.ok(!h.app.calls.some((c) => c.path === "/upload-token"), "hidden page: saved first, upload path skipped");
});

test("M6: a hidden tab (visibilitychange) stops and saves the recording when the setting is on", async () => {
  const { w, d } = await page();
  await startRec(d); hide(w, true);
  w.document.dispatchEvent(new w.Event("visibilitychange")); await tick(200);
  assert.strictEqual(w.__downloads.length, 1);
});

test("M6: with the setting off visibilitychange leaves the recording running, but pagehide still saves it", async () => {
  const { w, d } = await page({}, { autoSaveRecordingOnNavigate: false });
  await startRec(d); hide(w, true);
  w.document.dispatchEvent(new w.Event("visibilitychange")); await tick(150);
  assert.strictEqual(w.__recorders[0].state, "recording"); assert.strictEqual(w.__downloads.length, 0);
  w.dispatchEvent(new w.Event("pagehide")); await tick(200);
  assert.strictEqual(w.__downloads.length, 1);
});

test("M6: a normal stop on a visible page still goes through the upload path", async () => {
  const { h, w, d } = await page();
  await startRec(d); w.__recorders[0].stop(); await tick(250);
  assert.ok(h.app.calls.some((c) => c.path === "/upload-token"), "upload nonce requested");
  w.dispatchEvent(new w.Event("pagehide")); await tick(50);
  assert.strictEqual(w.__downloads.length, 1, "saved by the browser once (fake app has no upload route); the leftover listeners did nothing extra");
});
