// bg-features.js: Batch 2 features that live in the service worker. Loaded by
// background.js through importScripts() once the core (sniffer, app client,
// dispatcher) is ready, and talks to it only through the VGBG object, so the
// core file stays free of feature code.
(function () {
  "use strict";
  const B = self.VGBG, C = self.VGCORE, L = self.VGLOG;
  if (!B || !C || !L) return;

  // ---------------------------------------------------------------- storage --
  const K = { quality: "vgHostQuality", templates: "vgTemplates", watch: "vgWatchlist",
    sched: "vgSchedules", archive: "vgArchive", library: "vgSniffedLibrary" };
  async function sget(key, dflt) {
    try { const st = await chrome.storage.local.get(key); return st[key] === undefined ? dflt : st[key]; }
    catch (e) { L.warn("store", "get " + key + " failed", e); return dflt; }
  }
  async function sset(key, val) {
    try { await chrome.storage.local.set({ [key]: val }); return true; }
    catch (e) { L.error("store", "set " + key + " failed", e); return false; }
  }
  // Serialise read-modify-write per key so two quick events can't lose an update.
  const _locks = Object.create(null);
  function update(key, dflt, fn) {
    const prev = _locks[key] || Promise.resolve();
    const next = prev.then(async () => { const cur = await sget(key, dflt); const out = await fn(cur); await sset(key, out); return out; });
    _locks[key] = next.catch(() => {});
    return next;
  }

  // Shared with bg-library.js (loaded after this file).
  B.store = { get: sget, set: sset, update };

  // ------------------------------------------------------------- app client --
  // One path for every call to the app: alive check, auth, JSON, classified error.
  async function app(path, opts) {
    opts = opts || {};
    if (!(await B.checkBackend())) return { ok: false, ...C.classifyError("Video Grabber app isn't running."), error: "Video Grabber app isn't running." , offline: true };
    try {
      const { res, unpaired } = await B.authedFetch(path, {
        method: opts.method || "GET",
        headers: opts.body ? { "Content-Type": "application/json" } : {},
        body: opts.body ? JSON.stringify(opts.body) : undefined,
      });
      if (unpaired) return { ok: false, error: "Not paired yet.", code: "not_paired" };
      const data = await res.json().catch(() => ({}));
      if (!res.ok) { const c = C.classifyError((data && data.error) || ("HTTP " + res.status)); return { ok: false, status: res.status, data, error: (data && data.error) || c.message, code: c.code, retryable: c.retryable }; }
      return { ok: true, status: res.status, data };
    } catch (e) {
      const c = C.classifyError(e);
      return { ok: false, error: c.message, code: c.code, retryable: c.retryable };
    }
  }

  // ----------------------------------------------------------------- archive --
  B.listen("sent", (evt) => {
    update(K.archive, [], (list) => {
      let out = list;
      for (const it of evt.items || []) {
        if (!it || !it.url) continue;
        out = C.archiveAdd(out, { url: it.url, filename: it.filename || "", title: it.title || "", pageUrl: evt.pageUrl || "",
          status: "sent", via: evt.viaIntercept ? "intercept" : (evt.viaDialog ? "dialog" : "direct"), jobId: (evt.job_ids || [])[0] || "", format_id: it.format_id || "" });
      }
      return out;
    }).catch((e) => L.warn("archive", "add failed", e));
  });
  B.on("ARCHIVE_LIST", async (msg) => {
    const list = await sget(K.archive, []);
    const rows = C.archiveSearch(list, msg.q, { status: msg.status, host: msg.host });
    return { ok: true, total: list.length, items: rows.slice(0, msg.limit || 300) };
  });
  B.on("ARCHIVE_REMOVE", async (msg) => { await update(K.archive, [], (l) => l.filter((e) => !(e.url === msg.url && (msg.format_id === undefined || (e.format_id || "") === msg.format_id)))); return { ok: true }; });
  B.on("ARCHIVE_CLEAR", async () => { await update(K.archive, [], () => []); return { ok: true }; });   // sweep: locked like every other archive write

  // ------------------------------------------------------- per-host quality --
  B.on("QUALITY_GET", async (msg) => {
    const prefs = await sget(K.quality, {});
    return { ok: true, prefs, height: msg.host ? C.qualityFor(prefs, msg.host, B.cfg.defaultQuality) : undefined };
  });
  B.on("QUALITY_SET", async (msg) => {
    const out = await update(K.quality, {}, (p) => C.setQuality(p, msg.host, msg.height));
    return { ok: true, prefs: out };
  });

  // --------------------------------------------------------------- templates --
  const DEFAULT_TEMPLATES = [{ id: "default", name: "Title", pattern: "{title}" },
    { id: "host-title", name: "Site - Title", pattern: "{host} - {title}" },
    { id: "dated", name: "Date Title", pattern: "{date} {title}" },
    { id: "series", name: "Title 01, 02, ...", pattern: "{title} {nn}" }];
  B.on("TEMPLATES_GET", async () => ({ ok: true, templates: await sget(K.templates, DEFAULT_TEMPLATES), defaults: DEFAULT_TEMPLATES }));
  B.on("TEMPLATES_SET", async (msg) => {
    const list = (Array.isArray(msg.templates) ? msg.templates : []).slice(0, 30).map((t, i) => ({
      id: String(t.id || "t" + i).slice(0, 40), name: String(t.name || "Template").slice(0, 60), pattern: String(t.pattern || "{title}").slice(0, 200) }));
    await sset(K.templates, list.length ? list : DEFAULT_TEMPLATES);
    return { ok: true };
  });

  // ------------------------------------------------------- tab + page context --
  async function tabInfo(tabId) {
    try { const t = await chrome.tabs.get(tabId); return { url: t.url || "", title: t.title || "" }; } catch (e) { return { url: "", title: "" }; }
  }
  async function allItems() {
    await B.mapReadyOrTimeout();
    const out = [];
    for (const [tabId, m] of B.videoMap) {
      if (!m.size) continue;
      const t = await tabInfo(tabId);
      for (const it of m.values()) out.push({ ...it, tabId, pageUrl: t.url, pageTitle: t.title });
    }
    return out;
  }

  // ----------------------------------------------------------- sniffed library --
  B.tabInfo = (id) => tabInfo(id);
  B.decodeName = (u) => decodeName(u);
  B.on("LIB_LIST", async (msg) => {
    const remembered = msg.scope === "remembered";   // M1.3: stored library instead of the live per-tab lists
    let rows;
    if (remembered) {
      const lib = await sget(K.library, {}); rows = [];
      for (const host of Object.keys(lib || {})) for (const e of lib[host] || []) rows.push({ url: e.url, filename: e.filename, title: e.title, size: e.size, contentType: e.contentType,
        timestamp: e.lastSeen, firstSeen: e.firstSeen, lastSeen: e.lastSeen, pageTitle: e.title, pageUrl: "", tabId: null });
    } else rows = await allItems();
    if (msg.tabId != null && msg.scope === "tab") rows = rows.filter((r) => r.tabId === msg.tabId);
    if (msg.kinds && msg.kinds.length) rows = rows.filter((r) => msg.kinds.includes(C.kindOf(r)));
    if (msg.hideSegments !== false) rows = rows.filter((r) => !/^(ts|m4s|m2ts|mts)$/.test(C.extOf(r.url)));
    rows = C.searchItems(rows, msg.q);
    rows = C.sortItems(rows, msg.sort || "time");
    const arch = await sget(K.archive, []);
    const seen = new Set(arch.map((a) => a.url));
    const out = rows.slice(0, msg.limit || 400).map((r) => {
      const ins = C.inspectItem(r);
      return { url: r.url, tabId: r.tabId, pageUrl: r.pageUrl, pageTitle: r.pageTitle, filename: r.filename || "",
        size: r.size || 0, sizeLabel: ins.sizeLabel, kind: ins.kind, ext: ins.ext, host: ins.host, resolutionHint: ins.resolutionHint,
        contentType: r.contentType || "", timestamp: r.timestamp || 0, alreadySent: seen.has(r.url),
        ...(remembered ? { remembered: true, lastSeen: r.lastSeen || 0, firstSeen: r.firstSeen || 0 } : {}),
        suggestedName: C.improveFilename({ filename: r.filename || decodeName(r.url), title: r.pageTitle, url: r.url, contentType: r.contentType, resolution: ins.resolutionHint }) };
    });
    return { ok: true, total: rows.length, items: out };
  });
  function decodeName(url) {
    try { return decodeURIComponent(new URL(url).pathname.split("/").pop() || ""); } catch (e) { return ""; }
  }

  // ----------------------------------------------------------- media inspector --
  async function fetchText(url, maxBytes) {
    const ctl = new AbortController(); const t = setTimeout(() => ctl.abort(), 8000);
    try {
      const res = await fetch(url, { signal: ctl.signal, credentials: "omit" });
      if (!res.ok) return { ok: false, error: "HTTP " + res.status };
      const txt = await res.text();
      return { ok: true, text: txt.slice(0, maxBytes || 400000) };
    } catch (e) { return { ok: false, error: String(e && e.message ? e.message : e) }; }
    finally { clearTimeout(t); }
  }
  async function headInfo(url) {
    const ctl = new AbortController(); const t = setTimeout(() => ctl.abort(), 6000);
    try {
      const res = await fetch(url, { method: "HEAD", signal: ctl.signal, credentials: "omit" });
      return { ok: res.ok, status: res.status, size: parseInt(res.headers.get("content-length") || "0", 10) || 0,
        contentType: res.headers.get("content-type") || "", acceptRanges: /bytes/i.test(res.headers.get("accept-ranges") || ""),
        lastModified: res.headers.get("last-modified") || "" };
    } catch (e) { return { ok: false, error: String(e && e.message ? e.message : e) }; }
    finally { clearTimeout(t); }
  }
  async function inspect(url, base) {
    const ins = C.inspectItem({ ...(base || {}), url });
    const out = { ok: true, inspect: ins, head: null, hls: null };
    if (ins.isManifest && ins.ext === "m3u8") {
      const r = await fetchText(url);
      if (r.ok) out.hls = C.analyzeM3U8(r.text); else out.hlsError = r.error;
    } else if (/^https?:/i.test(url)) {
      out.head = await headInfo(url);
      if (out.head.ok && out.head.size && !ins.size) { ins.size = out.head.size; ins.sizeLabel = C.sizeLabel(out.head.size); }
      if (out.head.ok && !out.head.acceptRanges && ins.kind === "video") ins.notes.push("Server doesn't advertise range requests: no segmented speedup.");
    }
    return out;
  }
  B.on("LIB_INSPECT", async (msg) => inspect(String(msg.url || ""), msg.item));

  // HLS plan: which URL to send, and whether merging is technically safe.
  async function hlsPlan(url, host) {
    const r = await fetchText(url);
    if (!r.ok) return { ok: false, safe: false, reason: "Couldn't read the playlist (" + r.error + ").", url };
    const a = C.analyzeM3U8(r.text);
    if (!a.valid) return { ok: false, safe: false, reason: a.reason, url };
    if (!a.safeToMerge) return { ok: true, safe: false, reason: a.reason, url, analysis: a };
    let chosen = null, sendUrl = url;
    if (a.master && a.variants.length) {
      const prefs = await sget(K.quality, {});
      const cap = C.qualityFor(prefs, host, B.cfg.defaultQuality);
      const pool = a.variants.filter((v) => v.height > 0);
      const within = cap > 0 ? pool.filter((v) => v.height <= cap) : pool;
      chosen = (within.length ? within : pool.slice(-1))[0] || a.variants[0];
      if (chosen && chosen.uri) { try { sendUrl = new URL(chosen.uri, url).toString(); } catch (e) { sendUrl = url; } }
    }
    return { ok: true, safe: true, reason: a.reason, url: sendUrl, masterUrl: url, chosen, analysis: a };
  }
  B.on("HLS_PLAN", async (msg) => hlsPlan(String(msg.url || ""), msg.host || C.hostOf(msg.url)));

  // ---------------------------------------------------------- smart one-click --
  // Decide filename + variant, skip duplicates (unless forced), send, and when
  // the app is offline fall back to a plain browser download for direct files.
  async function smartSend(msg) {
    const url = String(msg.url || "");
    if (!url) return { ok: false, error: "No usable link.", code: "bad_request" };
    if (/^(blob|data):/i.test(url)) return { ok: false, ...C.classifyError("blob"), hint: "Use the recording fallback on the page." };
    const host = C.hostOf(url) || C.hostOf(msg.pageUrl);
    const arch = await sget(K.archive, []);
    if (!msg.force && C.archiveHas(arch, url)) return { ok: false, duplicate: true, error: "Already sent before.", code: "duplicate", canForce: true };
    const ins = C.inspectItem({ url, contentType: msg.contentType, size: msg.size });
    let sendUrl = url, note = "";
    if (ins.isManifest && ins.ext === "m3u8" && msg.merge !== false) {
      const plan = await hlsPlan(url, host);
      if (plan.ok && !plan.safe) return { ok: false, error: plan.reason, code: "hls_unsafe", analysis: plan.analysis };
      if (plan.ok && plan.safe) { sendUrl = plan.url; if (plan.chosen && plan.chosen.height) note = plan.chosen.height + "p" + (plan.chosen.fps ? " " + Math.round(plan.chosen.fps) + "fps" : ""); }
    }
    let template = null;
    if (msg.templateId) template = ((await sget(K.templates, DEFAULT_TEMPLATES)).find((t) => t.id === msg.templateId) || {}).pattern;
    const base = msg.filename || decodeName(url);
    const filename = template
      ? C.expandTemplate(template, { title: msg.title, url, n: msg.n, resolution: ins.resolutionHint, ext: ins.isManifest ? "" : ins.ext })
      : C.improveFilename({ filename: base, title: msg.title, url, contentType: msg.contentType, resolution: ins.resolutionHint });
    const r = await B.sendToBackend({ url: sendUrl, pageUrl: msg.pageUrl || url, filename, format_id: msg.format_id, target_format: msg.target_format });
    if (r.ok) return { ok: true, job_id: r.job_id, duplicate_of: r.duplicate_of, filename, note, sentUrl: sendUrl };
    const cls = C.classifyError(r.error);
    if (cls.code === "app_offline" && msg.standalone !== false && B.cfg.standaloneFallback) {
      const s = await B.standaloneDownload({ url, filename: msg.filename ? filename : undefined });
      if (s.ok) { L.info("smart", "app offline, saved with the browser"); return { ok: true, standalone: true, filename, note: "Saved by the browser (app offline)." }; }
      return { ok: false, error: s.error || cls.message, code: cls.code, hint: cls.hint };
    }
    return { ok: false, error: r.error, code: cls.code, hint: cls.hint, retryable: cls.retryable };
  }
  B.on("SEND_SMART", (msg) => smartSend(msg));

  // ---- batch send with template numbering --------------------------------------
  B.on("SEND_MANY", async (msg) => {
    const items = Array.isArray(msg.items) ? msg.items.slice(0, 200) : [];
    const results = [];
    let n = Number(msg.startAt) || 1;
    for (const it of items) {
      const r = await smartSend({ ...it, pageUrl: it.pageUrl || msg.pageUrl, templateId: msg.templateId, n: n++, force: msg.force });
      results.push({ url: it.url, ...r });
      if (r.code === "app_offline" && !r.ok) break;     // don't hammer a dead app
    }
    return { ok: results.some((r) => r.ok), sent: results.filter((r) => r.ok).length, failed: results.filter((r) => !r.ok).length, results };
  });

  // ---------------------------------------------------------------- job control --
  const ACT = { pause: "/pause/", resume: "/resume/", stop: "/stop/", delete: "/delete/", "delete-file": "/delete-file/", redownload: "/redownload/", retry: "/redownload/" };
  B.on("JOBS_LIST", async () => {
    const r = await app("/jobs");
    if (!r.ok) return r;
    const jobs = Object.values(r.data || {}).map((j) => ({ id: j.id, status: j.status, filename: j.filename || "", url: j.url || "", progress: j.progress || 0,
      speed: j.speed || "", eta: j.eta || "", error: j.error || "", retry_count: j.retry_count || 0, size: j.total_bytes || j.size || 0, created: j.created_ts || 0, category: j.category || "" }));
    const rank = { downloading: 0, queued: 1, paused: 2, error: 3, stopped: 4, done: 5 };
    jobs.sort((a, b) => (rank[a.status] ?? 9) - (rank[b.status] ?? 9) || (b.created - a.created));
    const counts = {}; for (const j of jobs) counts[j.status] = (counts[j.status] || 0) + 1;
    let queue = null; const q = await app("/queue-status"); if (q.ok) queue = q.data;
    return { ok: true, jobs, counts, queue };
  });
  B.on("JOB_ACTION", async (msg) => {
    const id = String(msg.id || "");
    if (!id) return { ok: false, error: "No job id." };
    if (msg.action === "open-folder") return app("/open-folder", { method: "POST", body: { job_id: id } });
    if (msg.action === "reorder") return app("/reorder/" + encodeURIComponent(id), { method: "POST", body: { action: ["top", "up", "down", "bottom"].includes(msg.direction) ? msg.direction : "up" } });
    const base = ACT[msg.action];
    if (!base) return { ok: false, error: "Unknown action: " + msg.action };
    return app(base + encodeURIComponent(id), { method: "POST", body: {} });
  });
  B.on("JOBS_RETRY_FAILED", async () => {
    const r = await app("/jobs"); if (!r.ok) return r;
    const failed = Object.values(r.data || {}).filter((j) => j.status === "error");
    let ok = 0, bad = 0;
    for (const j of failed) { const x = await app("/redownload/" + encodeURIComponent(j.id), { method: "POST", body: {} }); if (x.ok) ok++; else bad++; }
    return { ok: bad === 0, retried: ok, failed: bad, total: failed.length };
  });
  B.on("JOBS_CLEAR_FAILED", async (msg) => app("/clear-failed", { method: "POST", body: msg.olderHours ? { older_than_hours: Number(msg.olderHours) } : {} }));
  B.on("QUEUE_SET", async (msg) => B.setQueueRunning(!!msg.running));

  // ------------------------------------------------------------------ schedules --
  const ALARM = "vg-schedule-tick";
  function ensureAlarm() {
    try { if (chrome.alarms) chrome.alarms.get(ALARM, (a) => { void chrome.runtime.lastError; if (!a) chrome.alarms.create(ALARM, { periodInMinutes: 1, delayInMinutes: 1 }); }); } catch (e) { L.warn("sched", "alarms unavailable", e); }
  }
  B.sendToBackendMany = async function (job) {
    const items = (job.items || []).map((it, i) => ({ ...it, pageUrl: job.pageUrl, n: i + 1 }));
    return B.dispatchMany({ items, pageUrl: job.pageUrl, templateId: job.templateId, force: true });
  };
  B.dispatchMany = async function (msg) {
    const results = []; let n = 1;
    for (const it of msg.items) { const r = await smartSend({ ...it, pageUrl: it.pageUrl || msg.pageUrl, templateId: msg.templateId, n: it.n || n++, force: msg.force, standalone: false }); results.push(r); if (r.code === "app_offline" && !r.ok) break; }
    return { ok: results.some((r) => r.ok), sent: results.filter((r) => r.ok).length, failed: results.filter((r) => !r.ok).length, offline: results.some((r) => r.code === "app_offline") };
  };
  async function runDueSchedules() {
    const now = Date.now(); const fired = [];
    // Nothing due (the usual case, including every worker start): don't touch storage.
    if (!(await sget(K.sched, [])).some((j) => C.isDue(j, now))) return fired;
    await update(K.sched, [], async (list) => {
      for (const j of list) {
        if (!C.isDue(j, now)) continue;
        // Past due by more than a day (browser was closed): run once, then move on.
        // Sweep (v4.5.1): one throwing send used to reject the whole update, so no job
        // was rescheduled and every one refired on the next tick.
        let r;
        try { r = await B.sendToBackendMany(j); } catch (e) { L.warn("sched", "send threw for " + j.name, e); r = { ok: false, sent: 0, failed: 1, offline: false }; }
        j.lastRun = now; j.lastResult = r.ok ? ("sent " + r.sent + (r.failed ? ", " + r.failed + " failed" : "")) : (r.offline ? "app offline, will retry" : "failed");
        if (!r.ok && r.offline && (j.tries = (j.tries || 0) + 1) <= 5) { j.nextAt = now + C.retryDelayMs(j.tries, 60000, 600000); }
        else { j.tries = 0; const nx = j.spec && (j.spec.daily || j.spec.every) ? C.nextRun(j.spec, now) : null; j.nextAt = nx; if (nx == null) j.enabled = false; }
        fired.push({ name: j.name, result: j.lastResult });
      }
      return list;
    });
    for (const f of fired) B.commandNotify("Scheduled \"" + f.name + "\": " + f.result);
    return fired;
  }
  B.runDueSchedules = runDueSchedules;
  B.on("SCHEDULE_ADD", async (msg) => {
    const items = (Array.isArray(msg.items) ? msg.items : []).filter((i) => i && /^https?:/i.test(i.url || "")).slice(0, 100);
    if (!items.length) return { ok: false, error: "Nothing to schedule." };
    const spec = msg.spec || {};
    // B11 (v4.5.1): panel.js checks this too, but a direct message or a state
    // restored after a clock change must not create a job that fires at once.
    if (Number.isFinite(spec.when) && spec.when <= Date.now()) return { ok: false, error: "Pick a time in the future." };
    const nextAt = C.nextRun(spec, Date.now());
    if (nextAt == null) return { ok: false, error: "That time isn't valid." };
    const job = { id: "s" + Date.now().toString(36) + Math.random().toString(36).slice(2, 5), name: String(msg.name || "Scheduled download").slice(0, 80),
      items: items.map((i) => ({ url: i.url, filename: i.filename || "", title: i.title || "" })), pageUrl: msg.pageUrl || "", templateId: msg.templateId || "",
      spec, nextAt, enabled: true, created: Date.now() };
    await update(K.sched, [], (l) => l.concat(job).slice(-100));
    ensureAlarm();
    return { ok: true, job };
  });
  B.on("SCHEDULE_LIST", async () => ({ ok: true, schedules: await sget(K.sched, []), now: Date.now() }));
  B.on("SCHEDULE_REMOVE", async (msg) => { await update(K.sched, [], (l) => l.filter((j) => j.id !== msg.id)); return { ok: true }; });
  B.on("SCHEDULE_TOGGLE", async (msg) => {
    await update(K.sched, [], (l) => { for (const j of l) if (j.id === msg.id) { j.enabled = !!msg.enabled; if (j.enabled && (j.nextAt == null || j.nextAt < Date.now()) && !(j.spec && j.spec.when)) j.nextAt = C.nextRun(j.spec, Date.now()); } return l; });
    return { ok: true };
  });
  B.on("SCHEDULE_RUN_NOW", async (msg) => {
    const list = await sget(K.sched, []); const j = list.find((x) => x.id === msg.id);
    if (!j) return { ok: false, error: "Unknown schedule." };
    const r = await B.sendToBackendMany(j);
    await update(K.sched, [], (l) => { for (const x of l) if (x.id === j.id) { x.lastRun = Date.now(); x.lastResult = r.ok ? "sent " + r.sent : "failed"; } return l; });
    return { ok: r.ok, ...r };
  });
  try {
    if (chrome.alarms) chrome.alarms.onAlarm.addListener((a) => { if (a.name === ALARM) runDueSchedules().catch((e) => L.error("sched", "tick failed", e)); });
  } catch (e) { /* no alarms API */ }
  ensureAlarm();
  runDueSchedules().catch(() => {});      // catch up after a browser or worker restart

  // ------------------------------------------------- watchlists + discovery alerts --
  B.on("WATCH_LIST", async () => ({ ok: true, rules: await sget(K.watch, []) }));
  B.on("WATCH_ADD", async (msg) => {
    const pattern = String(msg.pattern || "").trim().slice(0, 200);
    const kind = ["host", "text", "regex"].includes(msg.kind) ? msg.kind : "text";
    if (!pattern) return { ok: false, error: "Enter something to watch for." };
    if (kind === "regex" && C.regexLooksUnsafe(pattern)) return { ok: false, error: "That pattern could freeze matching. Simplify it (no repeated groups like (a+)+)." };
    if (kind === "regex") { try { new RegExp(pattern); } catch (e) { return { ok: false, error: "That pattern isn't a valid regular expression." }; } }
    const rule = { id: "w" + Date.now().toString(36), pattern, kind, action: msg.action === "send" ? "send" : "notify", enabled: true, created: Date.now() };
    await update(K.watch, [], (l) => l.concat(rule).slice(-60));
    return { ok: true, rule };
  });
  B.on("WATCH_REMOVE", async (msg) => { await update(K.watch, [], (l) => l.filter((r) => r.id !== msg.id)); return { ok: true }; });
  B.on("WATCH_TOGGLE", async (msg) => { await update(K.watch, [], (l) => { for (const r of l) if (r.id === msg.id) r.enabled = !!msg.enabled; return l; }); return { ok: true }; });

  const NOTE_COOLDOWN_MS = 30000, _lastNote = new Map(), _noteItems = new Map();
  function notifyItem(tabId, item, reason) {
    const now = Date.now(); const last = _lastNote.get(tabId) || 0;
    if (now - last < NOTE_COOLDOWN_MS) return;
    _lastNote.set(tabId, now);
    const id = "vg-found-" + tabId + "-" + now;
    _noteItems.set(id, { tabId, url: item.url });
    if (_noteItems.size > 30) _noteItems.delete(_noteItems.keys().next().value);
    try {
      chrome.notifications.create(id, { type: "basic", iconUrl: chrome.runtime.getURL("icons/icon128.png"), title: "Video Grabber found media",
        message: (reason ? reason + ": " : "") + (C.sanitizeName(decodeName(item.url), 70) || C.hostOf(item.url)), buttons: [{ title: "Download" }], requireInteraction: false });
    } catch (e) { L.debug("notify", "found-media notification failed", e); }
  }
  B.listen("item", async ({ tabId, item }) => {
    try {
      const ext = C.extOf(item.url);
      if (/^(ts|m4s|m2ts|mts)$/.test(ext)) return;
      const rules = await sget(K.watch, []);
      let page = ""; if (rules.length || B.cfg.notifyDiscovery) page = (await tabInfo(tabId)).url;
      const hits = rules.length ? C.watchMatches(rules, item, page) : [];
      for (const h of hits) {
        if (h.action === "send") {
          const r = await smartSend({ url: item.url, pageUrl: page, contentType: item.contentType, size: item.size, standalone: false });
          if (r.ok) B.commandNotify("Watch \"" + h.pattern + "\": sent " + (r.filename || "a file"));
        } else notifyItem(tabId, item, "Watch \"" + h.pattern + "\"");
      }
      if (!hits.length && B.cfg.notifyDiscovery) {
        const k = C.kindOf(item);
        if ((k === "video" || k === "stream" || k === "audio") && (item.size || 0) > 200 * 1024 || k === "stream") notifyItem(tabId, item, "New " + k);
      }
    } catch (e) { L.warn("watch", "item handler failed", e); }
  });
  // B1 (v4.5.1): a master playlist replaced this variant, so a notification that
  // still offers it would send a sub-stream. Drop it and its record.
  B.listen("removed", ({ tabId, item }) => {
    for (const [id, rec] of Array.from(_noteItems)) {
      if (rec.tabId === tabId && rec.url === item.url) {
        _noteItems.delete(id);
        try { chrome.notifications.clear(id); } catch (e) { /* notifications unavailable */ }
      }
    }
  });
  try {
    chrome.notifications.onButtonClicked.addListener((id, idx) => {
      const rec = _noteItems.get(id); if (!rec || idx !== 0) return;
      B.markGesture();
      tabInfo(rec.tabId).then((t) => smartSend({ url: rec.url, pageUrl: t.url, title: t.title }))
        .then((r) => B.commandNotify(r.ok ? "Sent to Video Grabber \u2713" : (r.error || "Couldn't send that.")));
      chrome.notifications.clear(id);
    });
    chrome.notifications.onClicked.addListener((id) => {
      if (!_noteItems.has(id)) return;
      if (chrome.action && chrome.action.openPopup) chrome.action.openPopup().catch(() => {});
      chrome.notifications.clear(id);
    });
  } catch (e) { /* notifications unavailable */ }

  // ------------------------------------------------------ context menus + shortcuts --
  self.VG_MENU_DEFS = [
    { id: "vg-link", title: "Download with Video Grabber", contexts: ["link", "video", "audio", "image"] },
    { id: "vg-page", title: "Send this page to Video Grabber", contexts: ["page"] },
    { id: "vg-best", title: "Download the largest media on this page", contexts: ["page"] },
    { id: "vg-all", title: "Send all media found on this page", contexts: ["page"] },
    { id: "vg-tonight", title: "Schedule this page for 23:00 tonight", contexts: ["page", "link"] },
    { id: "vg-watch", title: "Watch this site for new media", contexts: ["page"] },
  ];
  chrome.contextMenus.onClicked.addListener(async (info, tab) => {
    const id = info.menuItemId;
    if (!/^vg-(best|all|tonight|watch)$/.test(id)) return;
    B.markGesture();
    const t = tab && tab.id ? tab : await (async () => { try { return (await chrome.tabs.query({ active: true, lastFocusedWindow: true }))[0]; } catch (e) { return null; } })();
    if (!t) return;
    try {
      if (id === "vg-best") {
        await B.mapReadyOrTimeout(); const m = B.videoMap.get(t.id);
        const best = B.pickLargestItem(m ? Array.from(m.values()) : []);
        if (!best) return B.commandNotify("No media detected on this page yet. Play it first.");
        const r = await smartSend({ url: best.url, pageUrl: t.url, title: t.title, contentType: best.contentType, size: best.size, force: true });
        B.commandNotify(r.ok ? (r.standalone ? r.note : "Sent to Video Grabber \u2713") : (r.error || "Couldn't send that."));
      } else if (id === "vg-all") {
        const r = await sendAllOnTab(t);
        B.commandNotify(r.ok ? "Sent " + r.sent + " item" + (r.sent === 1 ? "" : "s") + (r.failed ? ", " + r.failed + " failed" : "") : (r.error || "Nothing to send."));
      } else if (id === "vg-tonight") {
        const url = info.linkUrl || t.url;
        const d = new Date(); d.setHours(23, 0, 0, 0); if (d.getTime() <= Date.now()) d.setDate(d.getDate() + 1);
        const r = await B.dispatchScheduleAdd({ name: "Tonight: " + (t.title || url).slice(0, 50), items: [{ url, title: t.title }], pageUrl: t.url, spec: { when: d.getTime() } });
        B.commandNotify(r.ok ? "Scheduled for " + d.toLocaleString() : (r.error || "Couldn't schedule that."));
      } else if (id === "vg-watch") {
        const host = C.hostOf(t.url); if (!host) return;
        const r = await B.dispatchWatchAdd({ kind: "host", pattern: host, action: "notify" });
        B.commandNotify(r.ok ? "Watching " + host : (r.error || "Couldn't add that."));
      }
    } catch (e) { L.error("menus", "handler failed", e); B.commandNotify("Action failed: " + String(e && e.message ? e.message : e)); }
  });
  B.dispatchScheduleAdd = (m) => callHandler("SCHEDULE_ADD", m);
  B.dispatchWatchAdd = (m) => callHandler("WATCH_ADD", m);
  function callHandler(type, msg) {
    return new Promise((resolve) => {
      const h = B.handlers[type]; if (!h) return resolve({ ok: false, error: "missing " + type });
      const r = h(msg, {}, resolve); if (r && typeof r.then === "function") r.then(resolve, (e) => resolve({ ok: false, error: String(e && e.message ? e.message : e) })); else if (r !== true) resolve({ ok: true });
    });
  }
  async function sendAllOnTab(t) {
    await B.mapReadyOrTimeout(); const m = B.videoMap.get(t.id);
    const items = (m ? Array.from(m.values()) : []).filter((it) => !/^(ts|m4s|m2ts|mts)$/.test(C.extOf(it.url)) && /^https?:/i.test(it.url))
      .filter((it) => ["video", "audio", "stream"].includes(C.kindOf(it)));
    if (!items.length) return { ok: false, error: "No media detected on this page yet." };
    return B.dispatchMany({ items: items.map((it) => ({ url: it.url, title: t.title, contentType: it.contentType, size: it.size })), pageUrl: t.url, force: false });
  }
  B.on("SEND_ALL_TAB", async (msg, sender) => { const id = msg.tabId != null ? msg.tabId : (sender && sender.tab && sender.tab.id); if (id == null) return { ok: false, error: "No tab." }; const t = await tabInfo(id); return sendAllOnTab({ id, ...t }); });

  chrome.commands.onCommand.addListener(async (command, tab) => {
    if (!/^(send-all-page|toggle-pill|start-queue)$/.test(command)) return;
    B.markGesture();
    try {
      const t = tab && tab.id ? tab : (await chrome.tabs.query({ active: true, lastFocusedWindow: true }))[0];
      if (command === "start-queue") { const r = await B.setQueueRunning(true); return B.commandNotify(r.ok ? "Queue started." : (r.error || "Couldn't start the queue.")); }
      if (!t) return;
      if (command === "toggle-pill") {
        const st = await chrome.storage.local.get(VG_SETTINGS_KEY); const cur = vgNormalize(st[VG_SETTINGS_KEY]);
        const next = { ...cur, pillVisible: !cur.pillVisible };
        await chrome.storage.local.set({ [VG_SETTINGS_KEY]: next });
        return B.commandNotify(next.pillVisible ? "Pill shown." : "Pill hidden.");
      }
      if (command === "send-all-page") {
        const r = await sendAllOnTab(t);
        B.commandNotify(r.ok ? "Sent " + r.sent + " item" + (r.sent === 1 ? "" : "s") : (r.error || "Nothing to send."));
      }
    } catch (e) { L.error("commands", command + " failed", e); B.commandNotify("Shortcut failed: " + String(e && e.message ? e.message : e)); }
  });

  // --------------------------------------------------------- settings export/import --
  B.on("SETTINGS_EXPORT", async () => {
    const store = await chrome.storage.local.get(C.EXPORT_KEYS);
    return { ok: true, text: C.exportBundle(store), name: "videograbber-settings-" + new Date().toISOString().slice(0, 10) + ".json" };
  });
  B.on("SETTINGS_IMPORT", async (msg) => {
    const r = C.parseImport(String(msg.text || ""));
    if (!r.ok) return r;
    const out = {};
    if (r.data.vgSettings !== undefined) out.vgSettings = vgNormalize(r.data.vgSettings);
    if (r.data.vgHostQuality && typeof r.data.vgHostQuality === "object") { let p = {}; for (const h of Object.keys(r.data.vgHostQuality)) p = C.setQuality(p, h, r.data.vgHostQuality[h]); out.vgHostQuality = p; }
    if (Array.isArray(r.data.vgTemplates)) out.vgTemplates = r.data.vgTemplates.slice(0, 30).map((t, i) => ({ id: String(t.id || "t" + i).slice(0, 40), name: String(t.name || "Template").slice(0, 60), pattern: String(t.pattern || "{title}").slice(0, 200) }));
    if (Array.isArray(r.data.vgWatchlist)) out.vgWatchlist = r.data.vgWatchlist.slice(0, 60).filter((w) => w && w.pattern).map((w) => ({ id: String(w.id || "w" + Math.random().toString(36).slice(2, 8)), pattern: String(w.pattern).slice(0, 200), kind: ["host", "text", "regex"].includes(w.kind) ? w.kind : "text", action: w.action === "send" ? "send" : "notify", enabled: w.enabled !== false }));
    // Sweep: a one-off whose time has passed is kept but switched off, not fired on import.
    const pastOnce = (j) => Number.isFinite(j.spec.when) && j.spec.when <= Date.now();
    if (Array.isArray(r.data.vgSchedules)) out.vgSchedules = r.data.vgSchedules.slice(0, 100).filter((j) => j && Array.isArray(j.items) && j.spec).map((j) => ({ ...j, nextAt: pastOnce(j) ? null : C.nextRun(j.spec, Date.now()), enabled: pastOnce(j) ? false : j.enabled, items: j.items.filter((i) => i && /^https?:/i.test(i.url || "")).slice(0, 100) }));
    if (Array.isArray(r.data.disabledSites)) out.disabledSites = r.data.disabledSites.filter((h) => typeof h === "string").slice(0, 500);
    if (r.data.pillPrefs && typeof r.data.pillPrefs === "object") out.pillPrefs = r.data.pillPrefs;
    // Keys that other code updates read-modify-write go through the same
    // per-key lock, so an import can't be overwritten by a schedule tick or an
    // archive write that was already in flight (found by the import test).
    const locked = new Set(Object.values(K));
    for (const key of Object.keys(out)) {
      if (locked.has(key)) await update(key, Array.isArray(out[key]) ? [] : {}, () => out[key]);
      else await sset(key, out[key]);
    }
    return { ok: true, applied: Object.keys(out), warnings: r.warnings };
  });

  L.info("features", "bg-features ready");
})();
