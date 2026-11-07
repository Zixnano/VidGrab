const test = require("node:test");
const assert = require("node:assert");
const C = require("../vgcore.js");

test("sizeLabel", () => {
  assert.strictEqual(C.sizeLabel(0), "");
  assert.strictEqual(C.sizeLabel(1536), "1.5 KB");
  assert.strictEqual(C.sizeLabel(5 * 1024 ** 3), "5.0 GB");
});

test("improveFilename: random CDN name falls back to page title", () => {
  const n = C.improveFilename({ filename: "AbC123xYz987QwErTy45.mp4", title: "My: Cool/Video?", url: "https://cdn.x.com/a.mp4" });
  assert.strictEqual(n, "My Cool Video.mp4");
});
test("improveFilename: good names kept, illegal chars removed, ext from content-type", () => {
  assert.strictEqual(C.improveFilename({ filename: "holiday  trip.mp4" }), "holiday trip.mp4");
  assert.strictEqual(C.improveFilename({ filename: "clip", contentType: "video/webm" }), "clip.webm");
  assert.strictEqual(C.improveFilename({ filename: "CON.mp4" }), "_CON.mp4");
});
test("improveFilename: generic names (master.m3u8) use title and resolution", () => {
  const n = C.improveFilename({ filename: "master.m3u8", title: "Lecture 4", url: "https://a.com/master.m3u8", resolution: "720p" });
  assert.strictEqual(n, "Lecture 4 720p");
});
test("improveFilename: long names are truncated but keep the extension", () => {
  const n = C.improveFilename({ filename: "a ".repeat(200) + ".mp4" });
  assert.ok(n.length <= 120 && n.endsWith(".mp4"));
});
test("expandTemplate", () => {
  const out = C.expandTemplate("{host} - {title} {nn}", { url: "https://www.site.com/v.mp4", title: "Ep", n: 3, now: new Date(2026, 0, 2) });
  assert.strictEqual(out, "site.com - Ep 03.mp4");
  assert.strictEqual(C.expandTemplate("{date}", { now: new Date(2026, 10, 1) }), "2026-11-01");
});

test("inspectItem", () => {
  const a = C.inspectItem({ url: "https://v.com/a/1080p/seg.ts?token=abc", size: 5000 });
  assert.strictEqual(a.isSegment, true);
  assert.strictEqual(a.resolutionHint, "1080p");
  assert.ok(a.warnings.some((w) => /Signed URL/.test(w)));
  const b = C.inspectItem({ url: "https://v.com/index.m3u8", contentType: "application/vnd.apple.mpegurl" });
  assert.strictEqual(b.kind, "stream");
  assert.strictEqual(b.isManifest, true);
  assert.ok(C.inspectItem({ url: "https://v.com/x.mp4", size: 900 }).warnings.some((w) => /small/i.test(w)));
});

const MASTER = `#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360,FRAME-RATE=29.970,CODECS="avc1.4d401e,mp4a.40.2"
360/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=3000000,RESOLUTION=1920x1080,FRAME-RATE=60.000
1080/index.m3u8
`;
test("analyzeM3U8: master lists variants with fps, best first", () => {
  const r = C.analyzeM3U8(MASTER);
  assert.strictEqual(r.master, true);
  assert.strictEqual(r.variants[0].height, 1080);
  assert.strictEqual(r.variants[0].fps, 60);
  assert.strictEqual(r.variants[1].fps, 29.97);
  assert.strictEqual(r.safeToMerge, true);
});
test("analyzeM3U8: VOD is safe, live and DRM are not", () => {
  const vod = "#EXTM3U\n#EXTINF:4.0,\na.ts\n#EXTINF:4.0,\nb.ts\n#EXT-X-ENDLIST\n";
  const v = C.analyzeM3U8(vod);
  assert.strictEqual(v.safeToMerge, true); assert.strictEqual(v.duration, 8);
  const live = C.analyzeM3U8("#EXTM3U\n#EXTINF:4.0,\na.ts\n");
  assert.strictEqual(live.live, true); assert.strictEqual(live.safeToMerge, false);
  const drm = C.analyzeM3U8('#EXTM3U\n#EXT-X-KEY:METHOD=SAMPLE-AES,URI="skd://x",KEYFORMAT="com.apple.streamingkeydelivery"\n#EXTINF:4,\na.ts\n#EXT-X-ENDLIST\n');
  assert.strictEqual(drm.drm, true); assert.strictEqual(drm.safeToMerge, false);
  const aes = C.analyzeM3U8('#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="k"\n#EXTINF:4,\na.ts\n#EXT-X-ENDLIST\n');
  assert.strictEqual(aes.drm, false); assert.strictEqual(aes.encrypted, true); assert.strictEqual(aes.safeToMerge, true);
  assert.strictEqual(C.analyzeM3U8("<html>").valid, false);
});

test("search and sort", () => {
  const items = [
    { url: "https://a.com/x.mp4", filename: "alpha.mp4", size: 10, timestamp: 1 },
    { url: "https://b.com/y.mp3", filename: "beta.mp3", size: 99, timestamp: 3 },
    { url: "https://a.com/z.png", filename: "gamma.png", size: 50, timestamp: 2 },
  ];
  assert.deepStrictEqual(C.searchItems(items, "a.com mp4").map((i) => i.filename), ["alpha.mp4"]);
  assert.strictEqual(C.searchItems(items, "audio").length, 1);
  assert.deepStrictEqual(C.sortItems(items, "size").map((i) => i.size), [99, 50, 10]);
  assert.deepStrictEqual(C.sortItems(items, "time").map((i) => i.timestamp), [3, 2, 1]);
  assert.deepStrictEqual(C.sortItems(items, "name").map((i) => i.filename), ["alpha.mp4", "beta.mp3", "gamma.png"]);
  assert.strictEqual(C.sortItems(items, "host")[0].url.includes("a.com"), true);
});

test("per-host quality prefs", () => {
  let p = C.setQuality({}, "www.Example.com", 720);
  assert.strictEqual(C.qualityFor(p, "example.com", 1080), 720);
  assert.strictEqual(C.qualityFor(p, "cdn.example.com", 1080), 720);
  assert.strictEqual(C.qualityFor(p, "other.com", 1080), 1080);
  p = C.setQuality(p, "example.com", 0);
  assert.strictEqual(C.qualityFor(p, "example.com", 0), 0);
});
test("pickFormat respects the cap and prefers higher fps", () => {
  const f = [
    { format_id: "a", resolution: "1280x720", fps: 30, vcodec: "avc1" },
    { format_id: "b", resolution: "1280x720", fps: 60, vcodec: "avc1" },
    { format_id: "c", resolution: "1920x1080", fps: 30, vcodec: "avc1" },
    { format_id: "d", resolution: "audio only", fps: 0, vcodec: "none" },
  ];
  assert.strictEqual(C.pickFormat(f, 720).format_id, "b");
  assert.strictEqual(C.pickFormat(f, 0).format_id, "c");
  assert.strictEqual(C.pickFormat(f, 100).format_id, "a");
  assert.strictEqual(C.pickFormat([], 0), null);
  assert.strictEqual(C.fpsLabel({ fps: 59.94 }), "60 fps");
  assert.strictEqual(C.fpsLabel({}), "");
});

test("classifyError", () => {
  assert.strictEqual(C.classifyError("Video Grabber app isn't running.").code, "app_offline");
  assert.strictEqual(C.classifyError("Not paired yet.").code, "not_paired");
  assert.strictEqual(C.classifyError(new Error("The operation was aborted")).code, "timeout");
  assert.strictEqual(C.classifyError("HTTP 429").retryable, true);
  assert.strictEqual(C.classifyError("weird").code, "unknown");
  assert.strictEqual(C.retryDelayMs(0), 2000);
  assert.strictEqual(C.retryDelayMs(10), 120000);
});

test("archive keeps newest first, counts repeats, caps size, searches", () => {
  let a = [];
  a = C.archiveAdd(a, { url: "https://a.com/1.mp4", filename: "one.mp4", status: "sent" }, 3);
  a = C.archiveAdd(a, { url: "https://b.com/2.mp4", filename: "two.mp4", status: "sent" }, 3);
  a = C.archiveAdd(a, { url: "https://a.com/1.mp4", filename: "one.mp4", status: "sent" }, 3);
  assert.strictEqual(a.length, 2); assert.strictEqual(a[0].count, 2); assert.strictEqual(a[0].url, "https://a.com/1.mp4");
  a = C.archiveAdd(a, { url: "https://c.com/3.mp4" }, 3); a = C.archiveAdd(a, { url: "https://d.com/4.mp4" }, 3);
  assert.strictEqual(a.length, 3);
  assert.strictEqual(C.archiveHas(a, "https://d.com/4.mp4"), true);
  assert.strictEqual(C.archiveSearch(a, "c.com").length, 1);
  assert.strictEqual(C.archiveSearch(a, "", { host: "d.com" }).length, 1);
});

test("nextRun / isDue", () => {
  const now = new Date(2026, 10, 1, 10, 0, 0).getTime();
  assert.strictEqual(C.nextRun({ when: now + 5000 }, now), now + 5000);
  const d = new Date(C.nextRun({ daily: "09:30" }, now));
  assert.strictEqual(d.getDate(), 2); assert.strictEqual(d.getHours(), 9); assert.strictEqual(d.getMinutes(), 30);
  const d2 = new Date(C.nextRun({ daily: "23:15" }, now));
  assert.strictEqual(d2.getDate(), 1); assert.strictEqual(d2.getHours(), 23);
  assert.strictEqual(C.nextRun({ daily: "25:00" }, now), null);
  assert.strictEqual(C.nextRun({ every: 30, anchor: now - 45 * 60000 }, now), now + 15 * 60000);
  assert.strictEqual(C.nextRun({}, now), null);
  assert.strictEqual(C.isDue({ nextAt: now - 1 }, now), true);
  assert.strictEqual(C.isDue({ nextAt: now - 1, enabled: false }, now), false);
});

test("watchMatches", () => {
  const rules = [
    { id: 1, kind: "host", pattern: "example.com" },
    { id: 2, kind: "text", pattern: "lecture" },
    { id: 3, kind: "regex", pattern: "ep\\d+" },
    { id: 4, kind: "host", pattern: "off.com", enabled: false },
    { id: 5, kind: "regex", pattern: "(" },
  ];
  const ids = (it, p) => C.watchMatches(rules, it, p).map((r) => r.id);
  assert.deepStrictEqual(ids({ url: "https://cdn.example.com/a.mp4" }, ""), [1]);
  assert.deepStrictEqual(ids({ url: "https://x.com/Lecture1.mp4" }, ""), [2]);
  assert.deepStrictEqual(ids({ url: "https://x.com/ep12.mp4" }, ""), [3]);
  assert.deepStrictEqual(ids({ url: "https://off.com/a.mp4" }, ""), []);
});

test("settings export/import round trip and rejection", () => {
  const txt = C.exportBundle({ vgSettings: { pillVisible: false }, vgHostQuality: { "a.com": 720 }, apiToken: "SECRET" }, 5);
  assert.ok(!txt.includes("SECRET"));
  const r = C.parseImport(txt);
  assert.strictEqual(r.ok, true); assert.deepStrictEqual(r.data.vgHostQuality, { "a.com": 720 });
  assert.strictEqual(C.parseImport("nope").ok, false);
  assert.strictEqual(C.parseImport('{"magic":"x"}').ok, false);
  const evil = JSON.stringify({ magic: "videograbber-extension-settings", version: 2, data: { apiToken: "x", vgSettings: {} } });
  const e = C.parseImport(evil);
  assert.strictEqual(e.ok, true); assert.ok(!("apiToken" in e.data)); assert.strictEqual(e.warnings.length, 1);
  const newer = JSON.stringify({ magic: "videograbber-extension-settings", version: 99, data: {} });
  assert.strictEqual(C.parseImport(newer).ok, false);
});

test("sweep: classifyError does not read status codes out of unrelated numbers", () => {
  assert.strictEqual(C.classifyError("saved 4010 frames").code, "unknown");
  assert.strictEqual(C.classifyError("file_15003.mp4 missing").code, "unknown");
  assert.strictEqual(C.classifyError("HTTP 404").code, "gone");
  assert.strictEqual(C.classifyError("HTTP 503 Service Unavailable").code, "server");
  assert.strictEqual(C.classifyError("401 Unauthorized").code, "not_paired");
});

test("sweep: watch host rules match when the user typed www., and catastrophic regexes are flagged and never run", () => {
  const it = { url: "https://cdn.example.com/a.mp4" };
  assert.strictEqual(C.watchMatches([{ kind: "host", pattern: "www.example.com", action: "notify" }], it, "").length, 1);
  assert.strictEqual(C.regexLooksUnsafe("(a+)+$"), true);
  assert.strictEqual(C.regexLooksUnsafe("(x*)*y"), true);
  assert.strictEqual(C.regexLooksUnsafe("ep\\d+"), false);
  assert.strictEqual(C.regexLooksUnsafe("(ab)+"), false);
  const t = Date.now();
  assert.strictEqual(C.watchMatches([{ kind: "regex", pattern: "(a+)+$", action: "notify" }], { url: "https://x.com/" + "a".repeat(40) + "!" }, "").length, 0);
  assert.ok(Date.now() - t < 500, "no backtracking storm");
});

test("M1 helpers: libraryMerge updates, prunes at 30 days, caps at 200; libraryForget; relTime", () => {
  const now = 1e12, D = 86400000;
  let lib = C.libraryMerge({}, [{ url: "https://a.com/x.mp4", size: 5 }], now);
  lib = C.libraryMerge(lib, [{ url: "https://a.com/x.mp4", size: 9, title: "T" }], now + 1000);
  assert.strictEqual(lib["a.com"].length, 1); assert.strictEqual(lib["a.com"][0].size, 9); assert.strictEqual(lib["a.com"][0].firstSeen, now); assert.strictEqual(lib["a.com"][0].lastSeen, now + 1000);
  assert.deepStrictEqual(C.libraryMerge(lib, [], now + 31 * D), {});
  const many = []; for (let i = 0; i < 300; i++) many.push({ url: "https://h" + (i % 7) + ".com/" + i + ".mp4" });
  assert.strictEqual(Object.values(C.libraryMerge({}, many, now)).flat().length, 200);
  assert.deepStrictEqual(C.libraryForget(lib, { host: "a.com" }), {});
  assert.strictEqual(C.relTime(now - 3 * D, now), "3 days ago"); assert.strictEqual(C.relTime(now - 3600e3, now), "1 hour ago"); assert.strictEqual(C.relTime(now - 5e3, now), "just now");
});

test("sweep (verified, no bug): improveFilename keeps the URL extension when the name is random and neither title nor host exist", () => {
  const out = C.improveFilename({ filename: "AbC123xYz987QwErTy45.mp4", url: "notaurl/AbC123xYz987QwErTy45.mp4" });
  assert.match(out, /\.mp4$/);
  assert.match(C.improveFilename({ filename: "chunklist", url: "https://x.invalid/a/chunklist.m3u8", contentType: "video/mp4" }), /\.mp4$/);
});
