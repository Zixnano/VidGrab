// panel.js: the views added around the original popup (popup.js stays the
// "Page" view): Library, Queue, Archive and Tools. Used by popup.html and
// sidepanel.html. Every value from the page or the app is put in with
// textContent, never innerHTML.
(function () {
  "use strict";
  const C = window.VGCORE, X = window.VGCHROME, LOG = window.VGLOG;
  if (!C || !X) return;
  LOG.setContext(document.documentElement.classList.contains("side") ? "panel" : "popup");
  const isSide = document.documentElement.classList.contains("side");
  const $ = (id) => document.getElementById(id);

  function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    for (const k of Object.keys(attrs || {})) {
      const v = attrs[k];
      if (typeof v === "boolean" && k.startsWith("aria-")) { el.setAttribute(k, String(v)); continue; }   // aria-* needs "true"/"false", not presence
      if (v == null || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "text") el.textContent = v;
      else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
      else if (v === true) el.setAttribute(k, "");
      else el.setAttribute(k, String(v));
    }
    for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
    return el;
  }
  const send = (msg, o) => X.send(msg, o);
  let toastTimer = null;
  function toast(msg, bad) {
    const t = $("toast"); if (!t) return;
    t.textContent = msg; t.classList.toggle("err", !!bad); t.classList.add("show");
    clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.remove("show"), bad ? 4500 : 2200);
  }
  function errText(r) { const c = C.classifyError(r && r.error); return (r && r.error) || c.message; }
  function fail(r) { const c = C.classifyError(r && (r.error || r)); toast((r && r.error ? r.error : c.message) + (c.hint ? " " + c.hint : ""), true); }

  // ---------------------------------------------------------- shell + nav ---
  const VIEWS = [["page", "Page"], ["library", "Library"], ["queue", "Queue"], ["archive", "Archive"], ["tools", "Tools"]];
  const nav = h("div", { id: "vgnav", role: "tablist", "aria-label": "Video Grabber sections" });
  const mount = h("div", { id: "vgviews" });
  const hdr = document.querySelector(".hdr");
  hdr.after(nav); nav.after(mount);
  const panes = {};
  for (const [id, label] of VIEWS) {
    const b = h("button", { id: "nav-" + id, role: "tab", "aria-selected": id === "page", "aria-controls": "pane-" + id, tabindex: id === "page" ? 0 : -1, text: label,
      onclick: () => show(id) });
    nav.append(b);
    if (id !== "page") { panes[id] = h("section", { id: "pane-" + id, class: "pview", role: "tabpanel", "aria-labelledby": "nav-" + id }); mount.append(panes[id]); }
  }
  nav.addEventListener("keydown", (e) => {
    const ids = VIEWS.map((v) => v[0]); let i = ids.indexOf(current);
    if (e.key === "ArrowRight") i = (i + 1) % ids.length; else if (e.key === "ArrowLeft") i = (i + ids.length - 1) % ids.length; else return;
    e.preventDefault(); show(ids[i]); $("nav-" + ids[i]).focus();
  });
  let current = "page", pollTimer = null;
  const loaders = {};
  function show(id) {
    current = id;
    for (const [v] of VIEWS) { const b = $("nav-" + v); b.setAttribute("aria-selected", v === id); b.tabIndex = v === id ? 0 : -1; }
    $("mainView").style.display = id === "page" ? "" : "none";
    $("settingsView").style.display = "none";
    for (const k of Object.keys(panes)) panes[k].classList.toggle("on", k === id);
    clearInterval(pollTimer); pollTimer = null;
    if (loaders[id]) loaders[id]();
    saveState(); restoreScroll();
  }
  $("btnCog").addEventListener("click", () => { for (const k of Object.keys(panes)) panes[k].classList.remove("on"); for (const [v] of VIEWS) $("nav-" + v).setAttribute("aria-selected", "false"); });

  // --------------------------------------------------------- scroll memory ---
  let tabKey = "t0";
  const scrollKey = () => current + "|" + tabKey;
  let scrollTimer = null;
  window.addEventListener("scroll", () => {
    clearTimeout(scrollTimer);
    scrollTimer = setTimeout(async () => {
      const st = (await X.storageGet("vgScroll", "session")).vgScroll || {};
      st[scrollKey()] = Math.round(document.scrollingElement.scrollTop);
      const keys = Object.keys(st); if (keys.length > 60) delete st[keys[0]];
      X.storageSet({ vgScroll: st }, "session");
    }, 250);
  }, { passive: true });
  async function restoreScroll() {
    const st = (await X.storageGet("vgScroll", "session")).vgScroll || {};
    const y = st[scrollKey()] || 0; if (!y) return;
    let n = 0;   // content arrives async: retry until the page is tall enough
    const tryIt = () => { document.scrollingElement.scrollTop = y; if (Math.abs(document.scrollingElement.scrollTop - y) > 2 && n++ < 8) setTimeout(tryIt, 120); };
    setTimeout(tryIt, 60);
  }
  async function saveState() { X.storageSet({ vgPanelView: current }, "session"); }

  // ------------------------------------------------------------ app status ---
  let desk = null;
  async function refreshDesk(force) { desk = await send({ type: "VG_DESKTOP_INFO", force: !!force }, { timeout: 8000 }); return desk; }
  function offlineBox() {
    return h("div", { class: "pmsg warn", role: "status" }, "The Video Grabber app isn't running. Browsing and detection still work; direct files can be saved by the browser. ",
      h("button", { class: "pbtn", text: "Check again", onclick: async () => { await refreshDesk(true); if (loaders[current]) loaders[current](); } }));
  }

  // --------------------------------------------------------------- library ---
  const lib = { q: "", sort: "time", scope: "all", kinds: new Set(), sel: new Set(), rows: [], open: null, tpl: "", seq: 0, total: 0 };
  async function loadLibrary() {
    const box = panes.library; if (!box.dataset.built) buildLibraryShell(box);
    const tab = await X.activeTab();
    tabKey = "t" + (tab ? tab.id : 0);
    const seq = ++lib.seq;   // sweep: a slow answer to an older search must not replace a newer one
    const r = await send({ type: "LIB_LIST", q: lib.q, sort: lib.sort, scope: lib.scope, tabId: tab && tab.id, kinds: [...lib.kinds], hideSegments: true, limit: 400 });
    if (seq !== lib.seq) return;
    lib.rows = r.items || []; lib.total = r.total || lib.rows.length; renderLibrary(r);
  }
  function buildLibraryShell(box) {
    box.dataset.built = "1";
    const search = h("input", { type: "search", id: "libq", placeholder: "Search name, site, type…  ( / )", "aria-label": "Search the library", oninput: debounce((e) => { lib.q = e.target.value; loadLibrary(); }, 150) });
    const sort = h("select", { "aria-label": "Sort", onchange: (e) => { lib.sort = e.target.value; loadLibrary(); } },
      ...[["time", "Newest"], ["size", "Largest"], ["name", "Name"], ["host", "Site"], ["kind", "Type"]].map(([v, t]) => h("option", { value: v, text: t })));
    const scope = h("select", { "aria-label": "Scope", onchange: (e) => { lib.scope = e.target.value; loadLibrary(); } }, h("option", { value: "all", text: "All tabs" }), h("option", { value: "remembered", text: "Remembered" }), h("option", { value: "tab", text: "This tab" }));
    const chips = h("div", { class: "chips", role: "group", "aria-label": "Filter by type" },
      ...["video", "audio", "stream", "image", "document", "archive"].map((k) => h("button", { class: "chip", "aria-pressed": "false", text: k,
        onclick: (e) => { lib.kinds.has(k) ? lib.kinds.delete(k) : lib.kinds.add(k); e.target.setAttribute("aria-pressed", lib.kinds.has(k)); loadLibrary(); } })));
    const tpl = h("select", { id: "libtpl", "aria-label": "File name template", onchange: (e) => { lib.tpl = e.target.value; } }, h("option", { value: "", text: "Auto name" }));
    send({ type: "TEMPLATES_GET" }).then((t) => { for (const x of t.templates || []) tpl.append(h("option", { value: x.id, text: x.name })); });
    const bar2 = h("div", { class: "pbar" },
      h("button", { class: "pbtn pri", id: "libSend", text: "Send selected", onclick: sendSelected }),
      h("button", { class: "pbtn", text: "Select all", onclick: () => { for (const r of lib.rows) lib.sel.add(r.url); renderLibrary(); } }),
      h("button", { class: "pbtn", text: "None", onclick: () => { lib.sel.clear(); renderLibrary(); } }),
      h("button", { class: "pbtn", text: "Schedule…", onclick: () => { toolsPrefillSchedule(); show("tools"); } }),
      tpl);
    box.append(h("div", { class: "pbar" }, search, sort, scope), chips, bar2, h("div", { id: "libstat", class: "sethint", "aria-live": "polite" }), h("div", { id: "liblist", role: "list" }));
  }
  function renderLibrary(r) {
    const list = $("liblist"); list.textContent = "";
    // sweep: say "showing N of M" when the 400 row page is shorter than the match count, and keep the count on re-renders
    $("libstat").textContent = lib.rows.length ? (lib.total > lib.rows.length ? "showing " + lib.rows.length + " of " + lib.total : lib.total + " found") + (lib.sel.size ? " · " + lib.sel.size + " selected" : "") : "";
    const old = $("forgetHost"); if (old) old.remove();
    // M1.4: when the Remembered list has narrowed to one host, offer to forget that whole host
    const hosts = new Set(lib.rows.map((x) => x.host));
    if (lib.scope === "remembered" && lib.rows.length && hosts.size === 1) {
      const host = lib.rows[0].host;
      list.before(h("button", { class: "pbtn danger", id: "forgetHost", text: "Forget all on " + host, onclick: async () => {
        if (!confirm("Forget everything remembered for " + host + "?")) return;
        await send({ type: "LIB_FORGET", host }); loadLibrary(); } }));
    }
    if (!lib.rows.length) { list.append(h("div", { class: "empty", text: lib.q || lib.kinds.size ? "Nothing matches. Clear the search or filters." : "Nothing detected yet. Play a video and it will appear here." })); return; }
    lib.rows.forEach((row, i) => list.append(libRow(row, i)));
  }
  function libRow(row, i) {
    const open = lib.open === row.url;
    const cb = h("input", { type: "checkbox", "aria-label": "Select " + row.suggestedName, checked: lib.sel.has(row.url),
      onchange: (e) => { e.target.checked ? lib.sel.add(row.url) : lib.sel.delete(row.url); $("libstat").textContent = lib.sel.size + " selected"; } });
    const el = h("div", { class: "prow", role: "listitem", "data-i": i, tabindex: 0 },
      h("div", { class: "top" }, cb,
        h("span", { class: "nm", title: row.url }, row.suggestedName || row.filename || row.host,
          row.alreadySent ? h("span", { class: "badge ok", text: "sent" }) : null,
          row.kind === "stream" ? h("span", { class: "badge", text: "stream" }) : null),
        h("span", { class: "acts" },
          h("button", { text: "Send", "aria-label": "Send " + row.suggestedName, onclick: () => sendOne(row, !!row.alreadySent) }),
          row.remembered ? h("button", { text: "Forget", "aria-label": "Forget " + (row.suggestedName || row.url), onclick: async () => { await send({ type: "LIB_FORGET", url: row.url }); loadLibrary(); } }) : null,
          h("button", { text: "Info", "aria-expanded": open, onclick: () => { lib.open = open ? null : row.url; renderLibrary(); } }))),
      h("div", { class: "meta" }, [row.host, row.kind, row.resolutionHint, row.sizeLabel, row.pageTitle, row.remembered && row.lastSeen ? "seen " + C.relTime(row.lastSeen) : ""].filter(Boolean).join(" · ")));
    if (open) el.append(inspector(row));
    return el;
  }
  function inspector(row) {
    const box = h("div", { class: "insp", "aria-live": "polite" }, "Inspecting…");
    send({ type: "LIB_INSPECT", url: row.url, item: { contentType: row.contentType, size: row.size } }, { timeout: 20000 }).then(async (r) => {
      box.textContent = "";
      if (!r || !r.inspect) { box.textContent = "Couldn't inspect this item."; return; }
      const i = r.inspect, rows = [["URL", row.url], ["Type", i.kind + (i.ext ? " (." + i.ext + ")" : "")], ["Site", i.host], ["Size", i.sizeLabel || "unknown"]];
      if (i.contentType) rows.push(["Content-Type", i.contentType]);
      if (r.head && r.head.ok) rows.push(["Ranges", r.head.acceptRanges ? "supported" : "not advertised"], ["Modified", r.head.lastModified || "unknown"]);
      if (i.resolutionHint) rows.push(["Resolution hint", i.resolutionHint]);
      if (r.hls && r.hls.valid) {
        rows.push(["Playlist", r.hls.master ? "master, " + r.hls.variants.length + " variants" : (r.hls.live ? "live" : r.hls.segments + " segments, " + Math.round(r.hls.duration) + " s")]);
        rows.push(["Merge", r.hls.safeToMerge ? "safe" + (r.hls.reason ? " (" + r.hls.reason + ")" : "") : "not safe: " + r.hls.reason]);
        if (r.hls.variants.length) rows.push(["Variants", r.hls.variants.map((v) => (v.height ? v.height + "p" : "?") + (v.fps ? " " + v.fps + "fps" : "") + (v.codecs ? " " + v.codecs.split(",")[0] : "")).join(", ")]);
      }
      if (r.hlsError) rows.push(["Playlist", "couldn't read: " + r.hlsError]);
      box.append(h("dl", {}, rows.flatMap(([a, b]) => [h("dt", { text: a }), h("dd", { text: b })])));
      for (const w of i.warnings) box.append(h("div", { class: "w", text: "! " + w }));
      for (const n of i.notes) box.append(h("div", { class: "n", text: n }));
      const q = await send({ type: "QUALITY_GET", host: i.host });
      const sel = h("select", { "aria-label": "Preferred quality for " + i.host, onchange: async (e) => { await send({ type: "QUALITY_SET", host: i.host, height: Number(e.target.value) }); toast(Number(e.target.value) ? "Will prefer up to " + e.target.value + "p on " + i.host : "Quality preference cleared"); } },
        ...[[0, "Auto"], [2160, "Up to 2160p"], [1440, "Up to 1440p"], [1080, "Up to 1080p"], [720, "Up to 720p"], [480, "Up to 480p"]].map(([v, t]) => h("option", { value: v, text: t, selected: (q && q.prefs && q.prefs[i.host] || 0) === v })));
      box.append(h("div", { class: "pf row" }, h("label", { text: "Quality on " + i.host }), sel,
        h("button", { class: "pbtn", text: "Copy URL", onclick: () => navigator.clipboard.writeText(row.url).then(() => toast("Copied"), () => toast("Copy failed", true)) })));
    });
    return box;
  }
  async function sendOne(row, force) {
    if (row.alreadySent && !force) return;
    const r = await send({ type: "SEND_SMART", url: row.url, pageUrl: row.pageUrl, title: row.pageTitle, contentType: row.contentType, size: row.size, templateId: lib.tpl, force: !!force }, { timeout: 40000 });
    if (r.duplicate) { if (confirm("This was sent before. Send it again?")) return sendOne(row, true); return; }
    if (r.ok) { toast((r.standalone ? "Saved by the browser: " : "Sent: ") + (r.filename || "") + (r.note ? " (" + r.note + ")" : "")); loaders[current] && loaders[current](); }
    else fail(r);
  }
  async function sendSelected() {
    const rows = lib.rows.filter((r) => lib.sel.has(r.url));
    if (!rows.length) return toast("Select something first.", true);
    if (rows.length > 20 && !confirm("Send " + rows.length + " items?")) return;
    const r = await send({ type: "SEND_MANY", templateId: lib.tpl, items: rows.map((x) => ({ url: x.url, pageUrl: x.pageUrl, title: x.pageTitle, contentType: x.contentType, size: x.size })) }, { timeout: 120000 });
    if (r.sent) toast("Sent " + r.sent + (r.failed ? ", " + r.failed + " failed" : "")); else fail((r.results || [])[0] || r);
    lib.sel.clear(); loadLibrary();
  }
  loaders.library = loadLibrary;

  // ----------------------------------------------------------------- queue ---
  async function loadQueue() {
    const box = panes.queue; await refreshDesk(false);
    const render = async () => {
      const r = await send({ type: "JOBS_LIST" });
      box.textContent = "";
      if (!r.ok) { box.append(r.offline || (desk && !desk.alive) ? offlineBox() : h("div", { class: "pmsg err", text: errText(r) })); return; }
      const running = r.queue && (r.queue.running ?? r.queue.queue_running);
      const c = r.counts || {};
      box.append(h("div", { class: "pbar" },
        h("button", { class: "pbtn pri", text: running ? "Pause queue" : "Start queue", onclick: async () => { const x = await send({ type: "QUEUE_SET", running: !running }); x.ok ? render() : fail(x); } }),
        h("button", { class: "pbtn", text: "Retry failed" + (c.error ? " (" + c.error + ")" : ""), disabled: !c.error, onclick: async () => { const x = await send({ type: "JOBS_RETRY_FAILED" }); toast("Retried " + (x.retried || 0)); render(); } }),
        h("button", { class: "pbtn danger", text: "Clear failed", disabled: !c.error, onclick: async () => { if (!confirm("Remove all failed rows from the app's list?")) return; const x = await send({ type: "JOBS_CLEAR_FAILED" }); x.ok ? render() : fail(x); } })),
        h("div", { class: "sethint", "aria-live": "polite", text: Object.entries(c).map(([k, v]) => v + " " + k).join(" · ") || "No downloads yet." }));
      for (const j of r.jobs.slice(0, 150)) box.append(jobRow(j, render));
      if (!r.jobs.length) box.append(h("div", { class: "empty", text: "The app's list is empty." }));
    };
    await render();
    pollTimer = setInterval(() => { if (current === "queue" && !document.hidden) render(); }, 2500);
  }
  function jobRow(j, rerender) {
    const act = (action, extra) => async () => { const x = await send({ type: "JOB_ACTION", id: j.id, action, ...(extra || {}) }); x.ok ? rerender() : fail(x); };
    const btn = (t, a, extra) => h("button", { text: t, "aria-label": t + " " + (j.filename || j.id), onclick: act(a, extra) });
    const acts = [];
    if (j.status === "downloading") acts.push(btn("Pause", "pause"), btn("Stop", "stop"));
    else if (j.status === "paused") acts.push(btn("Resume", "resume"), btn("Stop", "stop"));
    else if (j.status === "queued") { acts.push(btn("↑", "reorder", { direction: "up" }), btn("↓", "reorder", { direction: "down" }), btn("Stop", "stop")); }
    else if (j.status === "error" || j.status === "stopped") acts.push(btn("Retry", "retry"));
    else if (j.status === "done") acts.push(btn("Folder", "open-folder"));
    acts.push(btn("✕", "delete"));
    const pct = Math.max(0, Math.min(100, Number(j.progress) || 0));
    return h("div", { class: "prow" + (j.status === "error" ? " err" : ""), role: "listitem" },
      h("div", { class: "top" }, h("span", { class: "nm", title: j.url }, j.filename || j.url || j.id, h("span", { class: "badge" + (j.status === "error" ? " err" : j.status === "done" ? " ok" : ""), text: j.status })), h("span", { class: "acts" }, acts)),
      j.status === "downloading" || j.status === "paused" ? h("div", { class: "bar", role: "progressbar", "aria-valuenow": pct, "aria-valuemin": 0, "aria-valuemax": 100 }, h("i", { style: "width:" + pct + "%" })) : null,
      h("div", { class: "meta" }, [j.speed, j.eta && "ETA " + j.eta, j.error, j.retry_count ? "retry " + j.retry_count : ""].filter(Boolean).join(" · ")));
  }
  loaders.queue = loadQueue;

  // --------------------------------------------------------------- archive ---
  const arch = { q: "", status: "" };
  async function loadArchive() {
    const box = panes.archive; box.textContent = "";
    const r = await send({ type: "ARCHIVE_LIST", q: arch.q, status: arch.status });
    box.append(h("div", { class: "pbar" },
      h("input", { type: "search", placeholder: "Search sent items…", "aria-label": "Search the archive", value: arch.q, oninput: debounce((e) => { arch.q = e.target.value; loadArchive(); }, 200) }),
      h("button", { class: "pbtn danger", text: "Clear all", disabled: !r.total, onclick: async () => { if (confirm("Forget all " + r.total + " archive entries? The app's downloads are not touched.")) { await send({ type: "ARCHIVE_CLEAR" }); loadArchive(); } } })),
      h("div", { class: "sethint", text: r.total + " item" + (r.total === 1 ? "" : "s") + " sent from this browser" }));
    for (const e of r.items || []) {
      box.append(h("div", { class: "prow", role: "listitem" },
        h("div", { class: "top" }, h("span", { class: "nm", title: e.url, text: e.filename || C.hostOf(e.url) || e.url }, e.count > 1 ? h("span", { class: "badge", text: "×" + e.count }) : null),
          h("span", { class: "acts" },
            h("button", { text: "Send again", onclick: async () => { const x = await send({ type: "SEND_SMART", url: e.url, pageUrl: e.pageUrl, filename: e.filename, force: true }, { timeout: 40000 }); x.ok ? toast("Sent again") : fail(x); } }),
            h("button", { text: "✕", "aria-label": "Remove from archive", onclick: async () => { await send({ type: "ARCHIVE_REMOVE", url: e.url, format_id: e.format_id || "" }); loadArchive(); } }))),
        h("div", { class: "meta", text: new Date(e.at).toLocaleString() + " · " + (C.hostOf(e.url) || "") + (e.via ? " · " + e.via : "") + (e.format_id ? " · format " + e.format_id : "") })));
    }
    if (!(r.items || []).length) box.append(h("div", { class: "empty", text: arch.q ? "No matches." : "Nothing sent yet." }));
    restoreScroll();
  }
  loaders.archive = loadArchive;

  // ----------------------------------------------------------------- tools ---
  let schedPrefill = null;
  function toolsPrefillSchedule() { schedPrefill = lib.rows.filter((r) => lib.sel.has(r.url)); }
  async function loadTools() {
    const box = panes.tools; box.textContent = "";
    const cfg = vgNormalize((await X.storageGet(VG_SETTINGS_KEY))[VG_SETTINGS_KEY]);
    // -- preferences
    const chk = (key, label) => h("div", { class: "setrow chk" }, h("input", { type: "checkbox", id: "pf-" + key, checked: !!cfg[key], onchange: async (e) => { const cur = vgNormalize((await X.storageGet(VG_SETTINGS_KEY))[VG_SETTINGS_KEY]); cur[key] = e.target.checked; await X.storageSet({ [VG_SETTINGS_KEY]: cur }); applyDensity(cur); } }), h("label", { for: "pf-" + key, text: label }));
    box.append(h("div", { class: "psec" }, h("h2", { text: "Behaviour" }),
      chk("smartOneClick", "Smart one-click: pick name, quality and variant automatically"),
      chk("standaloneFallback", "Let the browser save direct files when the app is off"),
      chk("notifyDiscovery", "Notify when new media is found on a page"),
      chk("libraryHideSegments", "Hide stream fragments (.ts, .m4s) in the library"),
      chk("confirmDuplicates", "Ask before sending something already sent"),
      h("div", { class: "pf row" }, h("label", { for: "pf-density", text: "Density" }), h("select", { id: "pf-density", onchange: async (e) => { const cur = vgNormalize((await X.storageGet(VG_SETTINGS_KEY))[VG_SETTINGS_KEY]); cur.density = e.target.value; await X.storageSet({ [VG_SETTINGS_KEY]: cur }); applyDensity(cur); } },
        h("option", { value: "comfortable", text: "Comfortable", selected: cfg.density === "comfortable" }), h("option", { value: "compact", text: "Compact", selected: cfg.density === "compact" })))));
    box.append(await scheduleSection(), await watchSection(), await templateSection(), await qualitySection(), themeSection(), await backupSection(), await shortcutSection(), diagSection());
  }
  async function scheduleSection() {
    const sec = h("div", { class: "psec" }, h("h2", { text: "Scheduled downloads" }));
    const tab = await X.activeTab();
    const name = h("input", { type: "text", id: "sc-name", placeholder: "Name", value: schedPrefill ? "Selected items" : (tab ? (tab.title || "").slice(0, 40) : "") });
    const kind = h("select", { id: "sc-kind", "aria-label": "When" }, h("option", { value: "once", text: "Once at…" }), h("option", { value: "daily", text: "Every day at…" }), h("option", { value: "every", text: "Every N minutes" }));
    const when = h("input", { type: "datetime-local", id: "sc-when" }); const t = h("input", { type: "time", id: "sc-time", value: "23:00" }); const mins = h("input", { type: "number", id: "sc-min", min: 1, value: 60, style: "width:80px" });
    const urls = h("textarea", { id: "sc-urls", rows: 3, placeholder: "Links, one per line. Leave empty to use " + (schedPrefill ? schedPrefill.length + " selected library items" : "everything detected on the current tab") });
    const sync = () => { when.style.display = kind.value === "once" ? "" : "none"; t.style.display = kind.value === "daily" ? "" : "none"; mins.style.display = kind.value === "every" ? "" : "none"; }; kind.onchange = sync; sync();
    const d = new Date(Date.now() + 3600000); d.setMinutes(0, 0, 0); when.value = new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
    const list = h("div", { id: "sc-list" });
    const add = h("button", { class: "pbtn pri", text: "Schedule", onclick: async () => {
      let items = urls.value.split(/\s+/).filter((u) => /^https?:/i.test(u)).map((u) => ({ url: u }));
      let pageUrl = "";
      if (!items.length && schedPrefill && schedPrefill.length) items = schedPrefill.map((r) => ({ url: r.url, title: r.pageTitle }));
      if (!items.length && tab) { const l = await send({ type: "LIB_LIST", scope: "tab", tabId: tab.id }); items = (l.items || []).filter((r) => ["video", "audio", "stream"].includes(r.kind)).map((r) => ({ url: r.url, title: r.pageTitle })); pageUrl = tab.url; }
      const spec = kind.value === "once" ? { when: new Date(when.value).getTime() } : kind.value === "daily" ? { daily: t.value } : { every: Number(mins.value), anchor: Date.now() };
      if (kind.value === "once" && !(spec.when > Date.now())) return toast("Pick a time in the future.", true);
      const r = await send({ type: "SCHEDULE_ADD", name: name.value, items, spec, pageUrl: pageUrl || (items[0] && items[0].pageUrl) || "" });
      if (r.ok) { toast("Scheduled " + items.length + " item(s)"); schedPrefill = null; renderList(); } else fail(r);
    } });
    sec.append(h("div", { class: "pf" }, name, h("div", { class: "pf row" }, kind, when, t, mins), urls, add,
      h("div", { class: "sethint", text: "Runs while the browser is open. If the app is off at that time it retries a few times, then reports it." }), list));
    async function renderList() {
      const r = await send({ type: "SCHEDULE_LIST" }); list.textContent = "";
      for (const j of r.schedules || []) list.append(h("div", { class: "prow" }, h("div", { class: "top" }, h("span", { class: "nm", text: j.name }, h("span", { class: "badge" + (j.enabled ? "" : " warn"), text: j.enabled ? "on" : "off" })),
        h("span", { class: "acts" }, h("button", { text: "Run now", onclick: async () => { const x = await send({ type: "SCHEDULE_RUN_NOW", id: j.id }, { timeout: 60000 }); x.ok ? toast("Sent") : fail(x); renderList(); } }),
          h("button", { text: j.enabled ? "Disable" : "Enable", onclick: async () => { await send({ type: "SCHEDULE_TOGGLE", id: j.id, enabled: !j.enabled }); renderList(); } }),
          h("button", { text: "✕", "aria-label": "Delete schedule " + j.name, onclick: async () => { await send({ type: "SCHEDULE_REMOVE", id: j.id }); renderList(); } }))),
        h("div", { class: "meta", text: j.items.length + " item(s) · " + (j.enabled && j.nextAt ? "next " + new Date(j.nextAt).toLocaleString() : "not scheduled") + (j.lastResult ? " · last: " + j.lastResult : "") })));
      if (!(r.schedules || []).length) list.append(h("div", { class: "empty", text: "No schedules." }));
    }
    renderList(); return sec;
  }
  async function watchSection() {
    const sec = h("div", { class: "psec" }, h("h2", { text: "Watchlist" }));
    const pat = h("input", { type: "text", id: "w-pat", placeholder: "example.com, lecture, ep\\d+", "aria-label": "Pattern" });
    const kind = h("select", { "aria-label": "Match type" }, h("option", { value: "host", text: "Site" }), h("option", { value: "text", text: "Text in link" }), h("option", { value: "regex", text: "Regex" }));
    const action = h("select", { "aria-label": "Action" }, h("option", { value: "notify", text: "Notify me" }), h("option", { value: "send", text: "Send automatically" }));
    const list = h("div");
    const render = async () => { const r = await send({ type: "WATCH_LIST" }); list.textContent = "";
      for (const w of r.rules || []) list.append(h("div", { class: "prow" }, h("div", { class: "top" }, h("span", { class: "nm", text: w.kind + ": " + w.pattern }, h("span", { class: "badge" + (w.action === "send" ? " warn" : ""), text: w.action })),
        h("span", { class: "acts" }, h("button", { text: w.enabled ? "Disable" : "Enable", onclick: async () => { await send({ type: "WATCH_TOGGLE", id: w.id, enabled: !w.enabled }); render(); } }), h("button", { text: "✕", "aria-label": "Remove watch " + w.pattern, onclick: async () => { await send({ type: "WATCH_REMOVE", id: w.id }); render(); } })))));
      if (!(r.rules || []).length) list.append(h("div", { class: "empty", text: "Nothing watched." })); };
    sec.append(h("div", { class: "pf row" }, pat, kind, action, h("button", { class: "pbtn pri", text: "Add", onclick: async () => { const r = await send({ type: "WATCH_ADD", pattern: pat.value, kind: kind.value, action: action.value }); if (r.ok) { pat.value = ""; render(); } else fail(r); } })),
      h("div", { class: "sethint", text: "Checked as media is detected. \"Send automatically\" sends without asking, use it only for sites you trust." }), list);
    render(); return sec;
  }
  async function templateSection() {
    const r = await send({ type: "TEMPLATES_GET" });
    const ta = h("textarea", { rows: 5, "aria-label": "Templates, one per line as Name = pattern", id: "tpl-text" }); ta.value = (r.templates || []).map((t) => t.name + " = " + t.pattern).join("\n");
    return h("div", { class: "psec" }, h("h2", { text: "Batch name templates" }), ta,
      h("div", { class: "sethint", text: "Tokens: {title} {host} {date} {time} {n} {nn} {res} {ext}. One per line: Name = pattern." }),
      h("div", { class: "pbar" }, h("button", { class: "pbtn pri", text: "Save templates", onclick: async () => {
        const list = ta.value.split("\n").map((l, i) => { const m = /^\s*(.+?)\s*=\s*(.+?)\s*$/.exec(l); return m ? { id: "t" + i + "-" + m[1].toLowerCase().replace(/\W+/g, "").slice(0, 12), name: m[1], pattern: m[2] } : null; }).filter(Boolean);
        await send({ type: "TEMPLATES_SET", templates: list }); toast("Saved " + list.length + " template(s)"); } }),
        h("button", { class: "pbtn", text: "Reset", onclick: () => { ta.value = (r.defaults || []).map((t) => t.name + " = " + t.pattern).join("\n"); } }),
        h("span", { class: "sethint", id: "tpl-prev" })));
  }
  async function qualitySection() {
    const sec = h("div", { class: "psec" }, h("h2", { text: "Quality per site" })); const list = h("div");
    const render = async () => { const r = await send({ type: "QUALITY_GET" }); list.textContent = "";
      for (const [host, hgt] of Object.entries(r.prefs || {})) list.append(h("div", { class: "setrow" }, h("span", { text: host + " → up to " + hgt + "p" }), h("button", { class: "pbtn", text: "Remove", "aria-label": "Remove quality for " + host, onclick: async () => { await send({ type: "QUALITY_SET", host, height: 0 }); render(); } })));
      if (!Object.keys(r.prefs || {}).length) list.append(h("div", { class: "empty", text: "No site has its own limit. Set one from a library item's Info." })); };
    sec.append(list); render(); return sec;
  }
  const PRESETS = { Handheld: { bg_base: "#0a0a0f", bg_panel: "#12121a", text: "#e6e6eb", accent: "#26c6da", text_on_accent: "#0a0a0f", border: "#242430" },
    Light: { bg_base: "#f6f7f9", bg_panel: "#ffffff", text: "#1b1d22", accent: "#0b6e99", text_on_accent: "#ffffff", border: "#d6d9df" },
    Amber: { bg_base: "#14100a", bg_panel: "#1d1710", text: "#f1e6d2", accent: "#ffb300", text_on_accent: "#14100a", border: "#3a2f1d" },
    "High contrast": { bg_base: "#000000", bg_panel: "#000000", text: "#ffffff", accent: "#ffff00", text_on_accent: "#000000", border: "#ffffff" } };
  function themeSection() {
    const prev = h("div", { class: "prev", id: "th-prev" }, h("div", { text: "Sample row: Lecture 4 720p.mp4" }), h("div", { style: "margin-top:6px" }, h("span", { class: "pp", text: "Download" })));
    const paint = (t) => { const s = prev.style; s.setProperty("--pbg", t.bg_panel || t.bg_base); s.setProperty("--ptx", t.text); s.setProperty("--pac", t.accent); s.setProperty("--pon", t.text_on_accent || t.bg_base); s.setProperty("--pbd", t.border || t.text); };
    let chosen = "";
    const sel = h("select", { "aria-label": "Theme preset", onchange: (e) => { chosen = e.target.value; if (chosen && PRESETS[chosen]) paint(PRESETS[chosen]); } }, h("option", { value: "", text: "Preview a theme…" }), ...Object.keys(PRESETS).map((k) => h("option", { value: k, text: k })));
    X.storageGet("theme").then((s) => { if (s.theme) paint(s.theme); });
    return h("div", { class: "psec" }, h("h2", { text: "Theme preview" }), prev,
      h("div", { class: "pbar", style: "margin-top:8px" }, sel,
        h("button", { class: "pbtn pri", text: "Use in extension", onclick: async () => { if (!PRESETS[chosen]) return toast("Pick a theme to preview first.", true); await X.storageSet({ themeOverride: PRESETS[chosen] }); toast("Applied. Reopen the panel to see it everywhere."); } }),
        h("button", { class: "pbtn", text: "Back to app theme", onclick: async () => { await X.call("storage.local.remove", "themeOverride"); const r = await send({ type: "VG_DESKTOP_INFO", force: true }); toast("Using the app's theme again."); } })),
      h("div", { class: "sethint", text: "Preview changes only the box above until you press Use in extension. The pill follows the app's theme." }));
  }
  async function backupSection() {
    const file = h("input", { type: "file", accept: "application/json,.json", class: "sr", id: "imp-file", "aria-label": "Settings file to import", onchange: async (e) => {
      const f = e.target.files[0]; if (!f) return; if (f.size > 2e6) return toast("That file is too large.", true);
      const text = await f.text(); const p = C.parseImport(text); if (!p.ok) return toast(p.error, true);
      if (!confirm("Import " + Object.keys(p.data).length + " settings group(s)? This replaces the matching ones here. Your pairing token is not touched.")) return;
      const r = await send({ type: "SETTINGS_IMPORT", text }); r.ok ? toast("Imported: " + r.applied.join(", ")) : fail(r); loadTools(); } });
    return h("div", { class: "psec" }, h("h2", { text: "Backup" }),
      h("div", { class: "pbar" }, h("button", { class: "pbtn", text: "Export settings", onclick: async () => {
        const r = await send({ type: "SETTINGS_EXPORT" }); if (!r.ok) return fail(r);
        const a = h("a", { href: URL.createObjectURL(new Blob([r.text], { type: "application/json" })), download: r.name }); document.body.append(a); a.click(); a.remove(); toast("Exported"); } }),
        h("button", { class: "pbtn", text: "Import settings…", onclick: () => file.click() }), file),
      h("div", { class: "sethint", text: "Includes settings, site limits, templates, watchlist, schedules and hidden sites. Never the pairing token." }));
  }
  async function shortcutSection() {
    const cmds = (await X.call("commands.getAll")).value || [];
    const sec = h("div", { class: "psec" }, h("h2", { text: "Keyboard shortcuts" }));
    for (const c of cmds) if (c.name !== "_execute_action") sec.append(h("div", { class: "setrow" }, h("span", { text: c.description || c.name }), h("kbd", { text: c.shortcut || "not set" })));
    sec.append(h("div", { class: "sethint" }, "In this window: ", h("kbd", { text: "/" }), " search, ", h("kbd", { text: "1" }), "–", h("kbd", { text: "5" }), " switch section, ", h("kbd", { text: "j" }), "/", h("kbd", { text: "k" }), " move, ", h("kbd", { text: "Enter" }), " send."),
      h("button", { class: "pbtn", text: "Change shortcuts…", onclick: () => { X.call("tabs.create", { url: "chrome://extensions/shortcuts" }); } }));
    return sec;
  }
  function diagSection() {
    const out = h("pre", { class: "log", id: "diag-out", tabindex: 0, "aria-label": "Diagnostic log" }, "Press Refresh.");
    const sum = h("div", { class: "sethint", id: "diag-sum" });
    const refresh = async () => { const d = await send({ type: "VG_DIAG" }); if (!d.ok) { out.textContent = errText(d); return; }
      const ap = d.desktop || {}; const caps = Object.entries(d.caps || {}).filter(([, v]) => v).map(([k]) => k).join(", ");
      sum.textContent = "v" + d.version + " · app " + (ap.alive ? (ap.identity ? "running " + ap.version : "other service on port") : "not running") + (ap.paired ? " · paired" : " · not paired") + " · " + d.itemsTracked + " items in " + d.tabsTracked + " tabs · browser features: " + caps;
      out.textContent = d.log || "(empty)"; out.scrollTop = out.scrollHeight; };
    return h("div", { class: "psec" }, h("h2", { text: "Diagnostics" }), sum,
      h("div", { class: "pbar" }, h("button", { class: "pbtn", text: "Refresh", onclick: refresh }),
        h("button", { class: "pbtn", text: "Copy report", onclick: () => navigator.clipboard.writeText((sum.textContent || "") + "\n" + out.textContent).then(() => toast("Copied"), () => toast("Copy failed", true)) }),
        h("button", { class: "pbtn", text: "Clear log", onclick: async () => { await send({ type: "VG_DIAG_CLEAR" }); refresh(); } }),
        null), out);
  }
  loaders.tools = loadTools;

  // popup.js flips the .active class on its two page tabs; keep aria-selected in step.
  for (const id of ["tabCurrent", "tabOther"]) {
    const b = $(id); if (!b) continue;
    new MutationObserver(() => b.setAttribute("aria-selected", b.classList.contains("active"))).observe(b, { attributes: true, attributeFilter: ["class"] });
  }
  // ------------------------------------------------------ misc: density, keys ---
  function applyDensity(cfg) { document.documentElement.classList.toggle("density-compact", cfg.density === "compact"); }
  X.storageGet(VG_SETTINGS_KEY).then((s) => applyDensity(vgNormalize(s[VG_SETTINGS_KEY])));
  function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }
  document.addEventListener("keydown", (e) => {
    const tag = (e.target.tagName || "").toLowerCase();
    const typing = tag === "input" || tag === "textarea" || tag === "select" || e.target.isContentEditable;
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.key === "Escape" && typing) { e.target.blur(); return; }
    if (typing) return;
    if (/^[1-5]$/.test(e.key)) { show(VIEWS[Number(e.key) - 1][0]); return; }
    if (e.key === "/") { e.preventDefault(); const q = current === "library" ? $("libq") : current === "page" ? $("searchInput") : null; if (q) { q.style.display = ""; q.focus(); } return; }
    if (e.key === "j" || e.key === "k") {
      const rows = [...document.querySelectorAll(".pview.on .prow, #mainView .mrow")]; if (!rows.length) return;
      let i = rows.indexOf(document.activeElement.closest(".prow, .mrow")); i = e.key === "j" ? Math.min(rows.length - 1, i + 1) : Math.max(0, i - 1);
      const r = rows[i]; r.tabIndex = 0; r.focus(); r.scrollIntoView({ block: "nearest" }); return; }
    if (e.key === "Enter" && current === "library") { const r = document.activeElement.closest && document.activeElement.closest(".prow"); if (r) { const row = lib.rows[Number(r.dataset.i)]; if (row) sendOne(row, !!row.alreadySent); } }
  });

  // ----------------------------------------------------- side panel behaviours ---
  if (isSide) {
    const refreshPage = () => { if (current === "page" && typeof window.loadCurrent === "function") window.loadCurrent(); else if (loaders[current] && current === "library") loaders.library(); };
    try {
      chrome.tabs.onActivated.addListener(() => { setTimeout(refreshPage, 150); });
      chrome.tabs.onUpdated.addListener((id, info) => { if (info.status === "complete" || info.url) setTimeout(refreshPage, 300); });
    } catch (e) { LOG.warn("panel", "tab listeners failed", e); }
    setInterval(() => { if (!document.hidden && (current === "page" || current === "library")) refreshPage(); }, 4000);
  }

  // ---------------------------------------------------------------- startup ---
  (async () => {
    X.caps(true);
    const st = await X.storageGet("vgPanelView", "session");
    refreshDesk(false);
    if (isSide && st.vgPanelView && panes[st.vgPanelView]) show(st.vgPanelView); else { const t = await X.activeTab(); tabKey = "t" + (t ? t.id : 0); restoreScroll(); }
  })();
  window.VGPANEL = { show, toast };
})();
const BACKEND_BASE = "http://127.0.0.1:5757";

// --- CIRCUIT BREAKER: Prevent popup lockups if the app is dead ---
const originalFetch = window.fetch;
window.fetch = function(...args) {
    if (args[0] && typeof args[0] === 'string' && !args[0].includes('127.0.0.1') && !args[0].includes('localhost')) {
        return originalFetch.apply(this, args);
    }
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 2000); 
    const options = args[1] || {};
    options.signal = controller.signal;
    args[1] = options;
    return originalFetch.apply(this, args).finally(() => clearTimeout(timeoutId));
};
// -----------------------------------------------------------------

// ---------------- helpers ----------------

function fmtBytes(n) {
  if (!n) return "";
  const units = ["B", "KB", "MB", "GB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(n >= 100 || i === 0 ? 0 : 1)} ${units[i]}`;
}

function fileName(url) {
  try {
    return decodeURIComponent(url.split("/").pop().split("?")[0] || url);
  } catch (e) {
    return url;
  }
}

// Reddit's CDN names files m2-res_1280p.mp4 / DASH_720.mp4. For those hosts
// use the tab title instead (same sanitizing as content.js sendChoice).
function redditCdnName(url, tab) {
  try {
    if (!/(^|\.)(v|packaged-media)\.redd\.it$/i.test(new URL(url).hostname)) return undefined;
    const title = tab && tab.title;
    const ext = (new URL(url).pathname.match(/(\.(?:mp4|m4v|mov|webm|mkv|mp3|m4a|aac))$/i) || [""])[0];
    if (!title || !ext) return undefined;
    return title.replace(/[\\/:*?"<>|]/g, "_").slice(0, 80) + ext;
  } catch (e) {
    return undefined;
  }
}

// 7.7: CDN chunk URLs end in a long random ID ("AbC123...xyz.mp4"), which says
// nothing. Show what the item is and how big instead; the full URL goes in
// the tooltip. Normal file names are shown as before.
const CHUNK_ID_RE = /^[A-Za-z0-9_-]{16,}\./;

function chunkLabel(it) {
  const name = fileName(it.url);
  if (!CHUNK_ID_RE.test(name)) return null;
  const ct = (it.contentType || "").toLowerCase();
  const e = extOf(it.url);
  let kind = "Stream";
  if (ct.startsWith("video/") || ["mp4", "m4v", "mov", "webm", "mkv", "avi"].includes(e)) kind = "Video";
  else if (ct.startsWith("audio/") || ["mp3", "m4a", "aac", "wav", "flac", "opus", "ogg"].includes(e)) kind = "Audio";
  return `${kind} \u00b7 ${fmtBytes(it.size) || "unknown"}`;
}

function extOf(url) {
  const m = url.match(/\.([a-z0-9]{1,5})(\?|#|$)/i);
  return m ? m[1].toLowerCase() : "";
}

function categoryOf(url) {
  const e = extOf(url);
  if (["mp4", "m4v", "mov", "avi", "flv", "wmv", "mpg", "mpeg", "ts", "3gp", "3g2", "m2ts", "mts", "vob", "ogv", "rm", "rmvb", "asf", "mxf", "divx"].includes(e)) return "mp4";
  if (e === "webm" || e === "mkv") return "webm";
  if (e === "m3u8" || e === "mpd") return "m3u8";
  if (["mp3", "m4a", "aac", "wav", "flac", "opus", "ogg", "wma", "aiff", "aif", "alac", "ape", "m4b", "mid", "midi", "amr"].includes(e)) return "mp3";
  if (["jpg", "jpeg", "jpe", "png", "gif", "webp", "avif", "heic", "heif", "bmp", "ico", "tif", "tiff", "jxl", "psd", "raw", "dng", "cr2", "nef", "xcf", "svg"].includes(e)) return "image";
  if (["pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "csv", "tsv", "txt", "md", "rtf", "odt", "ods", "odp", "json", "xml", "yaml", "yml", "epub", "mobi", "azw3"].includes(e)) return "doc";
  if (["zip", "rar", "7z", "tar", "gz", "tgz", "bz2", "xz", "lz", "lzma", "zst", "cab", "iso", "img"].includes(e)) return "archive";
  if (["exe", "msi", "dmg", "deb", "rpm", "apk", "appimage", "appx", "bat", "cmd", "sh", "ps1", "jar", "py", "js"].includes(e)) return "programs";
  if (["ttf", "otf", "woff", "woff2", "eot"].includes(e)) return "font";
  return "other";
}

// ---------------- state ----------------

let currentTab = null;
let tabMode = "current";       // "current" | "other"
let otherTabs = [];
let otherSelId = null;
let mediaItems = [];           // rows: {url, size, timestamp, checked, ...}
let filterSet = null;          // null = all visible; else Set of categories
let searchText = "";
let sortMode = "time";         // E5: "time" | "size" | "name" (saved as popupSort)
let _autoTried = false;        // v4.2: single-item auto-download runs once per popup open
const GRAB_CONFIRM_LIMIT = 5;  // v4.2: Grab-all sends above this need a confirmation

// v4.2: small toast at the bottom of the popup.
let _toastTimer = null;
function showToast(text, isErr) {
  const el = document.getElementById("toast");
  if (!el) return;
  el.textContent = text;
  el.classList.toggle("err", !!isErr);
  el.classList.add("show");
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => el.classList.remove("show"), 2200);
}

async function checkBackend() {
  const statusEl = document.getElementById("status");
  try {
    const res = await fetch(`${BACKEND_BASE}/ping`);
    if (!res.ok) throw new Error("bad status");
    const { apiToken } = await chrome.storage.local.get("apiToken");
    if (!apiToken) {
      statusEl.textContent = "App running, but not paired. Open this extension's Options and test the connection.";
      statusEl.className = "bad";
      return true;
    }
    let extra = "";
    try {
      const q = await fetch(`${BACKEND_BASE}/queue-status`, { headers: { "X-API-Token": apiToken } });
      if (q.status === 401) {
        statusEl.textContent = "App running, but the saved token was rejected. Re-pair in Options.";
        statusEl.className = "bad";
        return true;
      }
      if (q.ok) {
        const s = await q.json();
        const bits = [];
        if (s.active) bits.push(`${s.active} downloading`);
        if (s.queued) bits.push(`${s.queued} queued`);
        if (s.youtube_hold_s > 0) bits.push(`YouTube held ${Math.ceil(s.youtube_hold_s / 60)} min`);
        if (s.low_disk) bits.push("low disk space");
        extra = bits.length ? " · " + bits.join(" · ") : "";
      }
    } catch (e) { /* older app without /queue-status: plain status */ }
    statusEl.textContent = "Desktop app connected ✓" + extra;
    statusEl.className = "ok";
    return true;
  } catch (e) {
    statusEl.textContent = "Desktop app not running. Launch it, then reopen this popup.";
    statusEl.className = "bad";
    return false;
  }
}

// ---------------- media list ----------------

// E5: Time is newest first (the original behaviour), Size is largest first
// (unknown sizes last), Name is A to Z.
function compareItems(a, b) {
  const byTime = (b.timestamp || 0) - (a.timestamp || 0);
  if (sortMode === "size") return (b.size || 0) - (a.size || 0) || byTime;
  if (sortMode === "name") {
    return fileName(a.url).toLowerCase().localeCompare(fileName(b.url).toLowerCase()) || byTime;
  }
  return byTime;
}

function updateSortButton() {
  const btn = document.getElementById("btnSort");
  if (btn) btn.textContent = "Sort: " + { time: "Time", size: "Size", name: "Name" }[sortMode];
}

function setItems(items, tabRef) {
  // Preserve checkbox state across reloads, keyed by URL.
  const prev = new Map(mediaItems.map((i) => [i.url, i.checked]));
  mediaItems = (items || [])
    .slice()
    .sort(compareItems)
    .map((i) => ({
      ...i,
      size: i.size || 0,
      checked: prev.has(i.url) ? prev.get(i.url) : true,
      tabRef,
    }));
  renderMedia();
}

// ---- v5: recent downloads (live) and the per-site pill switch -------------
function fmtB(n) {
  if (!n) return "";
  const u = ["B", "KB", "MB", "GB"]; let i = 0; let v = n;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(i ? 1 : 0)} ${u[i]}`;
}

async function refreshRecent() {
  const box = document.getElementById("recent");
  const list = document.getElementById("recentList");
  try {
    const { apiToken } = await chrome.storage.local.get("apiToken");
    if (!apiToken) { box.style.display = "none"; return; }
    const res = await fetch(`${BACKEND_BASE}/jobs`, { headers: { "X-API-Token": apiToken } });
    if (!res.ok) { box.style.display = "none"; return; }
    // Keep the id (the object key) so a row can ask the app to open its folder.
    const jobsObj = await res.json();
    const jobs = Object.entries(jobsObj).map(([key, j]) => ({ ...j, _id: j.id || j.job_id || key }));
    jobs.sort((a, b) => (b.created_ts || 0) - (a.created_ts || 0));
    const recent = jobs.filter((j) => j.status !== "skipped").slice(0, 5);
    if (!recent.length) { box.style.display = "none"; return; }
    list.textContent = "";
    for (const j of recent) {
      const pct = j.size_total ? Math.min(100, Math.round(100 * (j.size_done || 0) / j.size_total))
                               : (j.status === "done" ? 100 : 0);
      const row = document.createElement("div");
      row.className = "rjob" + (j.status === "error" ? " err" : "");
      const name = document.createElement("span");
      name.className = "rname"; name.textContent = j.filename || j.url; name.title = j.filename || "";
      const bar = document.createElement("div");
      bar.className = "rbar";
      const fill = document.createElement("i"); fill.style.width = pct + "%";
      bar.appendChild(fill);
      const meta = document.createElement("div");
      meta.className = "rmeta";
      const stat = document.createElement("span");
      stat.className = "rstat";
      const retrying = j.status === "queued" && (j.retry_after || 0) > Date.now() / 1000;
      stat.textContent = retrying ? "retrying soon" : j.status + (j.status === "downloading" ? ` ${pct}%` : "");
      if (j.status === "done") {
        stat.classList.add("link");
        stat.title = "Open folder";
        stat.addEventListener("click", () => openJobFolder(j));
      }
      const right = document.createElement("span");
      right.textContent = j.status === "downloading" ? (j.speed || "") : fmtB(j.size_total);
      meta.appendChild(stat); meta.appendChild(right);
      row.appendChild(name); row.appendChild(bar); row.appendChild(meta);
      list.appendChild(row);
    }
    box.style.display = "block";
  } catch (e) {
    box.style.display = "none";
  }
}

// v4.2: ask the app to open Explorer at a finished job's file.
async function openJobFolder(j) {
  try {
    const { apiToken } = await chrome.storage.local.get("apiToken");
    const res = await fetch(`${BACKEND_BASE}/open-folder`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-API-Token": apiToken || "" },
      body: JSON.stringify({ job_id: j._id, filename: j.filename || null }),
    });
    if (res.ok) { showToast("Opening folder…"); return; }
    const d = await res.json().catch(() => ({}));
    showToast(d.error || (res.status === 404 ? "Open folder needs a newer Video Grabber app" : "Couldn't open the folder"), true);
  } catch (e) {
    showToast("Desktop app not reachable", true);
  }
}

function startRecent() {
  refreshRecent();
  setInterval(refreshRecent, 1500);
}

async function setupSiteSwitch() {
  const btn = document.getElementById("siteToggle");
  const nameEl = document.getElementById("siteName");
  const row = document.getElementById("siteRow");
  let host = "";
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    host = new URL(tab.url).hostname.replace(/^www\./, "").toLowerCase();
  } catch (e) {}
  if (!host) { row.style.display = "none"; return; }
  nameEl.textContent = host;
  const paint = async () => {
    const { disabledSites = [] } = await chrome.storage.local.get("disabledSites");
    const off = disabledSites.includes(host);
    btn.textContent = off ? "Show pill on this site" : "Hide pill on this site";
  };
  btn.addEventListener("click", async () => {
    const { disabledSites = [] } = await chrome.storage.local.get("disabledSites");
    const next = disabledSites.includes(host)
      ? disabledSites.filter((h) => h !== host) : [...disabledSites, host];
    await chrome.storage.local.set({ disabledSites: next });
    paint();
  });
  paint();
}

const TINY_BYTES = 100 * 1024;

function visibleItems() {
  const hideTiny = document.getElementById("hideTiny");
  const tinyOn = !hideTiny || hideTiny.checked;
  return mediaItems.filter((it) => {
    if (filterSet && !filterSet.has(categoryOf(it.url))) return false;
    // Known-size tiny files are almost always page assets, not downloads.
    // HLS/DASH manifests are tiny by nature, so they are exempt.
    if (tinyOn && it.size > 0 && it.size < TINY_BYTES
        && categoryOf(it.url) !== "m3u8") return false;
    if (searchText) {
      const hay = (it.url + " " + fileName(it.url)).toLowerCase();
      if (!hay.includes(searchText)) return false;
    }
    return true;
  });
}

function renderMedia() {
  const list = document.getElementById("mediaList");
  const statusEl = document.getElementById("mediaStatus");
  list.innerHTML = "";
  // Sizes arrive later (HEAD probes), so the order is re-applied every render.
  mediaItems.sort(compareItems);
  updateSortButton();
  const visible = visibleItems();
  const hidden = mediaItems.length - visible.length;
  if (!visible.length) {
    // 7.4: say why the list is empty. Nothing detected vs. all filtered out.
    const empty = document.createElement("div");
    empty.className = "empty";
    if (!hidden) {
      empty.textContent = "No media on this page.";
    } else {
      empty.append(`${hidden} item(s) hidden by filter \u2014 `);
      const showAll = document.createElement("button");
      showAll.type = "button";
      showAll.className = "linkbtn";
      showAll.textContent = "Show all";
      showAll.addEventListener("click", showAllItems);
      empty.appendChild(showAll);
    }
    list.appendChild(empty);
  } else {
    for (const it of visible) list.appendChild(rowEl(it));
  }
  const checked = visible.filter((i) => i.checked);
  statusEl.textContent = mediaItems.length
    ? `${checked.length} of ${visible.length} selected` + (hidden ? ` (${hidden} hidden by filter)` : "")
    : "";
  // 7.5: nothing visible and nothing hidden: no status line at all.
  statusEl.style.display = (!visible.length && !hidden) ? "none" : "";
  updateToolbar();
  probeUnknownSizes(visible);
}

// HEAD-probe items whose size is unknown (a few at a time) so the tiny-file
// filter and the size labels have real numbers. Extension pages are exempt
// from CORS thanks to host_permissions.
const _sizeProbed = new Set();
// Sweep (v4.5.1): a probe that failed used to stay in _sizeProbed forever, so the
// size stayed unknown. Failures are now retried, at most 3 times, 15 s apart.
const _sizeFail = new Map();   // url -> {n, at}
const SIZE_RETRY_MS = 15000, SIZE_MAX_TRIES = 3;
async function probeUnknownSizes(items) {
  const todo = items.filter((i) => {
    if (i.size || _sizeProbed.has(i.url) || !/^https?:/i.test(i.url)) return false;
    const f = _sizeFail.get(i.url);
    return !f || (f.n < SIZE_MAX_TRIES && Date.now() - f.at >= SIZE_RETRY_MS);
  }).slice(0, 25);
  if (!todo.length) return;
  todo.forEach((i) => _sizeProbed.add(i.url));
  let changed = false;
  const queue = todo.slice();
  const worker = async () => {
    while (queue.length) {
      const it = queue.shift();
      try {
        const r = await fetch(it.url, { method: "HEAD", signal: AbortSignal.timeout(6000) });
        const n = parseInt(r.headers.get("content-length") || "0", 10) || 0;
        if (n) { it.size = n; changed = true; }
        if (!r.ok && (r.status >= 500 || r.status === 429)) throw new Error("HTTP " + r.status);   // transient: retry later
      } catch (e) {
        // leave size unknown, but allow another try later
        _sizeProbed.delete(it.url);
        const f = _sizeFail.get(it.url) || { n: 0, at: 0 };
        _sizeFail.set(it.url, { n: f.n + 1, at: Date.now() });
      }
    }
  };
  await Promise.all([worker(), worker(), worker(), worker()]);
  if (changed) renderMedia();
}

// E4: 16x16 favicon of the page the item came from, from the browser's own
// favicon cache (needs the "favicon" permission, already declared). The
// letter tile shows until the image loads and stays if it never does.
function faviconEl(it) {
  const tab = it.tabRef || currentTab || {};
  const pageUrl = tab.url || "";
  let host = "";
  try { host = new URL(pageUrl || it.url).hostname.replace(/^www\./, ""); } catch (e) {}
  const tile = document.createElement("span");
  tile.className = "fav";
  tile.textContent = (host[0] || "?").toUpperCase();
  if (/^https?:/i.test(pageUrl)) {
    try {
      const u = new URL(chrome.runtime.getURL("/_favicon/"));
      u.searchParams.set("pageUrl", pageUrl);
      u.searchParams.set("size", "16");
      const img = document.createElement("img");
      img.alt = "";
      img.width = 16;
      img.height = 16;
      img.addEventListener("load", () => {
        tile.textContent = "";
        tile.classList.add("has-img");
        tile.appendChild(img);
      });
      img.src = u.toString();
    } catch (e) { /* keep the letter tile */ }
  }
  return tile;
}

// v4.2: the per-row send, shared by the row's download button and the
// single-item auto-download. Same message and filename rules as before.
function dlFilename(it, tab) {
  return redditCdnName(it.url, tab) || (/\.(mp4|m4v|mov|webm|mkv|avi|flv|wmv|mpg|mpeg|mp3|m4a|aac|wav|flac|ogg|opus|wma|aiff|m4b|m3u8|mpd|pdf|zip|rar|7z|tar|gz|tgz|bz2|xz|zst|cab|iso|epub|mobi|azw3|docx?|xlsx?|pptx?|txt|md|rtf|odt|ods|odp|csv|tsv|json|xml|yaml|yml|exe|msi|dmg|deb|rpm|apk|appimage|bat|cmd|sh|ps1|jar|jpe?g|png|gif|webp|avif|heic|heif|bmp|ico|tif|tiff|jxl|psd|raw|woff2?|eot|ttf|otf|3g2|3gp|aif|alac|amr|ape|appx|asf|cr2|divx|dng|img|jpe|js|lz|lzma|m2ts|mid|midi|mts|mxf|nef|ogv|py|rm|rmvb|svg|ts|vob|xcf)(\?|#|$)/i.test(it.url)
          ? fileName(it.url) : undefined);
}

function relayOne(it, tab, cb) {
  chrome.runtime.sendMessage(
    { type: "RELAY_DOWNLOAD", url: it.url, pageUrl: tab.url,
      tabId: tab.id, filename: dlFilename(it, tab) },
    cb
  );
}

function rowEl(it) {
  const row = document.createElement("div");
  row.className = "mrow";

  const cb = document.createElement("input");
  cb.type = "checkbox";
  cb.checked = it.checked;
  cb.addEventListener("change", () => { it.checked = cb.checked; renderMedia(); });

  const name = document.createElement("span");
  name.className = "mname";
  name.textContent = chunkLabel(it)
    || `${fileName(it.url)} — ${fmtBytes(it.size) || "unknown"}`;
  name.title = it.url;   // full URL on hover
  // v4.2: mark the top-level HLS playlist so it isn't mistaken for a sub-stream.
  if (extOf(it.url) === "m3u8" && /master/i.test(fileName(it.url))) {
    const tag = document.createElement("span");
    tag.className = "vtag";
    tag.textContent = "[Master]";
    tag.title = "Top-level playlist (contains every quality)";
    name.appendChild(tag);
  }

  const acts = document.createElement("span");
  acts.className = "macts";

  // ⬇ — same as the old per-row "Send to Grabber"
  const bDl = document.createElement("button");
  bDl.textContent = "⬇";
  bDl.title = "Send to Grabber";
  bDl.addEventListener("click", () => {
    bDl.textContent = "…";
    const tab = it.tabRef || currentTab || {};
    relayOne(it, tab, (resp) => {
      bDl.textContent = "⬇";
      setStatus(resp && resp.ok ? `Sent ${fileName(it.url)} ✓`
                                : (resp && resp.error) || "Failed");
    });
  });

  const bOpen = document.createElement("button");
  bOpen.textContent = "▶";
  bOpen.title = "Open in new tab";
  bOpen.addEventListener("click", () => chrome.tabs.create({ url: it.url }));

  const bCopy = document.createElement("button");
  bCopy.textContent = "📋";
  bCopy.title = "Copy URL";
  bCopy.addEventListener("click", () => {
    navigator.clipboard.writeText(it.url).then(() => setStatus("URL copied ✓"));
  });

  acts.append(bDl, bOpen, bCopy);
  row.append(cb, faviconEl(it), name, acts);
  return row;
}

function setStatus(text) {
  const el = document.getElementById("mediaStatus");
  el.textContent = text;
  el.style.display = "";   // a message must show even when the list is empty
}

// "Show all" in the hidden-by-filter message: every type on, small files
// shown, search cleared.
function showAllItems() {
  document.querySelectorAll("#filterPanel input[value]").forEach((b) => {
    if (b.id !== "hideTiny") b.checked = true;
  });
  const tiny = document.getElementById("hideTiny");
  if (tiny) tiny.checked = false;
  const s = document.getElementById("searchInput");
  if (s) s.value = "";
  searchText = "";
  rebuildFilterSet();
}

// ---------------- toolbar ----------------

function updateToolbar() {
  const checked = visibleItems().filter((i) => i.checked);
  document.getElementById("btnMerge").disabled =
    !checked.some((i) => categoryOf(i.url) === "m3u8");
  document.getElementById("btnDownload").disabled = !checked.length;
  document.getElementById("btnDownload").textContent =
    checked.length ? `Download (${checked.length})` : "Download";
  document.getElementById("btnCopy").disabled = !checked.length;
}

function sendBatch(viaMerge) {
  const checked = visibleItems().filter((i) => i.checked);
  if (!checked.length) return;
  if (viaMerge && checked.some((i) => categoryOf(i.url) === "m3u8")) {
    if (!confirm("HLS streams (.m3u8) will be merged into a single file by the desktop app. Continue?")) return;
  }
  const pageUrl = (currentTab && currentTab.url) || "";
  setStatus(`Sending ${checked.length} item(s)…`);
  chrome.runtime.sendMessage(
    { type: "RELAY_BATCH",
      items: checked.map((i) => {
        const { url, format_id, target_format } = i;
        const filename = redditCdnName(url, i.tabRef || currentTab);
        return filename ? { url, format_id, target_format, filename }
                        : { url, format_id, target_format };
      }),
      pageUrl },
    (resp) => {
      setStatus(resp && resp.ok
        ? `Sent ${(resp.job_ids || []).length} item(s) to Grabber ✓`
        : (resp && resp.error) || "Failed to send.");
    }
  );
}

// ---------------- tabs / loading ----------------

function showTab(mode) {
  tabMode = mode;
  document.getElementById("tabCurrent").classList.toggle("active", mode === "current");
  document.getElementById("tabOther").classList.toggle("active", mode === "other");
  document.getElementById("otherTabs").style.display = mode === "other" ? "block" : "none";
  if (mode === "current") loadCurrent();
  else loadOther();
}

async function loadCurrent() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab) { setItems([], null); return; } // tab closed mid-query
  currentTab = tab;
  chrome.runtime.sendMessage({ type: "GET_VIDEOS", tabId: tab.id }, (resp) => {
    setItems((resp && resp.videos) || [], tab);
    if (!_autoTried) { _autoTried = true; maybeAutoDownload(tab); }
  });
}

// v4.2: exactly one visible media item on the current page -> send it right
// away, toast, and close the popup. Runs once per popup open, and never twice
// for the same URL in the same tab (the second open shows the normal list).
async function maybeAutoDownload(tab) {
  if (document.documentElement.classList.contains("side")) return;   // a side panel never auto-sends
  try {
    if (tabMode !== "current" || !/^https?:/i.test(tab.url || "")) return;
    const vis = visibleItems();
    if (vis.length !== 1) return;
    const it = vis[0];
    const KEY = "autoSent";
    const st = await chrome.storage.session.get(KEY);
    const sent = st[KEY] || {};
    if (sent[tab.id] === it.url) return;
    sent[tab.id] = it.url;
    await chrome.storage.session.set({ [KEY]: sent });
    showToast(`Sending ${fileName(it.url)}…`);
    relayOne(it, tab, async (resp) => {
      if (resp && resp.ok) {
        showToast(`Sent ${fileName(it.url)} ✓`);
        setTimeout(() => window.close(), 1300);
      } else {
        delete sent[tab.id];
        try { await chrome.storage.session.set({ [KEY]: sent }); } catch (e) {}
        showToast((resp && resp.error) || "Failed to send", true);
      }
    });
  } catch (e) { /* keep the normal list */ }
}

// 7.6: "Other Pages (N)" once other tabs have detected media. The current tab
// is not "other", so it is left out of the list and the count.
function setOtherLabel() {
  document.getElementById("tabOther").textContent =
    otherTabs.length > 0 ? `Other Pages (${otherTabs.length})` : "Other Pages";
}

function fetchOtherTabs(cb) {
  chrome.runtime.sendMessage({ type: "GET_ALL_VIDEOS" }, (resp) => {
    const curId = currentTab && currentTab.id;
    otherTabs = ((resp && resp.tabs) || []).filter((t) => t.id !== curId);
    setOtherLabel();
    if (cb) cb();
  });
}

function loadOther() {
  fetchOtherTabs(() => {
    renderOtherTabs();
    if (otherTabs.length) selectOtherTab(otherTabs[0].id, false);
    else setItems([], null);
  });
}

function renderOtherTabs() {
  const box = document.getElementById("otherTabs");
  box.innerHTML = "";
  if (!otherTabs.length) {
    box.innerHTML = '<div class="empty">No media detected on other tabs.</div>';
    return;
  }
  for (const t of otherTabs) {
    const el = document.createElement("div");
    el.className = "otab" + (t.id === otherSelId ? " sel" : "");
    const title = document.createElement("span");
    title.textContent = t.title || t.url || `Tab ${t.id}`;
    title.style.overflow = "hidden";
    title.style.textOverflow = "ellipsis";
    title.style.whiteSpace = "nowrap";
    title.style.maxWidth = "260px";
    const count = document.createElement("span");
    count.textContent = `${t.items.length} item(s)`;
    el.append(title, count);
    el.addEventListener("click", () => selectOtherTab(t.id, true));
    box.appendChild(el);
  }
}

function selectOtherTab(tabId, activate) {
  otherSelId = tabId;
  renderOtherTabs();
  const t = otherTabs.find((x) => x.id === tabId);
  if (!t) return;
  if (activate) chrome.tabs.update(tabId, { active: true });
  setItems(t.items, { id: t.id, url: t.url, title: t.title });
}

// ---------------- filter / search ----------------

function rebuildFilterSet() {
  const boxes = Array.from(document.querySelectorAll("#filterPanel input[data-type], #filterPanel input[value]"))
    .filter((b) => b.value && b.id !== "hideTiny");
  const on = new Set(boxes.filter((b) => b.checked).map((b) => b.value));
  filterSet = on.size === boxes.length ? null : on;
  const tiny = document.getElementById("hideTiny");
  chrome.storage.local.set({ popupFilter: { types: Array.from(on), hideTiny: tiny ? tiny.checked : true } });
  renderMedia();
}

async function restoreFilter() {
  const { popupFilter } = await chrome.storage.local.get("popupFilter");
  if (!popupFilter || !Array.isArray(popupFilter.types)) return;
  const want = new Set(popupFilter.types);
  document.querySelectorAll("#filterPanel input[value]").forEach((b) => {
    if (b.id !== "hideTiny") b.checked = want.has(b.value);
  });
  const tiny = document.getElementById("hideTiny");
  if (tiny && typeof popupFilter.hideTiny === "boolean") tiny.checked = popupFilter.hideTiny;
}

// ---------------- grab-all (secondary, unchanged feature) ----------------

// Scans the page DOM for downloadable-looking links and media sources.
// Runs inside the page via chrome.scripting.executeScript — keep it
// self-contained (no outside references).
function scanPageForDownloads() {
  const EXT_RE = /\.(mp4|m4v|mov|webm|mkv|avi|mp3|wav|flac|m4a|zip|rar|7z|pdf|docx?|pptx?|exe|msi)(\?|#|$)/i;
  const found = new Map();
  document.querySelectorAll("a[href]").forEach((a) => {
    if (EXT_RE.test(a.href)) found.set(a.href, a.textContent.trim() || a.href);
  });
  document.querySelectorAll("video, audio").forEach((el) => {
    if (el.currentSrc && !el.currentSrc.startsWith("blob:")) found.set(el.currentSrc, document.title);
    el.querySelectorAll("source[src]").forEach((s) => {
      if (s.src && !s.src.startsWith("blob:")) found.set(s.src, document.title);
    });
  });
  return Array.from(found.entries()).map(([url, filename]) => ({ url, filename }));
}

let grabItems = [];

// v4.2: "Queue X items? [Yes] [Cancel]" for big sends.
function hideGrabConfirm() {
  const box = document.getElementById("grabConfirm");
  if (box) box.style.display = "none";
  const sendBtn = document.getElementById("grabSendBtn");
  if (sendBtn) sendBtn.style.display = "block";
}

function showGrabConfirm(count, onYes) {
  document.getElementById("grabConfirmText").textContent = `Queue ${count} items?`;
  document.getElementById("grabSendBtn").style.display = "none";
  document.getElementById("grabConfirm").style.display = "flex";
  document.getElementById("grabConfirmYes").onclick = () => { hideGrabConfirm(); onYes(); };
  document.getElementById("grabConfirmNo").onclick = hideGrabConfirm;
}

function renderGrabChecklist() {
  hideGrabConfirm();
  const checklist = document.getElementById("grabChecklist");
  const toolbar = document.getElementById("grabToolbar");
  const sendBtn = document.getElementById("grabSendBtn");
  checklist.innerHTML = "";
  if (!grabItems.length) {
    toolbar.style.display = "none";
    sendBtn.style.display = "none";
    return;
  }
  toolbar.style.display = "flex";
  sendBtn.style.display = "block";
  grabItems.forEach((item, idx) => {
    const row = document.createElement("div");
    row.className = "grab-item";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = item.checked;
    cb.addEventListener("change", () => {
      grabItems[idx].checked = cb.checked;
      updateGrabToolbar();
    });
    const name = document.createElement("span");
    name.className = "name";
    name.textContent = item.filename && item.filename !== item.url
      ? `${item.filename} — ${item.url.length > 60 ? item.url.slice(0, 60) + "…" : item.url}`
      : (item.url.length > 90 ? item.url.slice(0, 90) + "…" : item.url);
    row.appendChild(cb);
    row.appendChild(name);
    checklist.appendChild(row);
  });
  updateGrabToolbar();
}

function updateGrabToolbar() {
  hideGrabConfirm();
  const countEl = document.getElementById("grabCount");
  const sendBtn = document.getElementById("grabSendBtn");
  const checkedCount = grabItems.filter((i) => i.checked).length;
  countEl.textContent = `${checkedCount} of ${grabItems.length} selected`;
  sendBtn.disabled = checkedCount === 0;
  sendBtn.textContent = checkedCount
    ? `Download Selected (${checkedCount})`
    : "Download Selected";
}

async function grabAll(tab) {
  const resultEl = document.getElementById("grabResult");
  resultEl.textContent = "Scanning page…";
  grabItems = [];
  renderGrabChecklist();
  let results;
  try {
    const frames = await chrome.scripting.executeScript({
      target: { tabId: tab.id, allFrames: true },
      func: scanPageForDownloads,
    });
    // Same URL can surface from several frames (iframe + parent) — dedupe.
    const seen = new Set();
    results = frames.flatMap((f) => f.result || []).filter((r) => {
      if (seen.has(r.url)) return false;
      seen.add(r.url);
      return true;
    });
  } catch (e) {
    resultEl.textContent = "Couldn't scan this page.";
    return;
  }
  if (!results || !results.length) {
    resultEl.textContent = "No downloadable links found on this page.";
    return;
  }
  grabItems = results.map((r) => ({ ...r, checked: true }));
  resultEl.textContent = `Found ${results.length} item(s) — choose which to send below.`;
  renderGrabChecklist();
}

// ---------------- v4.3: overflow menu (the "..." button) ----------------
// Merge, Copy, Toggle, Sort, Filter, Clear and Search live in #moreMenu. They
// are the same buttons with the same ids and handlers; this only opens and
// closes the menu (click, Escape, arrow keys, click outside).
function setupOverflowMenu() {
  const btn = document.getElementById("btnMore");
  const menu = document.getElementById("moreMenu");
  const items = () => Array.from(menu.querySelectorAll("button")).filter((b) => !b.disabled);
  const close = (refocus) => {
    menu.hidden = true;
    btn.setAttribute("aria-expanded", "false");
    if (refocus) btn.focus();
  };
  const open = () => {
    menu.hidden = false;
    btn.setAttribute("aria-expanded", "true");
    const first = items()[0];
    if (first) first.focus();
  };
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    if (menu.hidden) open(); else close(true);
  });
  // The button's own handler runs first (target phase), then the menu closes.
  menu.addEventListener("click", (e) => {
    if (e.target.closest("button:not(:disabled)")) close(false);
  });
  document.addEventListener("click", (e) => {
    if (!menu.hidden && !menu.contains(e.target) && e.target !== btn) close(false);
  });
  document.addEventListener("keydown", (e) => {
    if (menu.hidden) return;
    if (e.key === "Escape") { e.preventDefault(); close(true); return; }
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const list = items();
      if (!list.length) return;
      const i = list.indexOf(document.activeElement);
      const next = e.key === "ArrowDown" ? (i + 1) % list.length : (i - 1 + list.length) % list.length;
      list[next].focus();
    }
  });
}

// ---------------- v4.3: settings panel (cog) ----------------
// Sections: Pill / Image Micro-Pill / Sniffer / Downloads / Notifications.
// Every control saves straight to chrome.storage.local (key vgSettings,
// see vgsettings.js); content.js and background.js pick the change up
// through storage.onChanged.
async function setupSettings() {
  const $ = (id) => document.getElementById(id);
  const mainView = $("mainView");
  const setView = $("settingsView");
  const showSettings = (on) => {
    mainView.style.display = on ? "none" : "";
    setView.style.display = on ? "block" : "none";
    if (on) paintSites();
  };
  $("btnCog").addEventListener("click", () => showSettings(setView.style.display !== "block"));
  $("setBack").addEventListener("click", () => showSettings(false));

  let cfg = vgNormalize((await chrome.storage.local.get(VG_SETTINGS_KEY))[VG_SETTINGS_KEY]);
  // [element id, setting key, kind]
  const controls = [
    ["setPillVisible", "pillVisible", "bool"],
    ["setOneClick", "oneClickMode", "bool"],
    ["setMicroPill", "microPill", "bool"],
    ["setMicroPos", "microPillPos", "str"],
    ["setMicroSize", "microPillSize", "bool"],
    ["setIntercept", "interceptDownloads", "bool"],
    ["setNeedsClick", "interceptNeedsGesture", "bool"],
    ["setIgnore", "interceptIgnore", "str"],
    ["setCorner", "pillCorner", "str"],
    ["setQuality", "defaultQuality", "num"],
    ["setMenuH", "maxMenuHeight", "num"],
    ["setSniffer", "snifferMode", "str"],
    ["setNotifyRec", "notifyRecording", "bool"],
    ["setNotifyAct", "notifyActions", "bool"],
  ];
  const paint = () => {
    for (const [id, key, kind] of controls) {
      const el = $(id);
      if (kind === "bool") el.checked = !!cfg[key];
      else el.value = String(cfg[key]);
    }
  };
  for (const [id, key, kind] of controls) {
    $(id).addEventListener("change", async () => {
      const el = $(id);
      const value = kind === "bool" ? el.checked : kind === "num" ? Number(el.value) : el.value;
      // Sweep: start from what is stored now, not from the copy read when the popup
      // opened, or a change made meanwhile in Tools or Options would be overwritten.
      try { cfg = vgNormalize((await chrome.storage.local.get(VG_SETTINGS_KEY))[VG_SETTINGS_KEY]); } catch (e) { /* keep the last copy */ }
      cfg = vgNormalize({ ...cfg, [key]: value });
      try { await chrome.storage.local.set({ [VG_SETTINGS_KEY]: cfg }); }
      catch (e) { showToast("Couldn't save the setting", true); }
      paint();
    });
  }
  paint();

  // Sites the pill is hidden on (the same list the "Hide pill on this site"
  // button edits), each removable here.
  async function paintSites() {
    const box = $("setSites");
    const { disabledSites = [] } = await chrome.storage.local.get("disabledSites");
    box.textContent = "";
    if (!disabledSites.length) {
      const none = document.createElement("div");
      none.className = "none";
      none.textContent = "None. The pill shows on every site.";
      box.appendChild(none);
      return;
    }
    for (const host of disabledSites) {
      const row = document.createElement("div");
      row.className = "site";
      const name = document.createElement("span");
      name.textContent = host;
      const rm = document.createElement("button");
      rm.textContent = "\u00d7";
      rm.title = "Show the pill on " + host;
      rm.setAttribute("aria-label", "Show the pill on " + host);
      rm.addEventListener("click", async () => {
        const cur = (await chrome.storage.local.get("disabledSites")).disabledSites || [];
        await chrome.storage.local.set({ disabledSites: cur.filter((h) => h !== host) });
        paintSites();
      });
      row.append(name, rm);
      box.appendChild(row);
    }
  }
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area === "local" && changes.disabledSites && setView.style.display === "block") paintSites();
  });

  $("setResetPos").addEventListener("click", async () => {
    await chrome.storage.local.remove("pillPrefs");
    showToast("Pill positions reset");
  });
  $("setOptions").addEventListener("click", () => {
    try { chrome.runtime.openOptionsPage(); } catch (e) { showToast("Couldn't open Options", true); }
  });
}

// ---------------- wiring ----------------

(async () => {
  await checkBackend();
  startRecent();
  setupSiteSwitch();
  setupSettings();
  setupOverflowMenu();

  document.getElementById("tabCurrent").addEventListener("click", () => showTab("current"));
  document.getElementById("tabOther").addEventListener("click", () => showTab("other"));

  document.getElementById("btnMerge").addEventListener("click", () => sendBatch(true));
  document.getElementById("btnDownload").addEventListener("click", () => sendBatch(false));
  document.getElementById("btnCopy").addEventListener("click", () => {
    const urls = visibleItems().filter((i) => i.checked).map((i) => i.url);
    if (!urls.length) return;
    navigator.clipboard.writeText(urls.join("\n")).then(() =>
      setStatus(`Copied ${urls.length} URL(s) ✓`));
  });
  document.getElementById("btnToggle").addEventListener("click", () => {
    // Toggle only what the user can see — hidden (filtered/searched-out)
    // items must not get swept into a batch download.
    const vis = visibleItems();
    const allChecked = vis.length > 0 && vis.every((i) => i.checked);
    vis.forEach((i) => { i.checked = !allChecked; });
    renderMedia();
  });
  document.getElementById("btnFilter").addEventListener("click", () => {
    // Read the real state: the panel's initial display comes from CSS, so
    // comparing the inline style made the first click a no-op.
    const p = document.getElementById("filterPanel");
    p.style.display = getComputedStyle(p).display === "none" ? "block" : "none";
  });
  await restoreFilter();
  // E5: restore the saved sort order and let the button cycle it.
  try {
    const { popupSort } = await chrome.storage.local.get("popupSort");
    if (["time", "size", "name"].includes(popupSort)) sortMode = popupSort;
  } catch (e) {}
  updateSortButton();
  document.getElementById("btnSort").addEventListener("click", () => {
    sortMode = sortMode === "time" ? "size" : sortMode === "size" ? "name" : "time";
    chrome.storage.local.set({ popupSort: sortMode });
    renderMedia();
  });
  document.querySelectorAll("#filterPanel input[type=checkbox]")
    .forEach((b) => b.addEventListener("change", rebuildFilterSet));
  rebuildFilterSet();
  document.getElementById("btnClear").addEventListener("click", () => {
    if (!currentTab) return;
    chrome.runtime.sendMessage({ type: "CLEAR_VIDEOS", tabId: currentTab.id }, () => {
      setStatus("List cleared.");
      showTab(tabMode);
    });
  });
  document.getElementById("btnSearch").addEventListener("click", () => {
    const s = document.getElementById("searchInput");
    s.style.display = s.style.display === "none" ? "block" : "none";
    if (s.style.display === "block") s.focus();
    else { s.value = ""; searchText = ""; renderMedia(); }
  });
  document.getElementById("searchInput").addEventListener("input", (e) => {
    searchText = e.target.value.trim().toLowerCase();
    renderMedia();
  });

  // grab-all section
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  currentTab = tab || null;
  fetchOtherTabs();   // fills in the "Other Pages (N)" label up front
  if (tab) {
    document.getElementById("grabAllBtn").addEventListener("click", () => grabAll(tab));
  }
  document.getElementById("grabCheckAll").addEventListener("click", () => {
    grabItems = grabItems.map((i) => ({ ...i, checked: true }));
    renderGrabChecklist();
  });
  document.getElementById("grabUncheckAll").addEventListener("click", () => {
    grabItems = grabItems.map((i) => ({ ...i, checked: false }));
    renderGrabChecklist();
  });
  const sendGrabSelected = () => {
    const resultEl = document.getElementById("grabResult");
    const selected = grabItems.filter((i) => i.checked).map(({ url, filename }) => ({ url, filename }));
    if (!selected.length) return;
    resultEl.textContent = `Sending ${selected.length} item(s) to Grabber…`;
    chrome.runtime.sendMessage(
      { type: "RELAY_BATCH", items: selected, pageUrl: tab.url },
      (resp) => {
        resultEl.textContent = resp && resp.ok
          ? `Sent ${(resp.job_ids || []).length} item(s) to Grabber ✓`
          : (resp && resp.error) || "Failed to send.";
        if (resp && resp.ok) {
          grabItems = [];
          renderGrabChecklist();
        }
      }
    );
  };
  document.getElementById("grabSendBtn").addEventListener("click", () => {
    const n = grabItems.filter((i) => i.checked).length;
    if (n > GRAB_CONFIRM_LIMIT) { showGrabConfirm(n, sendGrabSelected); return; }
    sendGrabSelected();
  });

  showTab("current");
})();
