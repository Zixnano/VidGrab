// Loads the real options.html + options.js in jsdom against a fake chrome.storage whose reads can be held back.
const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const { JSDOM } = require("../../node_modules/jsdom");
const ROOT = path.join(__dirname, "..");
const tick = (ms) => new Promise((r) => setTimeout(r, ms || 30));

function openOptions(initial, o) {
  o = o || {};
  const html = fs.readFileSync(path.join(ROOT, "options.html"), "utf8").replace(/<script src="[^"]+"><\/script>/g, "");
  const dom = new JSDOM(html, { url: "chrome-extension://abc/options.html", runScripts: "outside-only" });
  const w = dom.window; const store = JSON.parse(JSON.stringify(initial || {})); const listeners = [];
  let release; const gate = o.hold ? new Promise((r) => { release = r; }) : Promise.resolve();
  const pick = (k) => { const ks = k == null ? Object.keys(store) : Array.isArray(k) ? k : [k]; const out = {}; for (const x of ks) if (x in store) out[x] = JSON.parse(JSON.stringify(store[x])); return out; };
  const fire = (changes) => listeners.forEach((l) => l(changes, "local"));
  w.chrome = { storage: {
    local: {
      get: async (k) => { if (o.failGet) throw new Error("storage unavailable"); const snap = pick(k); if ([].concat(k).includes("vgSettings")) await gate; return snap; },   // vgSettings reads: snapshot at call time, delivered after the gate (a stale read)
      set: async (obj) => { const ch = {}; for (const k of Object.keys(obj)) { ch[k] = { oldValue: store[k], newValue: JSON.parse(JSON.stringify(obj[k])) }; store[k] = ch[k].newValue; } fire(ch); },
    },
    onChanged: { addListener: (f) => listeners.push(f) } } };
  w.fetch = async () => { throw new Error("app off"); };
  w.eval([fs.readFileSync(path.join(ROOT, "vgsettings.js"), "utf8"), fs.readFileSync(path.join(ROOT, "options.js"), "utf8")].join("\n;\n"));
  return { w, d: w.document, store, fire, release: () => release && release() };
}

test("B9: a settings change that arrives before the initial read resolves is not overwritten by it", async () => {
  const O = openOptions({ vgSettings: { interceptDownloads: false }, interceptDownloads: false }, { hold: true });
  await tick(10);
  // the popup toggle flips the switch while options.js is still waiting for its first read
  O.store.vgSettings = { interceptDownloads: true };
  O.fire({ vgSettings: { oldValue: { interceptDownloads: false }, newValue: { interceptDownloads: true } } });
  O.release(); await tick(60);
  assert.strictEqual(O.d.getElementById("interceptDownloads").checked, true, "newer value kept");
  assert.strictEqual(O.store.vgSettings.interceptDownloads, true, "storage not overwritten with the stale snapshot");
});

test("B9: the legacy interceptDownloads migration write is skipped when a newer change already landed", async () => {
  const O = openOptions({ interceptDownloads: true }, { hold: true });   // no vgSettings yet: migration would write the legacy value
  await tick(10);
  O.store.vgSettings = { interceptDownloads: false };
  O.fire({ vgSettings: { newValue: { interceptDownloads: false } } });
  O.release(); await tick(60);
  assert.strictEqual(O.store.vgSettings.interceptDownloads, false, "migration did not clobber the newer value");
  assert.strictEqual(O.d.getElementById("interceptDownloads").checked, false);
});

test("sweep: when storage reads fail the page still works and blobHookEnabled defaults to ON like the content script", async () => {
  const O = openOptions({}, { failGet: true });
  await tick(60);
  assert.strictEqual(O.d.getElementById("blobHookEnabled").checked, true, "undefined means on, as in content.js");
  O.d.getElementById("token").value = "NEWTOKEN";
  O.d.getElementById("save").dispatchEvent(new O.w.MouseEvent("click", { bubbles: true })); await tick(40);
  assert.strictEqual(O.store.apiToken, "NEWTOKEN", "the Save button was still wired");
});
