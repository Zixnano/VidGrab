// vgcore.js: pure helpers with no Chrome API calls, so they run unchanged in the
// service worker (importScripts), extension pages (<script>) and Node tests.
// Everything hangs off one global, VGCORE. Nothing here touches storage or the
// network; callers pass data in and get data out.
(function (root) {
  "use strict";

  // ---------------------------------------------------------------- sizes ---
  function sizeLabel(bytes) {
    const n = Number(bytes);
    if (!Number.isFinite(n) || n <= 0) return "";
    const u = ["B", "KB", "MB", "GB", "TB"];
    let i = 0, v = n;
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    return (i === 0 ? v.toFixed(0) : v.toFixed(v >= 100 ? 0 : 1)) + " " + u[i];
  }

  function hostOf(url) {
    try { return new URL(url).hostname.replace(/^www\./, "").toLowerCase(); } catch (e) { return ""; }
  }

  // ---------------------------------------------------------- file names ---
  const ILLEGAL = /[<>:"\/\\|?*\u0000-\u001f]/g;
  const RESERVED = /^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?$/i;
  const NOISE_NAME = /^(index|master|playlist|chunklist|manifest|video|audio|media|stream|download|file|dash_\d+|m2-res_\d+p?)$/i;

  function extOf(nameOrUrl) {
    try {
      const p = /^[a-z]+:\/\//i.test(nameOrUrl) ? new URL(nameOrUrl).pathname : String(nameOrUrl || "");
      const m = /\.([a-z0-9]{1,5})$/i.exec(p.split("/").pop());
      return m ? m[1].toLowerCase() : "";
    } catch (e) { return ""; }
  }

  // A name that is mostly a random ID (CDN chunk names) says nothing useful.
  function looksRandom(stem) {
    if (!stem) return true;
    if (NOISE_NAME.test(stem)) return true;
    if (stem.length >= 16 && !/[\s_-]/.test(stem) && /\d/.test(stem) && /[a-z]/i.test(stem)) return true;
    if (/^[0-9a-f]{16,}$/i.test(stem)) return true;
    return false;
  }

  function sanitizeName(name, maxLen) {
    let s = String(name == null ? "" : name).normalize("NFC");
    s = s.replace(ILLEGAL, " ").replace(/\s+/g, " ").trim();
    s = s.replace(/^[.\s]+/, "").replace(/[.\s]+$/, "");
    if (RESERVED.test(s)) s = "_" + s;
    const limit = maxLen || 120;
    if (s.length > limit) {
      const e = extOf(s);
      const keep = e ? "." + e : "";
      s = s.slice(0, limit - keep.length).trim() + keep;
    }
    return s;
  }

  // Build the best file name we can from what the extension knows.
  // ctx: {filename, title, url, contentType, index, date, resolution, host}
  function improveFilename(ctx) {
    ctx = ctx || {};
    const urlExt = extOf(ctx.url || "");
    let name = sanitizeName(ctx.filename || "");
    // A playlist's own extension is never the saved file's extension.
    const nameExt = extOf(name);
    let ext = /^(m3u8|mpd)$/.test(nameExt) ? "" : (nameExt || (/^(m3u8|mpd)$/.test(urlExt) ? "" : urlExt));
    if (!ext && ctx.contentType) {
      const ct = String(ctx.contentType).toLowerCase();
      const map = { "video/mp4": "mp4", "video/webm": "webm", "audio/mpeg": "mp3", "audio/mp4": "m4a",
        "audio/aac": "aac", "audio/ogg": "ogg", "image/jpeg": "jpg", "image/png": "png",
        "image/gif": "gif", "image/webp": "webp", "image/avif": "avif", "application/pdf": "pdf" };
      for (const k of Object.keys(map)) if (ct.startsWith(k)) { ext = map[k]; break; }
    }
    const stem = name ? name.replace(/\.[a-z0-9]{1,5}$/i, "") : "";
    let base = stem;
    if (looksRandom(stem)) {
      const t = sanitizeName(ctx.title || "", 100);
      base = t || stem || sanitizeName(ctx.host || hostOf(ctx.url || ""), 40) || "download";
      if (ctx.resolution && !/\d{3,4}p/.test(base)) base += " " + ctx.resolution;
    }
    if (ctx.index != null && ctx.index !== "") base += " " + String(ctx.index).padStart(2, "0");
    base = sanitizeName(base, 110);
    if (!base) base = "download";
    return ext ? base + "." + ext : base;
  }

  // Batch template: {title} {host} {date} {time} {n} {nn} {res} {ext}.
  function expandTemplate(tpl, ctx) {
    ctx = ctx || {};
    const d = ctx.now instanceof Date ? ctx.now : new Date();
    const p2 = (x) => String(x).padStart(2, "0");
    const vals = {
      title: ctx.title || "", host: ctx.host || hostOf(ctx.url || ""),
      date: d.getFullYear() + "-" + p2(d.getMonth() + 1) + "-" + p2(d.getDate()),
      time: p2(d.getHours()) + p2(d.getMinutes()),
      n: ctx.n == null ? "" : String(ctx.n), nn: ctx.n == null ? "" : p2(ctx.n),
      res: ctx.resolution || "", ext: ctx.ext || extOf(ctx.url || ""),
    };
    let out = String(tpl || "{title}").replace(/\{(title|host|date|time|n|nn|res|ext)\}/g, (_, k) => vals[k]);
    out = sanitizeName(out.replace(/\s*\.\s*$/, ""));
    if (vals.ext && !/\.[a-z0-9]{1,5}$/i.test(out) && tpl && !/\{ext\}/.test(tpl)) out += "." + vals.ext;
    return out || "download";
  }

  // ------------------------------------------------------------ inspector ---
  const KIND_BY_EXT = {
    video: "mp4 m4v mov webm mkv avi flv wmv mpg mpeg ts m2ts ogv 3gp",
    audio: "mp3 m4a aac wav flac opus ogg wma aiff",
    image: "jpg jpeg png gif webp avif heic bmp svg ico tif tiff jxl",
    stream: "m3u8 mpd",
    document: "pdf epub doc docx xls xlsx ppt pptx txt csv json xml",
    archive: "zip rar 7z tar gz tgz bz2 xz iso",
    app: "exe msi dmg deb rpm apk",
  };
  const EXT_KIND = {};
  for (const k of Object.keys(KIND_BY_EXT)) for (const e of KIND_BY_EXT[k].split(" ")) EXT_KIND[e] = k;

  function kindOf(item) {
    const ct = String((item && item.contentType) || "").toLowerCase();
    if (/mpegurl|dash\+xml/.test(ct)) return "stream";
    if (ct.startsWith("video/")) return "video";
    if (ct.startsWith("audio/")) return "audio";
    if (ct.startsWith("image/")) return "image";
    const e = extOf((item && item.url) || "");
    return EXT_KIND[e] || (ct ? "other" : "unknown");
  }

  // Everything we can say about an item without touching the network.
  function inspectItem(item) {
    item = item || {};
    const url = item.url || "";
    let u = null;
    try { u = new URL(url); } catch (e) {}
    const kind = kindOf(item);
    const ext = extOf(url);
    const out = {
      url, kind, ext, host: u ? u.hostname : "", protocol: u ? u.protocol.replace(":", "") : "",
      contentType: item.contentType || "", size: item.size || 0, sizeLabel: sizeLabel(item.size),
      isManifest: kind === "stream", isSegment: /^(ts|m4s|m2ts|mts)$/.test(ext),
      resolutionHint: "", fpsHint: 0, warnings: [], notes: [],
    };
    const r = /(?:^|[^0-9])(2160|1440|1080|720|576|480|360|240|144)p?(?:[^0-9]|$)/.exec(u ? u.pathname : url);
    if (r) out.resolutionHint = r[1] + "p";
    const f = /(?:^|[^0-9])(24|25|30|48|50|60|120)\s?fps/i.exec(url);
    if (f) out.fpsHint = Number(f[1]);
    if (u) {
      if (/[?&](token|sig|signature|expires?|exp|hdnts|policy|key-pair-id)=/i.test(u.search)) {
        out.warnings.push("Signed URL: it can expire, send it soon.");
      }
      if (u.protocol === "blob:") out.warnings.push("Blob URL: only the page can read it. Use recording.");
    }
    if (out.isSegment) out.notes.push("Stream segment: grab the playlist instead.");
    if (out.isManifest) out.notes.push(ext === "mpd" ? "DASH manifest." : "HLS playlist.");
    if (out.size && out.size < 20 * 1024 && (kind === "video" || kind === "audio")) {
      out.warnings.push("Very small for " + kind + ": probably a fragment or a probe.");
    }
    return out;
  }

  // ------------------------------------------------------------ HLS parse ---
  // Parse an m3u8 text. Safe-merge verdict: only unencrypted (or plain AES-128),
  // finished (ENDLIST) streams are marked safe. DRM or live streams are never.
  function analyzeM3U8(text) {
    const lines = String(text || "").split(/\r?\n/).map((l) => l.trim());
    const res = { valid: lines[0] === "#EXTM3U", master: false, variants: [], live: false,
      encrypted: false, drm: false, fmp4: false, segments: 0, duration: 0,
      safeToMerge: false, reason: "" };
    if (!res.valid) { res.reason = "Not an HLS playlist."; return res; }
    let pendingInf = 0;
    for (let i = 0; i < lines.length; i++) {
      const l = lines[i];
      if (l.startsWith("#EXT-X-STREAM-INF:")) {
        res.master = true;
        const attr = l.slice(18);
        const bw = /BANDWIDTH=(\d+)/.exec(attr);
        const rs = /RESOLUTION=(\d+)x(\d+)/.exec(attr);
        const fr = /FRAME-RATE=([\d.]+)/.exec(attr);
        const cd = /CODECS="([^"]+)"/.exec(attr);
        let uri = "";
        for (let j = i + 1; j < lines.length; j++) { if (lines[j] && !lines[j].startsWith("#")) { uri = lines[j]; break; } }
        res.variants.push({ bandwidth: bw ? Number(bw[1]) : 0, width: rs ? Number(rs[1]) : 0,
          height: rs ? Number(rs[2]) : 0, fps: fr ? Math.round(Number(fr[1]) * 100) / 100 : 0,
          codecs: cd ? cd[1] : "", uri });
      } else if (l.startsWith("#EXT-X-KEY:")) {
        if (!/METHOD=NONE/.test(l)) {
          res.encrypted = true;
          if (!/METHOD=AES-128/.test(l)) res.drm = true;
          if (/KEYFORMAT="(?!identity)/.test(l)) res.drm = true;
        }
      } else if (l.startsWith("#EXT-X-SESSION-KEY:") && !/METHOD=NONE/.test(l)) {
        res.encrypted = true; if (!/METHOD=AES-128/.test(l)) res.drm = true;
      } else if (l.startsWith("#EXT-X-MAP:")) res.fmp4 = true;
      else if (l.startsWith("#EXTINF:")) { pendingInf = parseFloat(l.slice(8)) || 0; res.segments++; res.duration += pendingInf; }
    }
    res.variants.sort((a, b) => (b.height - a.height) || (b.bandwidth - a.bandwidth));
    if (!res.master) res.live = !lines.includes("#EXT-X-ENDLIST") && res.segments > 0;
    if (res.master) { res.safeToMerge = !res.drm; res.reason = res.drm ? "DRM protected." : ""; }
    else if (res.drm) res.reason = "DRM protected: cannot be merged.";
    else if (res.live) res.reason = "Live stream: use recording, not a merge.";
    else if (res.segments === 0) res.reason = "No segments listed.";
    else { res.safeToMerge = true; res.reason = res.encrypted ? "AES-128 stream: the app decrypts it." : ""; }
    return res;
  }

  // ------------------------------------------------- search, sort, filter ---
  function itemText(it) {
    const ins = inspectItem(it);
    return [it.filename, it.title, it.url, it.contentType, ins.host, ins.kind, ins.ext].join(" ").toLowerCase();
  }
  function searchItems(items, q) {
    const terms = String(q || "").toLowerCase().split(/\s+/).filter(Boolean);
    if (!terms.length) return items.slice();
    return items.filter((it) => { const t = itemText(it); return terms.every((w) => t.includes(w)); });
  }
  function sortItems(items, mode) {
    const a = items.slice();
    const nameOf = (it) => (it.filename || it.url || "").toLowerCase();
    if (mode === "size") a.sort((x, y) => (y.size || 0) - (x.size || 0));
    else if (mode === "name") a.sort((x, y) => nameOf(x).localeCompare(nameOf(y)));
    else if (mode === "host") a.sort((x, y) => hostOf(x.url).localeCompare(hostOf(y.url)) || (y.timestamp || 0) - (x.timestamp || 0));
    else if (mode === "kind") a.sort((x, y) => kindOf(x).localeCompare(kindOf(y)) || (y.timestamp || 0) - (x.timestamp || 0));
    else a.sort((x, y) => (y.timestamp || 0) - (x.timestamp || 0));
    return a;
  }

  // -------------------------------------------------- per-host quality ------
  function qualityFor(prefs, host, fallback) {
    const h = String(host || "").replace(/^www\./, "").toLowerCase();
    const p = prefs && typeof prefs === "object" ? prefs : {};
    if (h && Number.isFinite(p[h])) return p[h];
    // parent domain match: "cdn.example.com" falls back to "example.com"
    const parts = h.split(".");
    for (let i = 1; i < parts.length - 1; i++) {
      const parent = parts.slice(i).join(".");
      if (Number.isFinite(p[parent])) return p[parent];
    }
    return Number.isFinite(fallback) ? fallback : 0;
  }
  function setQuality(prefs, host, height) {
    const out = Object.assign({}, prefs || {});
    const h = String(host || "").replace(/^www\./, "").toLowerCase();
    if (!h) return out;
    const n = Math.round(Number(height));
    if (!Number.isFinite(n) || n <= 0) delete out[h]; else out[h] = Math.min(4320, n);
    const keys = Object.keys(out);
    if (keys.length > 300) for (const k of keys.slice(0, keys.length - 300)) delete out[k];
    return out;
  }
  // formats: [{format_id, resolution:"1920x1080"|"1080p", fps, vcodec, ...}]
  function heightOfFormat(f) {
    const r = String((f && f.resolution) || "");
    let m = /(\d+)x(\d+)/.exec(r); if (m) return Number(m[2]);
    m = /(\d{3,4})p/.exec(r); if (m) return Number(m[1]);
    m = /^(\d{3,4})$/.exec(r); return m ? Number(m[1]) : 0;
  }
  function pickFormat(formats, maxHeight) {
    const vids = (formats || []).filter((f) => f && f.vcodec !== "none" && heightOfFormat(f) > 0);
    if (!vids.length) return null;
    const lim = maxHeight > 0 ? maxHeight : Infinity;
    const ok = vids.filter((f) => heightOfFormat(f) <= lim);
    const pool = ok.length ? ok : vids.slice().sort((a, b) => heightOfFormat(a) - heightOfFormat(b)).slice(0, 1);
    return pool.slice().sort((a, b) => heightOfFormat(b) - heightOfFormat(a) || (b.fps || 0) - (a.fps || 0))[0];
  }
  function fpsLabel(f) {
    const fps = Number(f && f.fps);
    return Number.isFinite(fps) && fps > 0 ? (Math.round(fps) + " fps") : "";
  }

  // ---------------------------------------------------------- errors --------
  // One place that turns any failure into something the UI can show.
  function classifyError(err) {
    const raw = typeof err === "string" ? err : (err && (err.error || err.message)) || String(err || "");
    const s = raw.toLowerCase();
    const mk = (code, message, hint, retryable) => ({ code, message, hint: hint || "", retryable: !!retryable, raw });
    if (/isn'?t running|not running|failed to fetch|networkerror|load failed|econnrefused/.test(s)) {
      return mk("app_offline", "Video Grabber app isn't running.", "Launch the desktop app, then retry.", true);
    }
    if (/not paired|\b401\b|unauthor/.test(s)) {
      return mk("not_paired", "Not paired with the app.", "Open Options and press Test connection.", false);
    }
    if (/abort|timed? ?out|timeout/.test(s)) {
      return mk("timeout", "The app took too long to answer.", "It may be busy. Retry in a moment.", true);
    }
    if (/\b403\b|forbidden/.test(s)) return mk("forbidden", "The site refused the request.", "Open the page again and retry.", false);
    if (/\b404\b|not found|\b410\b/.test(s)) return mk("gone", "That media is no longer available.", "", false);
    if (/\b429\b|too many/.test(s)) return mk("rate_limited", "The site is rate limiting requests.", "Wait a minute, then retry.", true);
    if (/\b5\d\d\b|server error/.test(s)) return mk("server", "The server had a problem.", "Retry later.", true);
    if (/blob|in memory/.test(s)) return mk("blob", "The page built this in memory.", "Use the recording fallback.", false);
    if (/missing url|no url/.test(s)) return mk("bad_request", "No usable link.", "", false);
    return mk("unknown", raw || "Something went wrong.", "", false);
  }
  function retryDelayMs(attempt, base, cap) {
    const b = base || 2000, c = cap || 120000;
    return Math.min(c, b * Math.pow(2, Math.max(0, attempt)));
  }

  // ----------------------------------------------------------- archive ------
  const ARCHIVE_MAX = 1000;
  function archiveAdd(list, entry, max) {
    const out = Array.isArray(list) ? list.slice() : [];
    const key = (e) => (e.url || "") + "|" + (e.format_id || "");
    const k = key(entry);
    const i = out.findIndex((e) => key(e) === k);
    const rec = Object.assign({ at: Date.now() }, entry);
    if (i >= 0) { rec.count = (out[i].count || 1) + 1; rec.firstAt = out[i].firstAt || out[i].at; out.splice(i, 1); }
    else { rec.count = 1; rec.firstAt = rec.at; }
    out.unshift(rec);
    const m = max || ARCHIVE_MAX;
    return out.length > m ? out.slice(0, m) : out;
  }
  function archiveHas(list, url) { return (list || []).some((e) => e.url === url); }
  function archiveSearch(list, q, opts) {
    opts = opts || {};
    const terms = String(q || "").toLowerCase().split(/\s+/).filter(Boolean);
    return (list || []).filter((e) => {
      if (opts.status && e.status !== opts.status) return false;
      if (opts.host && hostOf(e.url) !== opts.host) return false;
      if (!terms.length) return true;
      const t = [e.filename, e.title, e.url, e.pageUrl, e.status].join(" ").toLowerCase();
      return terms.every((w) => t.includes(w));
    });
  }

  // --------------------------------------------------------- scheduling -----
  // spec: {when: ms epoch}  one-off
  //   or  {daily: "HH:MM"}  every day at local time
  //   or  {every: minutes}  repeating interval, anchored at spec.anchor
  function nextRun(spec, now) {
    now = now || Date.now();
    if (!spec) return null;
    if (Number.isFinite(spec.when)) return spec.when;
    if (typeof spec.daily === "string") {
      const m = /^(\d{1,2}):(\d{2})$/.exec(spec.daily);
      if (!m || +m[1] > 23 || +m[2] > 59) return null;
      const d = new Date(now); d.setHours(+m[1], +m[2], 0, 0);
      if (d.getTime() <= now) d.setDate(d.getDate() + 1);
      return d.getTime();
    }
    if (Number.isFinite(spec.every) && spec.every >= 1) {
      const step = spec.every * 60000, anchor = Number.isFinite(spec.anchor) ? spec.anchor : now;
      if (now < anchor) return anchor;
      return anchor + (Math.floor((now - anchor) / step) + 1) * step;
    }
    return null;
  }
  // Does the schedule have a run that should have fired but didn't (worker asleep)?
  function isDue(job, now) {
    now = now || Date.now();
    return !!job && job.enabled !== false && Number.isFinite(job.nextAt) && job.nextAt <= now;
  }

  // ---------------------------------------------------------- watchlist -----
  // rule: {id, pattern, kind: "host"|"text"|"regex", action: "notify"|"send", enabled}
  // Sweep (v4.5.1): a group that holds a quantifier and is itself quantified,
  // such as (a+)+ or (x*)*, can backtrack catastrophically. Such patterns are
  // refused when added and never executed.
  function regexLooksUnsafe(p) {
    return /\((?:[^()\\]|\\.)*[+*](?:[^()\\]|\\.)*\)\s*(?:[+*]|\{\d*,?\d*\})/.test(String(p || ""));
  }
  function watchMatches(rules, item, pageUrl) {
    const hay = (String(item && item.url || "") + " " + String(pageUrl || "") + " " + String(item && item.filename || "")).toLowerCase();
    const out = [];
    for (const r of rules || []) {
      if (!r || r.enabled === false || !r.pattern) continue;
      const p = String(r.pattern).toLowerCase().replace(/^www\./, "");
      let hit = false;
      try {
        if (r.kind === "host") { const h = hostOf(item && item.url) || hostOf(pageUrl); hit = !!h && (h === p || h.endsWith("." + p)); }
        else if (r.kind === "regex") hit = !regexLooksUnsafe(r.pattern) && new RegExp(r.pattern, "i").test(hay);
        else hit = hay.includes(p);
      } catch (e) { hit = false; }
      if (hit) out.push(r);
    }
    return out;
  }

  // ------------------------------------------------ remembered library (M1) --
  // lib: { [host]: [{url, filename, title, size, contentType, firstSeen, lastSeen}] }
  const LIB_MAX = 200, LIB_MAX_AGE_MS = 30 * 86400000;
  function libraryMerge(lib, entries, now, max) {
    now = now || Date.now(); max = max || LIB_MAX;
    const out = {};
    for (const h of Object.keys(lib && typeof lib === "object" ? lib : {})) if (Array.isArray(lib[h])) out[h] = lib[h].map((e) => ({ ...e }));
    for (const en of entries || []) {
      if (!en || !en.url) continue;
      const host = hostOf(en.url); if (!host) continue;
      const list = out[host] = out[host] || [];
      const cur = list.find((e) => e.url === en.url);
      if (cur) {
        cur.lastSeen = now;
        if (en.size && en.size > (cur.size || 0)) cur.size = en.size;
        if (en.contentType && !cur.contentType) cur.contentType = en.contentType;
        if (en.title) cur.title = en.title;
      } else list.push({ url: en.url, filename: en.filename || "", title: en.title || "", size: en.size || 0, contentType: en.contentType || "", firstSeen: now, lastSeen: now });
    }
    let all = [];
    for (const h of Object.keys(out)) {
      out[h] = out[h].filter((e) => Number.isFinite(e.lastSeen) && now - e.lastSeen <= LIB_MAX_AGE_MS);
      if (!out[h].length) delete out[h]; else for (const e of out[h]) all.push([h, e]);
    }
    if (all.length > max) {
      all.sort((a, b) => a[1].lastSeen - b[1].lastSeen);
      for (const [h, e] of all.slice(0, all.length - max)) out[h] = out[h].filter((x) => x !== e);
      for (const h of Object.keys(out)) if (!out[h].length) delete out[h];
    }
    return out;
  }
  function libraryForget(lib, spec) {
    const out = {};
    for (const h of Object.keys(lib || {})) {
      if (spec.host && h === spec.host) continue;
      const keep = (lib[h] || []).filter((e) => !(spec.url && e.url === spec.url));
      if (keep.length) out[h] = keep;
    }
    return out;
  }
  function relTime(ms, now) {
    const s = Math.max(0, Math.round(((now || Date.now()) - ms) / 1000));
    if (s < 45) return "just now";
    const u = [[60, "minute"], [3600, "hour"], [86400, "day"], [2592000, "month"]];
    let div = 1, name = "second";
    for (const [lim, n] of [[60, "minute"], [3600, "hour"], [86400, "day"], [2592000, "month"]]) { if (s >= lim) { div = lim; name = n; } }
    const n = Math.floor(s / div);
    return n + " " + name + (n === 1 ? "" : "s") + " ago";
  }

  // ------------------------------------------------- settings export/import -
  const EXPORT_MAGIC = "videograbber-extension-settings";
  const EXPORT_VERSION = 2;
  const EXPORT_KEYS = ["vgSettings", "vgHostQuality", "vgTemplates", "vgWatchlist", "vgSchedules", "disabledSites", "pillPrefs"];
  function exportBundle(store, now) {
    const data = {};
    for (const k of EXPORT_KEYS) if (store && store[k] !== undefined) data[k] = store[k];
    return JSON.stringify({ magic: EXPORT_MAGIC, version: EXPORT_VERSION, exportedAt: now || Date.now(), data }, null, 2);
  }
  // Returns {ok, data, error, warnings}. Never throws, never accepts unknown keys,
  // never lets an import carry the API token or cookies.
  function parseImport(text) {
    let j;
    try { j = JSON.parse(text); } catch (e) { return { ok: false, error: "That file isn't valid JSON." }; }
    if (!j || j.magic !== EXPORT_MAGIC) return { ok: false, error: "That isn't a Video Grabber settings file." };
    if (!Number.isFinite(j.version) || j.version > EXPORT_VERSION) {
      return { ok: false, error: "That file comes from a newer version of the extension." };
    }
    const data = {}, warnings = [];
    for (const k of Object.keys(j.data || {})) {
      if (!EXPORT_KEYS.includes(k)) { warnings.push("Ignored unknown key: " + k); continue; }
      data[k] = j.data[k];
    }
    return { ok: true, data, warnings, version: j.version };
  }

  root.VGCORE = {
    sizeLabel, hostOf, extOf, sanitizeName, looksRandom, improveFilename, expandTemplate,
    kindOf, inspectItem, analyzeM3U8, searchItems, sortItems, qualityFor, setQuality,
    heightOfFormat, pickFormat, fpsLabel, classifyError, retryDelayMs,
    archiveAdd, archiveHas, archiveSearch, nextRun, isDue, watchMatches, regexLooksUnsafe,
    libraryMerge, libraryForget, relTime, LIB_MAX, LIB_MAX_AGE_MS,
    exportBundle, parseImport, EXPORT_KEYS, EXPORT_VERSION, ARCHIVE_MAX,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = root.VGCORE;
})(typeof self !== "undefined" ? self : (typeof globalThis !== "undefined" ? globalThis : this));
