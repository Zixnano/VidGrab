// Runs the real mainhook.js inside jsdom (it only needs window, URL, HTMLAnchorElement and
// navigator.userActivation) and drives it with the same window messages content.js sends.
const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const { JSDOM } = require("../../node_modules/jsdom");
const SRC = fs.readFileSync(path.join(__dirname, "..", "mainhook.js"), "utf8");
const tick = (ms) => new Promise((r) => setTimeout(r, ms || 30));
const CONTENT = "CONTENT-SECRET-0123456789";

function hook() {
  const dom = new JSDOM("<!doctype html><body></body>", { url: "https://claude.ai/", runScripts: "outside-only", pretendToBeVisual: true });
  const w = dom.window; let n = 0;
  const natives = [], posted = [];
  w.URL.createObjectURL = () => "blob:t/" + (++n);
  w.URL.revokeObjectURL = () => {};
  Object.defineProperty(w.navigator, "userActivation", { value: { isActive: true } });
  w.HTMLAnchorElement.prototype.click = function () { natives.push({ download: this.download, href: this.href }); };   // captured as the "original" click
  w.postMessage = (m) => { posted.push(m); };
  w.eval(SRC);
  const msg = (data) => w.dispatchEvent(new w.MessageEvent("message", { data, source: w }));
  const arm = (secret) => msg({ type: "vg-armed", source: secret || CONTENT, armed: true });
  const clickBlob = (name) => {
    const a = w.document.createElement("a"); a.setAttribute("download", name || "x.txt");
    a.href = w.URL.createObjectURL(new w.Blob(["hello"])); w.document.body.appendChild(a); a.click(); return a;
  };
  return { w, msg, arm, clickBlob, natives, posted };
}

test("B7: the secret is only ever adopted from a vg-armed message; other message types cannot plant one", async () => {
  const H = hook();
  H.msg({ type: "vg-blob-ack", source: "PAGE-SECRET-0123456789", id: "x" });
  H.msg({ type: "vg-blob-download", source: "PAGE-SECRET-0123456789", id: "y" });
  H.msg({ type: "vg-blob-replay", source: "PAGE-SECRET-0123456789", id: "z" });
  H.arm();   // content.js speaks first as far as the hook is concerned
  H.clickBlob();
  assert.strictEqual(H.posted.length, 1, "the click was taken over");
  assert.strictEqual(H.posted[0].type, "vg-blob-download");
  assert.strictEqual(H.posted[0].source, CONTENT, "hook uses content.js's secret, not the one the page tried to plant");
  assert.strictEqual(H.natives.length, 0);
});

test("B7: a too-short secret in vg-armed is refused and the hook stays unarmed", async () => {
  const H = hook();
  H.msg({ type: "vg-armed", source: "short", armed: true });
  H.clickBlob();
  assert.strictEqual(H.posted.length, 0); assert.strictEqual(H.natives.length, 1, "native click went through untouched");
});

test("sweep: an ack that arrives after the watchdog already replayed natively is answered with vg-blob-cancel (no duplicate upload)", async () => {
  const H = hook(); H.arm(); H.clickBlob();
  const id = H.posted[0].id;
  await tick(1700);   // ACK_MS is 1500: the page-side watchdog replays the click natively
  assert.strictEqual(H.natives.length, 1, "replayed once");
  H.msg({ type: "vg-blob-ack", source: CONTENT, id });
  const cancel = H.posted.find((m) => m.type === "vg-blob-cancel");
  assert.ok(cancel && cancel.id === id && cancel.source === CONTENT);
  H.msg({ type: "vg-blob-replay", source: CONTENT, id });
  assert.strictEqual(H.natives.length, 1, "a late replay request does not save a second copy");
});

test("sweep: after an ack the pending entry is not held forever (long safety timer, no second replay)", async () => {
  const H = hook(); const timers = [];
  const orig = H.w.setTimeout; H.w.setTimeout = (f, ms) => { timers.push({ f, ms }); return orig(() => {}, 1e9); };
  H.arm(); H.clickBlob(); const id = H.posted[0].id;
  H.msg({ type: "vg-blob-ack", source: CONTENT, id });
  const safety = timers.filter((t) => t.ms >= 60000);
  assert.ok(safety.length >= 1, "a long safety timer was set after the ack");
  safety[safety.length - 1].f();
  H.msg({ type: "vg-blob-replay", source: CONTENT, id });
  assert.strictEqual(H.natives.length, 0, "entry was dropped: nothing to replay");
});
