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
