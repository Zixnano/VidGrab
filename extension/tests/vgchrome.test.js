const test = require("node:test");
const assert = require("node:assert");

function fresh(chromeObj) {
  delete require.cache[require.resolve("../vglog.js")];
  delete require.cache[require.resolve("../vgchrome.js")];
  globalThis.chrome = chromeObj;
  globalThis.VGLOG = require("../vglog.js");
  return require("../vgchrome.js");
}

test("log: ring buffer caps, redacts nothing but truncates, never throws", () => {
  fresh({});
  const L = globalThis.VGLOG; L.setLevel("error");
  for (let i = 0; i < 400; i++) L.info("t", "m" + i);
  assert.strictEqual(L.entries().length, 300);
  L.warn("t", "x".repeat(2000), { big: "y".repeat(5000) });
  const last = L.entries().pop();
  assert.ok(last.m.length <= 400);
  assert.ok(JSON.stringify(last.d).length < 800);
  assert.ok(L.dump({ level: "warn" }).includes("WARN"));
  const circ = {}; circ.self = circ;
  assert.doesNotThrow(() => L.error("t", "circ", circ));
});

test("log: guard converts a throw into {ok:false}", async () => {
  fresh({});
  const f = globalThis.VGLOG.guard("t", async () => { throw new Error("boom"); });
  const r = await f();
  assert.strictEqual(r.ok, false); assert.strictEqual(r.error, "boom");
});

test("log: persists to and hydrates from session storage", async () => {
  const mem = {};
  fresh({ storage: { session: { set: async (o) => Object.assign(mem, o), get: async (k) => ({ [k]: mem[k] }), remove: async () => {} } } });
  const L = globalThis.VGLOG; L.setLevel("error");
  L.warn("a", "first");
  await L.flush();
  assert.strictEqual(mem.vgLog.length, 1);
  L._reset();
  await L.hydrate();
  assert.strictEqual(L.entries()[0].m, "first");
});

test("caps: detects missing sidePanel and present notifications", () => {
  const C = fresh({ notifications: { create() {} }, storage: { session: { set() {} } } });
  const c = C.caps(true);
  assert.strictEqual(c.sidePanel, false); assert.strictEqual(c.notifications, true);
  assert.strictEqual(c.storageSession, true); assert.strictEqual(c.alarms, false);
  const C2 = fresh({ sidePanel: { setOptions() {}, open() {} } });
  assert.strictEqual(C2.caps(true).sidePanel, true);
});

test("call: promise style, callback style, lastError and missing API all resolve", async () => {
  const chromeObj = { runtime: { lastError: null }, tabs: {
    query: async () => [{ id: 1 }],
    get: (id, cb) => cb({ id }),
    bad: (cb) => { chromeObj.runtime.lastError = { message: "No tab" }; cb(); chromeObj.runtime.lastError = null; },
    boom: () => { throw new Error("sync throw"); },
  } };
  const C = fresh(chromeObj);
  assert.deepStrictEqual((await C.call("tabs.query", {})).value, [{ id: 1 }]);
  assert.deepStrictEqual((await C.call("tabs.get", 7)).value, { id: 7 });
  const b = await C.call("tabs.bad"); assert.strictEqual(b.ok, false); assert.strictEqual(b.error, "No tab");
  assert.strictEqual((await C.call("tabs.boom")).ok, false);
  assert.match((await C.call("nope.nothing")).error, /unsupported/);
});

test("send: retries once when the worker is asleep, then succeeds", async () => {
  let n = 0;
  const chromeObj = { runtime: { lastError: null, sendMessage: (m, cb) => {
    n++;
    if (n === 1) { chromeObj.runtime.lastError = { message: "Could not establish connection. Receiving end does not exist." }; cb(); chromeObj.runtime.lastError = null; }
    else cb({ ok: true, pong: 1 });
  } } };
  const C = fresh(chromeObj);
  const r = await C.send({ type: "X" }, { timeout: 2000 });
  assert.strictEqual(r.ok, true); assert.strictEqual(n, 2);
});
test("send: permanent failure returns {ok:false} and does not throw; empty reply is an error", async () => {
  const chromeObj = { runtime: { lastError: null, sendMessage: (m, cb) => { chromeObj.runtime.lastError = { message: "Something else" }; cb(); chromeObj.runtime.lastError = null; } } };
  const C = fresh(chromeObj);
  const r = await C.send({ type: "X" }, { timeout: 500 });
  assert.strictEqual(r.ok, false); assert.strictEqual(r.error, "Something else");
  const c2 = { runtime: { lastError: null, sendMessage: (m, cb) => cb(undefined) } };
  const C2 = fresh(c2);
  assert.strictEqual((await C2.send({ type: "Y" }, { timeout: 500 })).ok, false);
});
test("send: times out instead of hanging", async () => {
  const C = fresh({ runtime: { lastError: null, sendMessage: () => {} } });
  const r = await C.send({ type: "Z" }, { timeout: 50, retries: 0 });
  assert.strictEqual(r.ok, false); assert.strictEqual(r.error, "timeout");
});
test("storageGet falls back to {} on failure", async () => {
  const C = fresh({ storage: { local: { get: async () => { throw new Error("quota"); } } } });
  assert.deepStrictEqual(await C.storageGet("k"), {});
});

test("sweep: call() resolves for an API that returns a plain value and never uses the callback", async () => {
  const X = fresh({ runtime: { getURL: (p) => "chrome-extension://abc/" + p } });
  const r = await Promise.race([X.call("runtime.getURL", "a.png"), new Promise((res) => setTimeout(() => res("HUNG"), 300))]);
  assert.deepStrictEqual(r, { ok: true, value: "chrome-extension://abc/a.png" });
});

test("sweep: call() turns a synchronous throw into {ok:false}, and a null answer from send() is an error, not success", async () => {
  const X = fresh({ runtime: { lastError: null, thrower: () => { throw new Error("sync boom"); }, sendMessage: (m, cb) => cb(null) } });
  const r = await X.call("runtime.thrower");
  assert.strictEqual(r.ok, false); assert.match(r.error, /sync boom/);
  const s = await X.send({ type: "X" }, { retries: 0 });
  assert.strictEqual(s.ok, false); assert.match(s.error, /No answer/);
});
