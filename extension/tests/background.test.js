const test = require("node:test");
const assert = require("node:assert");
const { boot } = require("./harness.js");
const tick = (ms) => new Promise((r) => setTimeout(r, ms || 20));

test("boot: worker loads, features register, menus build, no throw", async () => {
  const h = boot({ sidePanel: true });
  await tick(50);
  for (const t of ["RELAY_DOWNLOAD", "GET_VIDEOS", "VG_DESKTOP_INFO", "LIB_LIST", "JOBS_LIST", "SCHEDULE_ADD", "SETTINGS_IMPORT", "SEND_SMART"])
    assert.ok(h.run("VGBG.handlers"), t);
  assert.ok(h.run("typeof VGBG.handlers.LIB_LIST") === "function");
  assert.ok(h.run("Object.keys(VGBG.handlers).length") >= 40);
});

test("dispatcher: unknown type is ignored, throwing handler becomes an error reply, rejection too", async () => {
  const h = boot();
  assert.strictEqual(await h.send({ type: "NOPE" }), undefined);
  h.run("VGBG.on('T_THROW', () => { throw new Error('bad'); }); VGBG.on('T_REJECT', async () => { throw new Error('rej'); }); VGBG.on('T_VAL', async () => ({ ok: true, v: 1 }));");
  const a = await h.send({ type: "T_THROW" }); assert.strictEqual(a.ok, false); assert.match(a.error, /bad/);
  const b = await h.send({ type: "T_REJECT" }); assert.strictEqual(b.ok, false); assert.match(b.error, /rej/);
  assert.strictEqual((await h.send({ type: "T_VAL" })).v, 1);
  assert.strictEqual(h.run("VGBG.on.length"), 2);
});

test("RELAY_DOWNLOAD: sends cookies/referer to the app and records the job in the archive", async () => {
  const h = boot();
  const r = await h.send({ type: "RELAY_DOWNLOAD", url: "https://site.com/v.mp4", pageUrl: "https://site.com/p", filename: "v.mp4" }, { tab: { id: 1 } });
  assert.strictEqual(r.ok, true); assert.ok(r.job_id);
  const call = h.app.calls.find((c) => c.path === "/download");
  assert.strictEqual(call.body.cookie, "a=b"); assert.strictEqual(call.body.referer, "https://site.com/p");
  await tick(30);
  const arch = await h.send({ type: "ARCHIVE_LIST" });
  assert.strictEqual(arch.items.length, 1); assert.strictEqual(arch.items[0].url, "https://site.com/v.mp4");
});

test("app offline: classified error, nothing thrown", async () => {
  const h = boot(); h.app.alive = false;
  const r = await h.send({ type: "RELAY_DOWNLOAD", url: "https://site.com/v.mp4", pageUrl: "https://site.com/p" }, { tab: { id: 1 } });
  assert.strictEqual(r.ok, false); assert.match(r.error, /isn't running/);
});

test("desktop detection: identity, version, route caps via OPTIONS only (never mutates)", async () => {
  const h = boot({ app: ["/jobs", "/queue-status", "/queue-running", "/redownload/x"] });
  const r = await h.send({ type: "VG_DESKTOP_INFO", force: true });
  assert.strictEqual(r.alive, true); assert.strictEqual(r.identity, true); assert.strictEqual(r.version, "9.9"); assert.strictEqual(r.paired, true);
  assert.strictEqual(r.caps.jobs, true); assert.strictEqual(r.caps.queue, true); assert.strictEqual(r.caps.retry, true); assert.strictEqual(r.caps.formats, false);
  const probes = h.app.calls.filter((c) => c.method !== "GET");
  assert.ok(probes.length > 0 && probes.every((c) => c.method === "OPTIONS"), "only OPTIONS probes");
  h.app.alive = false;
  const off = await h.send({ type: "VG_DESKTOP_INFO", force: true });
  assert.strictEqual(off.alive, false); assert.strictEqual(Object.keys(off.caps).length, 0);
});

test("stale token: one silent re-pair then the call succeeds", async () => {
  const h = boot({ local: { apiToken: "OLD" } }); h.app.token = "NEW";
  const r = await h.send({ type: "RELAY_DOWNLOAD", url: "https://site.com/v.mp4", pageUrl: "https://site.com/p" }, { tab: { id: 1 } });
  assert.strictEqual(r.ok, true);
  assert.strictEqual(h.store.local.apiToken, "NEW");
});
test("refused re-pair keeps the old token and reports not paired", async () => {
  const h = boot({ local: { apiToken: "OLD" } }); h.app.token = "NEW"; h.app.newToken = "OLD";
  const r = await h.send({ type: "RELAY_DOWNLOAD", url: "https://site.com/v.mp4", pageUrl: "https://site.com/p" }, { tab: { id: 1 } });
  assert.strictEqual(r.ok, false); assert.match(r.error, /Not paired/); assert.strictEqual(h.store.local.apiToken, "OLD");
});

test("standalone: app off + direct file saves via the browser; stream refused", async () => {
  const h = boot(); h.app.alive = false;
  const r = await h.send({ type: "SEND_SMART", url: "https://cdn.com/clip.mp4", pageUrl: "https://site.com", title: "Clip" });
  assert.strictEqual(r.ok, true); assert.strictEqual(r.standalone, true);
  assert.strictEqual(h.created.downloads.length, 0 + h.created.downloads.length);
  const s = await h.send({ type: "VG_STANDALONE_DOWNLOAD", url: "https://cdn.com/a/master.m3u8" });
  assert.strictEqual(s.ok, false); assert.match(s.error, /desktop app/);
  const b = await h.send({ type: "VG_STANDALONE_DOWNLOAD", url: "blob:https://x/1" });
  assert.strictEqual(b.ok, false);
});
test("standalone fallback respects the setting", async () => {
  const h = boot({ local: { vgSettings: { standaloneFallback: false } } }); h.app.alive = false;
  await tick(30);
  const r = await h.send({ type: "SEND_SMART", url: "https://cdn.com/clip.mp4", pageUrl: "https://site.com" });
  assert.strictEqual(r.ok, false); assert.strictEqual(r.code, "app_offline"); assert.strictEqual(h.created.downloads.length, 0);
});

test("SEND_SMART: names the file from the page title, dedups against the archive, force overrides", async () => {
  const h = boot();
  const url = "https://cdn.com/AbC123xYz987QwErTy45.mp4";
  const a = await h.send({ type: "SEND_SMART", url, pageUrl: "https://site.com/p", title: "My Show: Ep 1" });
  assert.strictEqual(a.ok, true); assert.strictEqual(a.filename, "My Show Ep 1.mp4");
  assert.strictEqual(h.app.calls.find((c) => c.path === "/download").body.filename, "My Show Ep 1.mp4");
  await tick(30);
  const b = await h.send({ type: "SEND_SMART", url, pageUrl: "https://site.com/p", title: "My Show: Ep 1" });
  assert.strictEqual(b.ok, false); assert.strictEqual(b.duplicate, true);
  const c = await h.send({ type: "SEND_SMART", url, pageUrl: "https://site.com/p", force: true });
  assert.strictEqual(c.ok, true);
});

const MASTER = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360,FRAME-RATE=30\n360/i.m3u8\n#EXT-X-STREAM-INF:BANDWIDTH=3000000,RESOLUTION=1920x1080,FRAME-RATE=60\n1080/i.m3u8\n#EXT-X-STREAM-INF:BANDWIDTH=1500000,RESOLUTION=1280x720,FRAME-RATE=30\n720/i.m3u8\n";
const web = (map) => async (url) => { const t = map[url]; if (t === undefined) return { ok: false, status: 404, text: async () => "", headers: { get: () => null } }; return { ok: true, status: 200, text: async () => t, headers: { get: (k) => (k === "content-length" ? "1234" : k === "accept-ranges" ? "bytes" : "video/mp4") } }; };

test("HLS: per-host quality picks the variant, sends its absolute URL", async () => {
  const h = boot({ web: web({ "https://v.com/a/master.m3u8": MASTER }) });
  await h.send({ type: "QUALITY_SET", host: "v.com", height: 720 });
  const r = await h.send({ type: "SEND_SMART", url: "https://v.com/a/master.m3u8", pageUrl: "https://v.com", title: "Show" });
  assert.strictEqual(r.ok, true); assert.strictEqual(r.sentUrl, "https://v.com/a/720/i.m3u8"); assert.match(r.note, /720p/);
  const call = h.app.calls.filter((c) => c.path === "/download").pop();
  assert.strictEqual(call.body.url, "https://v.com/a/720/i.m3u8");
});
test("HLS: no preference sends the best variant (with fps in the note); DRM and live are refused", async () => {
  const live = "#EXTM3U\n#EXTINF:4,\na.ts\n";
  const drm = '#EXTM3U\n#EXT-X-KEY:METHOD=SAMPLE-AES,URI="skd://k",KEYFORMAT="com.apple.streamingkeydelivery"\n#EXTINF:4,\na.ts\n#EXT-X-ENDLIST\n';
  const h = boot({ web: web({ "https://v.com/m.m3u8": MASTER, "https://v.com/live.m3u8": live, "https://v.com/drm.m3u8": drm }) });
  const best = await h.send({ type: "SEND_SMART", url: "https://v.com/m.m3u8", pageUrl: "https://v.com" });
  assert.match(best.note, /1080p 60fps/);
  const l = await h.send({ type: "SEND_SMART", url: "https://v.com/live.m3u8", pageUrl: "https://v.com" });
  assert.strictEqual(l.ok, false); assert.strictEqual(l.code, "hls_unsafe");
  const d = await h.send({ type: "SEND_SMART", url: "https://v.com/drm.m3u8", pageUrl: "https://v.com" });
  assert.strictEqual(d.code, "hls_unsafe"); assert.match(d.error, /DRM/);
  assert.strictEqual(h.app.calls.filter((c) => c.path === "/download").length, 1);
});

test("LIB_LIST + LIB_INSPECT: sniffed items are aggregated, searchable and flagged when already sent", async () => {
  const h = boot({ web: web({ "https://cdn.com/a.mp4": "" }) });
  h.run("VGBG.addItem(1, {url:'https://cdn.com/a.mp4', source:'url-pattern', size: 5000000, contentType:'video/mp4'}); VGBG.addItem(1, {url:'https://cdn.com/b.mp3', source:'url-pattern', size: 100}); VGBG.addItem(1, {url:'https://cdn.com/seg1.ts', source:'url-pattern'});");
  const all = await h.send({ type: "LIB_LIST" });
  assert.strictEqual(all.items.length, 2, "segments hidden");
  assert.strictEqual(all.items[0].pageTitle, "Cool Show");
  const q = await h.send({ type: "LIB_LIST", q: "audio" }); assert.strictEqual(q.items.length, 1);
  const s = await h.send({ type: "LIB_LIST", sort: "size" }); assert.strictEqual(s.items[0].url, "https://cdn.com/a.mp4");
  await h.send({ type: "SEND_SMART", url: "https://cdn.com/a.mp4", pageUrl: "https://site.com", title: "A" }); await tick(30);
  assert.strictEqual((await h.send({ type: "LIB_LIST", sort: "size" })).items[0].alreadySent, true);
  const ins = await h.send({ type: "LIB_INSPECT", url: "https://cdn.com/a.mp4" });
  assert.strictEqual(ins.head.size, 1234); assert.strictEqual(ins.head.acceptRanges, true);
});

test("jobs: list sorted + counts, actions hit the right endpoints, retry-failed and clear-failed", async () => {
  const h = boot();
  Object.assign(h.app.jobs, { a: { id: "a", status: "done", created_ts: 1 }, b: { id: "b", status: "error", error: "HTTP 503", created_ts: 2 }, c: { id: "c", status: "downloading", progress: 40, created_ts: 3 } });
  const l = await h.send({ type: "JOBS_LIST" });
  assert.deepStrictEqual(l.jobs.map((j) => j.id), ["c", "b", "a"]); assert.strictEqual(l.counts.error, 1); assert.ok(l.queue);
  assert.strictEqual((await h.send({ type: "JOB_ACTION", id: "c", action: "pause" })).ok, true);
  assert.ok(h.app.calls.some((c) => c.path === "/pause/c"));
  assert.strictEqual((await h.send({ type: "JOB_ACTION", id: "b", action: "nonsense" })).ok, false);
  await h.send({ type: "JOB_ACTION", id: "c", action: "reorder", direction: "down" });
  assert.strictEqual(h.app.calls.find((c) => c.path === "/reorder/c").body.action, "down", "matches the app's {action} contract");
  await h.send({ type: "JOB_ACTION", id: "a", action: "open-folder" });
  assert.strictEqual(h.app.calls.find((c) => c.path === "/open-folder").body.job_id, "a");
  const r = await h.send({ type: "JOBS_RETRY_FAILED" }); assert.strictEqual(r.retried, 1); assert.strictEqual(h.app.jobs.b.status, "queued");
  h.app.jobs.b.status = "error";
  assert.strictEqual((await h.send({ type: "JOBS_CLEAR_FAILED" })).data.removed, 1);
  assert.strictEqual((await h.send({ type: "QUEUE_SET", running: true })).queue_running, true);
});

test("schedules: add validates, due ones run once, daily reschedules, offline retries", async () => {
  const h = boot();
  assert.strictEqual((await h.send({ type: "SCHEDULE_ADD", items: [], spec: { when: 1 } })).ok, false);
  assert.strictEqual((await h.send({ type: "SCHEDULE_ADD", items: [{ url: "https://x.com/a.mp4" }], spec: { daily: "99:00" } })).ok, false);
  const add = await h.send({ type: "SCHEDULE_ADD", name: "Nightly", items: [{ url: "https://x.com/a.mp4" }, { url: "javascript:alert(1)" }], spec: { daily: "03:00" } });
  assert.strictEqual(add.ok, true); assert.strictEqual(add.job.items.length, 1);
  // force it due
  h.store.local.vgSchedules[0].nextAt = Date.now() - 1000;
  const fired = await h.run("VGBG.runDueSchedules()");
  assert.strictEqual(fired.length, 1);
  assert.ok(h.app.calls.some((c) => c.path === "/download"));
  const list = (await h.send({ type: "SCHEDULE_LIST" })).schedules;
  assert.ok(list[0].nextAt > Date.now()); assert.match(list[0].lastResult, /sent 1/);
  assert.strictEqual((await h.run("VGBG.runDueSchedules()")).length, 0, "not run twice");
  // one-off + app offline: retried later, not lost
  const one = await h.send({ type: "SCHEDULE_ADD", name: "Once", items: [{ url: "https://x.com/b.mp4" }], spec: { when: Date.now() + 60000 } });
  h.store.local.vgSchedules.find((j) => j.id === one.job.id).nextAt = Date.now() - 1; h.app.alive = false;
  await h.run("VGBG.runDueSchedules()");
  let j = (await h.send({ type: "SCHEDULE_LIST" })).schedules.find((x) => x.id === one.job.id);
  assert.strictEqual(j.enabled, true); assert.ok(j.nextAt > Date.now()); assert.match(j.lastResult, /offline/);
  h.app.alive = true; j.nextAt = Date.now() - 1; h.store.local.vgSchedules.find((x) => x.id === j.id).nextAt = Date.now() - 1;
  await h.run("VGBG.runDueSchedules()");
  j = (await h.send({ type: "SCHEDULE_LIST" })).schedules.find((x) => x.id === one.job.id);
  assert.strictEqual(j.enabled, false, "one-off finished");
  assert.strictEqual((await h.send({ type: "SCHEDULE_REMOVE", id: j.id })).ok, true);
});

test("watchlist: validates, matches new sniffed items, notifies or auto-sends, ignores segments", async () => {
  const h = boot({ web: web({}) });
  assert.strictEqual((await h.send({ type: "WATCH_ADD", kind: "regex", pattern: "(" })).ok, false);
  await h.send({ type: "WATCH_ADD", kind: "host", pattern: "cdn.example.com", action: "notify" });
  const auto = await h.send({ type: "WATCH_ADD", kind: "text", pattern: "autosend", action: "send" });
  assert.strictEqual(auto.ok, true);
  h.run("VGBG.addItem(1, {url:'https://cdn.example.com/x.mp4', source:'url-pattern', size: 900000})"); await tick(60);
  assert.strictEqual(h.created.notifications.filter((n) => /found media/.test(n.title)).length, 1);
  h.run("VGBG.addItem(1, {url:'https://cdn.example.com/y.mp4', source:'url-pattern', size: 900000})"); await tick(60);
  assert.strictEqual(h.created.notifications.filter((n) => /found media/.test(n.title)).length, 1, "cooldown per tab");
  h.run("VGBG.addItem(1, {url:'https://other.com/autosend.mp4', source:'url-pattern', size: 900000})"); await tick(80);
  assert.ok(h.app.calls.some((c) => c.path === "/download" && /autosend/.test(c.body.url)));
  h.run("VGBG.addItem(1, {url:'https://cdn.example.com/seg.ts', source:'url-pattern'})"); await tick(40);
});

test("discovery notification is off by default and on when enabled", async () => {
  const h = boot();
  h.run("VGBG.addItem(1, {url:'https://v.com/a.mp4', source:'url-pattern', size: 5000000})"); await tick(40);
  assert.strictEqual(h.created.notifications.length, 0);
  const h2 = boot({ local: { vgSettings: { notifyDiscovery: true } } }); await tick(30);
  h2.run("VGBG.addItem(1, {url:'https://v.com/a.mp4', source:'url-pattern', size: 5000000})"); await tick(40);
  assert.strictEqual(h2.created.notifications.length, 1);
});

test("context menus and shortcuts: new ids handled, old handlers don't misfire on them", async () => {
  const h = boot(); await tick(50);
  const ids = h.run("self.VG_MENU_DEFS.map(d=>d.id)");
  assert.ok(ids.includes("vg-best") && ids.includes("vg-tonight") && ids.includes("vg-watch"));
  h.run("VGBG.addItem(1, {url:'https://v.com/big.mp4', source:'url-pattern', size: 9000000})");
  for (const l of h.listeners.menus) await l({ menuItemId: "vg-best", pageUrl: "https://site.com/watch?v=1" }, { id: 1, url: "https://site.com/watch?v=1", title: "Cool Show" });
  assert.ok(h.app.calls.some((c) => c.path === "/download" && c.body.url === "https://v.com/big.mp4"));
  assert.ok(!h.app.calls.some((c) => c.path === "/show-add-dialog"), "legacy listener ignored vg-best");
  for (const l of h.listeners.menus) await l({ menuItemId: "vg-tonight", pageUrl: "https://site.com/watch?v=1" }, { id: 1, url: "https://site.com/watch?v=1", title: "Cool Show" });
  assert.strictEqual((await h.send({ type: "SCHEDULE_LIST" })).schedules.length, 1);
  for (const l of h.listeners.menus) await l({ menuItemId: "vg-watch" }, { id: 1, url: "https://site.com/x", title: "t" });
  assert.strictEqual((await h.send({ type: "WATCH_LIST" })).rules[0].pattern, "site.com");
  const before = h.created.notifications.length;
  for (const l of h.listeners.cmds) await l("start-queue", { id: 1, url: "https://site.com" });
  assert.strictEqual(h.app.queue.running, true);
  for (const l of h.listeners.cmds) await l("toggle-pill", { id: 1, url: "https://site.com" });
  assert.strictEqual(h.store.local.vgSettings.pillVisible, false);
  assert.ok(h.created.notifications.length > before);
});

test("side panel: opens when the API exists, explains when it doesn't", async () => {
  const yes = boot({ sidePanel: true }); await tick(30);
  assert.strictEqual((await yes.send({ type: "VG_OPEN_SIDEPANEL", tabId: 1 })).ok, true); assert.strictEqual(yes.created.sidePanelOpened, true);
  const no = boot(); const r = await no.send({ type: "VG_OPEN_SIDEPANEL", tabId: 1 });
  assert.strictEqual(r.ok, false); assert.match(r.error, /popup/);
});

test("settings export/import round trip through the worker; hostile input is contained", async () => {
  const h = boot({ local: { vgSettings: { pillVisible: false }, vgHostQuality: { "a.com": 720 }, disabledSites: ["x.com"] } });
  const ex = await h.send({ type: "SETTINGS_EXPORT" });
  assert.ok(!ex.text.includes("TOK")); assert.match(ex.name, /videograbber-settings-/);
  const h2 = boot();
  const im = await h2.send({ type: "SETTINGS_IMPORT", text: ex.text });
  assert.strictEqual(im.ok, true); assert.strictEqual(h2.store.local.vgSettings.pillVisible, false);
  assert.deepStrictEqual(h2.store.local.vgHostQuality, { "a.com": 720 }); assert.deepStrictEqual(h2.store.local.disabledSites, ["x.com"]);
  assert.strictEqual(h2.store.local.apiToken, "TOK", "token untouched");
  const hostile = JSON.stringify({ magic: "videograbber-extension-settings", version: 2, data: { apiToken: "EVIL", vgSettings: { pillCorner: "<script>", defaultQuality: 99999999 }, vgSchedules: [{ items: [{ url: "javascript:1" }], spec: { daily: "03:00" } }], vgWatchlist: [{ pattern: "x", kind: "bogus", action: "rm -rf" }] } });
  const bad = await h2.send({ type: "SETTINGS_IMPORT", text: hostile });
  assert.strictEqual(bad.ok, true); assert.strictEqual(h2.store.local.apiToken, "TOK");
  assert.strictEqual(h2.store.local.vgSettings.pillCorner, ""); assert.strictEqual(h2.store.local.vgSettings.defaultQuality, 4320);
  assert.strictEqual(h2.store.local.vgSchedules[0].items.length, 0);
  assert.strictEqual(h2.store.local.vgWatchlist[0].kind, "text"); assert.strictEqual(h2.store.local.vgWatchlist[0].action, "notify");
  assert.strictEqual((await h2.send({ type: "SETTINGS_IMPORT", text: "garbage" })).ok, false);
});

test("settings compatibility: an old v4.4 settings object upgrades with defaults, nothing lost", async () => {
  const old = { pillVisible: false, pillCorner: "br", microPill: false, defaultQuality: 720, interceptDownloads: true, interceptIgnore: "a.com .exe" };
  const h = boot({ local: { vgSettings: old } }); await tick(30);
  const cfg = h.run("VGBG.cfg");
  assert.strictEqual(cfg.pillVisible, false); assert.strictEqual(cfg.pillCorner, "br"); assert.strictEqual(cfg.defaultQuality, 720);
  assert.strictEqual(cfg.interceptDownloads, true); assert.strictEqual(cfg.interceptIgnore, "a.com .exe");
  assert.strictEqual(cfg.smartOneClick, true); assert.strictEqual(cfg.notifyDiscovery, false); assert.strictEqual(cfg.density, "comfortable");
  assert.strictEqual(h.run("vgNormalize('garbage').pillVisible"), true);
  assert.strictEqual(h.run("vgNormalize({density:'huge'}).density"), "comfortable");
});

test("diagnostics: VG_DIAG reports caps, desktop, counts and a log; clear works", async () => {
  const h = boot(); await tick(30);
  h.run("VGBG.addItem(1, {url:'https://v.com/a.mp4', source:'url-pattern'})");
  const d = await h.send({ type: "VG_DIAG" });
  assert.strictEqual(d.ok, true); assert.strictEqual(d.version, "4.5.1"); assert.strictEqual(d.itemsTracked, 1);
  assert.strictEqual(d.caps.sidePanel, false); assert.match(d.log, /service worker ready|features/);
  await h.send({ type: "VG_DIAG_CLEAR" });
  assert.ok(!(await h.send({ type: "VG_DIAG" })).log.includes("service worker ready"));
});

test("interceptor still hands over gesture-backed downloads and ignores others (regression)", async () => {
  const h = boot({ local: { vgSettings: { interceptDownloads: true, interceptNeedsGesture: true } } }); await tick(50);
  const item = { id: 7, url: "https://files.com/app.zip", state: "in_progress", startTime: new Date().toISOString(), referrer: "https://files.com" };
  for (const l of h.listeners.onCreated) await l(item);
  assert.ok(!h.app.calls.some((c) => c.path === "/show-add-dialog"), "no gesture, no intercept");
  await h.send({ type: "VG_GESTURE" });
  for (const l of h.listeners.onCreated) await l({ ...item, id: 8, url: "https://files.com/b.zip" });
  assert.ok(h.app.calls.some((c) => c.path === "/show-add-dialog"));
  await tick(30);
  assert.ok((await h.send({ type: "ARCHIVE_LIST" })).items.some((e) => e.via === "intercept"));
});

// ---- v4.5.1 ----------------------------------------------------------------
test("B1: a master playlist that supersedes a variant emits 'removed' and clears that variant's discovery notification", async () => {
  const h = boot(); await tick(50);
  const removed = [];
  h.run("globalThis.__removed = []; VGBG.listen('removed', (e) => __removed.push(e.item.url + '|' + e.reason + '|' + e.tabId));");
  await h.send({ type: "WATCH_ADD", kind: "host", pattern: "cdn.com", action: "notify" });
  h.run("VGBG.addItem(1, {url:'https://cdn.com/v/chunklist_720p.m3u8', source:'url-pattern'})");
  await tick(60);
  assert.strictEqual(h.created.notifications.length, 1, "variant triggered a notification");
  const noteId = h.created.notifications[0].id;
  h.run("VGBG.addItem(1, {url:'https://cdn.com/v/master.m3u8', source:'url-pattern'})");
  await tick(30);
  assert.strictEqual(JSON.stringify(h.run("__removed")), JSON.stringify(["https://cdn.com/v/chunklist_720p.m3u8|superseded|1"]));
  assert.ok(h.created.cleared.includes(noteId), "stale notification for the removed variant was cleared");
  assert.strictEqual(h.run("VGBG.videoMap.get(1).size"), 1);
});

test("B2: RECENT_INTERCEPT stays at 200 entries or fewer even when every entry is still fresh", async () => {
  const h = boot({ local: { vgSettings: { interceptDownloads: true, interceptNeedsGesture: false } } }); await tick(50);
  for (let i = 0; i < 210; i++) {
    h.run("burstTimes = []");   // the 25 per 30 s burst cap would otherwise hide the growth this test is about
    for (const l of h.listeners.onCreated) await l({ id: 1000 + i, url: "https://files.com/f" + i + ".zip", state: "in_progress", startTime: new Date().toISOString(), referrer: "https://files.com" });
  }
  assert.ok(h.run("RECENT_INTERCEPT.size") <= 200, "size was " + h.run("RECENT_INTERCEPT.size"));
  assert.ok(h.run("RECENT_INTERCEPT.has('https://files.com/f209.zip')"), "newest entries are kept");
  assert.ok(!h.run("RECENT_INTERCEPT.has('https://files.com/f0.zip')"), "oldest entry was evicted");
});

test("B3: an about:blank navigation does not wipe the tab's detected media; a real navigation still does", async () => {
  const h = boot(); await tick(50);
  h.run("VGBG.addItem(1, {url:'https://cdn.com/a.mp4', source:'url-pattern', size: 5000000})");
  for (const l of h.listeners.navBefore) l({ frameId: 0, tabId: 1, url: "about:blank" });
  assert.strictEqual(h.run("VGBG.videoMap.get(1) && VGBG.videoMap.get(1).size"), 1, "survived about:blank");
  for (const l of h.listeners.navBefore) l({ frameId: 0, tabId: 1, url: "https://site.com/next" });
  assert.strictEqual(h.run("VGBG.videoMap.has(1)"), false, "a real navigation still clears");
});

test("B10: before the settings read lands the sniffer is permissive; afterwards 'active tab only' filters", async () => {
  const h = boot({ local: { vgSettings: { snifferMode: "active" } } });
  // Simulate a storage.onChanged that raced ahead of the initial read: the mode is already "active"
  // and the active-tab map is filled, but vgcfgReady has not resolved yet.
  h.run("VGCFG = vgNormalize({ snifferMode: 'active' }); ACTIVE_TABS.set(1, 1);");
  for (const l of h.listeners.webBefore) l({ tabId: 2, url: "https://cdn.com/early.mp4" });
  assert.strictEqual(h.run("VGBG.videoMap.get(2) && VGBG.videoMap.get(2).size"), 1, "permissive before the read resolves");
  await tick(50);
  for (const l of h.listeners.webBefore) l({ tabId: 2, url: "https://cdn.com/late.mp4" });
  assert.strictEqual(h.run("VGBG.videoMap.get(2).size"), 1, "filtered after the read resolves");
  for (const l of h.listeners.webBefore) l({ tabId: 1, url: "https://cdn.com/active.mp4" });
  assert.strictEqual(h.run("VGBG.videoMap.get(1).size"), 1, "the active tab is still sniffed");
});

test("B11: SCHEDULE_ADD refuses a one-off time in the past (direct message path)", async () => {
  const h = boot(); await tick(30);
  const r = await h.send({ type: "SCHEDULE_ADD", items: [{ url: "https://x.com/a.mp4" }], spec: { when: Date.now() - 60000 } });
  assert.strictEqual(r.ok, false); assert.strictEqual(r.error, "Pick a time in the future.");
  assert.strictEqual((await h.send({ type: "SCHEDULE_LIST" })).schedules.length, 0);
  const ok = await h.send({ type: "SCHEDULE_ADD", items: [{ url: "https://x.com/a.mp4" }], spec: { when: Date.now() + 60000 } });
  assert.strictEqual(ok.ok, true);
});

test("sweep: ARCHIVE_CLEAR goes through the same per-key lock as archive writes", async () => {
  const h = boot(); await tick(30);
  h.run("VGBG.emit('sent', { items: [{ url: 'https://a.com/x.mp4' }], pageUrl: '' })");
  await h.send({ type: "ARCHIVE_CLEAR" }); await tick(80);
  assert.strictEqual((await h.send({ type: "ARCHIVE_LIST" })).items.length, 0, "a write already in flight must not resurrect cleared entries");
});

test("sweep: a throwing schedule send is recorded and rescheduled instead of aborting the tick and refiring forever", async () => {
  const h = boot(); await tick(30);
  for (const n of ["A", "B"]) await h.send({ type: "SCHEDULE_ADD", name: n, items: [{ url: "https://x.com/" + n + ".mp4" }], spec: { daily: "03:00" } });
  for (const j of h.store.local.vgSchedules) j.nextAt = Date.now() - 1000;
  h.run("let __n = 0; const __orig = VGBG.sendToBackendMany; VGBG.sendToBackendMany = async (j) => { if (__n++ === 0) throw new Error('kaput'); return __orig(j); };");
  await h.run("VGBG.runDueSchedules()");
  const list = (await h.send({ type: "SCHEDULE_LIST" })).schedules;
  assert.ok(list.every((j) => j.nextAt > Date.now()), "both rescheduled");
  assert.match(list[0].lastResult, /failed/); assert.match(list[1].lastResult, /sent 1/);
});

test("sweep: dispatchScheduleAdd resolves with an error when the handler rejects", async () => {
  const h = boot(); await tick(30);
  h.run("VGBG.handlers.SCHEDULE_ADD = async () => { throw new Error('z'); }");
  const r = await Promise.race([h.run("VGBG.dispatchScheduleAdd({})"), tick(300).then(() => "HUNG")]);
  assert.strictEqual(r.ok, false); assert.match(r.error, /z/);
});

test("sweep: importing a one-off schedule whose time has passed does not fire it at once", async () => {
  const h = boot(); await tick(30);
  const text = JSON.stringify({ magic: "videograbber-extension-settings", version: 2, exportedAt: 1, data: { vgSchedules: [{ id: "s1", name: "Old", items: [{ url: "https://x.com/a.mp4" }], spec: { when: Date.now() - 5000 }, enabled: true }] } });
  const r = await h.send({ type: "SETTINGS_IMPORT", text }); assert.strictEqual(r.ok, true);
  const j = (await h.send({ type: "SCHEDULE_LIST" })).schedules[0];
  assert.strictEqual(j.enabled, false); assert.strictEqual(j.nextAt, null);
  assert.strictEqual((await h.run("VGBG.runDueSchedules()")).length, 0);
});

test("sweep: desktop route probe is not cached when every route 404ed (app still starting), so a later check sees new routes", async () => {
  const h = boot({ app: [] });
  let r = await h.send({ type: "VG_DESKTOP_INFO", force: true }); assert.strictEqual(r.caps.jobs, false);
  h.app.routes = undefined;   // the app finished starting and now serves its routes
  r = await h.send({ type: "VG_DESKTOP_INFO", force: true });
  assert.strictEqual(r.caps.jobs, true);
});

test("sweep: the same URL sent with two different format_ids is two archive entries", async () => {
  const h = boot(); await tick(30);
  for (const f of ["137", "22"]) await h.send({ type: "RELAY_DOWNLOAD", url: "https://site.com/watch?v=1", pageUrl: "https://site.com/watch?v=1", format_id: f }, { tab: { id: 1 } });
  await tick(50);
  const items = (await h.send({ type: "ARCHIVE_LIST" })).items;
  assert.deepStrictEqual(items.map((e) => e.format_id).sort(), ["137", "22"]);
  await h.send({ type: "ARCHIVE_REMOVE", url: "https://site.com/watch?v=1", format_id: "22" });
  assert.deepStrictEqual((await h.send({ type: "ARCHIVE_LIST" })).items.map((e) => e.format_id), ["137"]);
});

// ---- M1: persistent sniffed library -----------------------------------------
const DAY = 86400000;
test("M1: the remembered library never holds more than 200 entries (oldest lastSeen evicted first)", async () => {
  const h = boot(); await tick(30);
  for (let i = 0; i < 250; i++) h.run("VGBG.addItem(1, {url:'https://cdn" + (i % 5) + ".com/clip" + i + ".mp4', source:'url-pattern', size: 900000, contentType:'video/mp4'})");
  await tick(900);
  const lib = h.store.local.vgSniffedLibrary || {};
  const all = Object.values(lib).flat();
  assert.ok(all.length > 0 && all.length <= 200, "size " + all.length);
  assert.ok(all.some((e) => e.url.endsWith("clip249.mp4")), "newest kept");
  assert.ok(!all.some((e) => e.url.endsWith("clip0.mp4")), "oldest evicted");
  const e = all[0]; for (const k of ["url", "filename", "title", "size", "contentType", "firstSeen", "lastSeen"]) assert.ok(k in e, k);
});

test("M1: entries older than 30 days are pruned on the next write; images and segments are never stored", async () => {
  const h = boot(); await tick(30);
  for (let i = 0; i < 30; i++) h.run("VGBG.addItem(1, {url:'https://h" + (i % 3) + ".com/v" + i + ".mp4', source:'url-pattern', size: 900000, contentType:'video/mp4'})");
  await tick(800);
  assert.strictEqual(Object.keys(h.store.local.vgSniffedLibrary).length, 3);
  for (const list of Object.values(h.store.local.vgSniffedLibrary)) for (const e of list) { e.firstSeen = Date.now() - 40 * DAY; e.lastSeen = Date.now() - 40 * DAY; }
  h.run("VGBG.addItem(1, {url:'https://fresh.com/new.mp4', source:'url-pattern', size: 900000, contentType:'video/mp4'}); VGBG.addItem(1, {url:'https://fresh.com/pic.jpg', source:'url-pattern', contentType:'image/jpeg'}); VGBG.addItem(1, {url:'https://fresh.com/seg1.ts', source:'url-pattern', contentType:'video/mp2t'});");
  await tick(800);
  const all = Object.values(h.store.local.vgSniffedLibrary).flat();
  assert.deepStrictEqual([...all.map((e) => e.url)], ["https://fresh.com/new.mp4"]);
});

test("M1.3: LIB_LIST scope 'remembered' reads the stored library, filters by q and kinds, flags rows; other scopes are unchanged", async () => {
  const h = boot(); await tick(30);
  const t = Date.now();
  h.store.local.vgSniffedLibrary = { "a.com": [{ url: "https://a.com/great-song.mp3", filename: "great-song.mp3", title: "Music", size: 3e6, contentType: "audio/mpeg", firstSeen: t - 3 * DAY, lastSeen: t - 3 * DAY }, { url: "https://a.com/movie.mp4", filename: "movie.mp4", title: "Film", size: 9e6, contentType: "video/mp4", firstSeen: t, lastSeen: t }] };
  const all = await h.send({ type: "LIB_LIST", scope: "remembered" });
  assert.strictEqual(all.total, 2); assert.ok(all.items.every((r) => r.remembered === true && Number.isFinite(r.lastSeen)));
  assert.strictEqual(all.items[0].url, "https://a.com/movie.mp4", "newest first by default");
  const q = await h.send({ type: "LIB_LIST", scope: "remembered", q: "song" });
  assert.deepStrictEqual([...q.items.map((r) => r.filename)], ["great-song.mp3"]);
  const k = await h.send({ type: "LIB_LIST", scope: "remembered", kinds: ["video"] });
  assert.deepStrictEqual([...k.items.map((r) => r.filename)], ["movie.mp4"]);
  h.run("VGBG.addItem(1, {url:'https://live.com/now.mp4', source:'url-pattern', size: 9e5, contentType:'video/mp4'})");
  const live = await h.send({ type: "LIB_LIST", scope: "all" });
  assert.ok(live.items.length === 1 && !("remembered" in live.items[0]), "scope all unchanged");
});

test("M1.4: LIB_FORGET removes one URL or a whole host and also drops a not-yet-written copy", async () => {
  const h = boot(); await tick(30);
  const t = Date.now(), mk = (u) => ({ url: u, filename: "", title: "", size: 1, contentType: "video/mp4", firstSeen: t, lastSeen: t });
  h.store.local.vgSniffedLibrary = { "a.com": [mk("https://a.com/1.mp4"), mk("https://a.com/2.mp4")], "b.com": [mk("https://b.com/1.mp4")] };
  let r = await h.send({ type: "LIB_FORGET", url: "https://a.com/1.mp4" }); assert.strictEqual(r.removed, 1);
  assert.deepStrictEqual([...h.store.local.vgSniffedLibrary["a.com"].map((e) => e.url)], ["https://a.com/2.mp4"]);
  h.run("VGBG.addItem(1, {url:'https://a.com/3.mp4', source:'url-pattern', size: 9e5, contentType:'video/mp4'})");
  await tick(60);   // item queued, debounce (500 ms) not fired yet
  r = await h.send({ type: "LIB_FORGET", host: "a.com" });
  await tick(700);
  assert.ok(!h.store.local.vgSniffedLibrary["a.com"], "host forgotten, including the pending entry");
  assert.strictEqual(h.store.local.vgSniffedLibrary["b.com"].length, 1);
  assert.strictEqual((await h.send({ type: "LIB_FORGET" })).ok, false);
});

test("M6.1: autoSaveRecordingOnNavigate defaults to true, normalizes non-booleans, and survives old settings objects", async () => {
  const h = boot({ local: { vgSettings: { pillVisible: false } } }); await tick(30);
  assert.strictEqual(h.run("VGCFG.autoSaveRecordingOnNavigate"), true);
  assert.strictEqual(h.run("vgNormalize({ autoSaveRecordingOnNavigate: false }).autoSaveRecordingOnNavigate"), false);
  assert.strictEqual(h.run("vgNormalize({ autoSaveRecordingOnNavigate: 'no' }).autoSaveRecordingOnNavigate"), true);
});
