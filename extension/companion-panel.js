// companion-panel.js: adds a "Pal" tab to the popup and side panel. Load it after panel.js and
// companion-anim.js. It does not edit panel.js: it adds its own nav button and pane to the same
// containers and hides itself when any other tab is chosen. Every value from the app goes in with
// textContent, never innerHTML. All data comes from the background worker (COMPANION_* messages);
// when the app is off or unreachable the tab says so and never invents progress.
(function () {
  "use strict";
  const C = window.VGCORE, X = window.VGCHROME, A = window.VGCAnim;
  if (!C || !X || !A) return;
  const $ = (id) => document.getElementById(id);
  const nav = $("vgnav"), mount = $("vgviews");
  if (!nav || !mount) return;                              // panel.js has not built the shell

  function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    for (const k of Object.keys(attrs || {})) {
      const v = attrs[k];
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
  const send = (msg) => X.send(msg);

  // ----------------------------------------------------------------- styles --
  const style = document.createElement("style");
  style.textContent = [
    ".vgc-stage{display:flex;flex-direction:column;align-items:center;padding:10px 6px 4px;border:1px solid var(--bd);background:var(--panel);border-radius:var(--r);margin-bottom:8px}",
    ".vgc-sprite{width:128px;height:134px;position:relative;overflow:hidden}",
    ".vgc-sprite i{position:absolute;left:0;top:3px;width:128px;height:128px;background-repeat:no-repeat;image-rendering:pixelated}",
    ".vgc-name{font-size:14px;font-weight:700;margin:2px 0 0}",
    ".vgc-sub{font-size:11px;color:var(--mu);margin:2px 0 6px;text-align:center}",
    ".vgc-say{font-size:12px;padding:6px 10px;border:1px solid var(--bd-hi,var(--bd));border-radius:12px;background:var(--elev);margin:4px 0 2px;max-width:100%;text-align:center}",
    ".vgc-need{display:grid;grid-template-columns:70px 1fr 28px;gap:6px;align-items:center;font-size:11px;color:var(--mu);margin:3px 0}",
    ".vgc-need b{font-weight:400;text-align:right}",
    ".vgc-acts{display:flex;gap:5px;margin:8px 0}",
    ".vgc-acts button{flex:1}",
    ".vgc-note{font-size:11px;color:var(--mu);margin-top:6px}",
  ].join("\n");
  document.head.append(style);

  // ------------------------------------------------------------- nav + pane --
  const btn = h("button", { id: "nav-pal", role: "tab", "aria-selected": "false", "aria-controls": "pane-pal", tabindex: "0", text: "Pal", onclick: open });
  nav.append(btn);
  const pane = h("section", { id: "pane-pal", class: "pview", role: "tabpanel", "aria-labelledby": "nav-pal" });
  mount.append(pane);

  let animTimer = null, pollTimer = null, t0 = performance.now();
  let manifest = null, snap = null, sprite = { name: "", url: "", entry: null }, animator = null, animKey = "";
  let spriteEl = null, toastText = "", lastSig = null;

  function open() {
    for (const b of nav.querySelectorAll("button")) { const on = b === btn; b.setAttribute("aria-selected", on ? "true" : "false"); b.tabIndex = on ? 0 : -1; }
    if ($("mainView")) $("mainView").style.display = "none";
    if ($("settingsView")) $("settingsView").style.display = "none";
    for (const p of mount.querySelectorAll(".pview")) p.classList.toggle("on", p === pane);
    start();
  }
  function stop() {
    clearInterval(animTimer); clearInterval(pollTimer); animTimer = pollTimer = null;
    pane.classList.remove("on");
    btn.setAttribute("aria-selected", "false");
  }
  // Another tab (or the settings cog) was chosen: panel.js shows its own pane, we step aside.
  nav.addEventListener("click", (e) => { const b = e.target.closest("button"); if (b && b !== btn) stop(); }, true);
  nav.addEventListener("keydown", (e) => { if ((e.key === "ArrowLeft" || e.key === "ArrowRight") && pane.classList.contains("on")) stop(); }, true);
  const cog = $("btnCog");
  if (cog) cog.addEventListener("click", stop);
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) { clearInterval(animTimer); clearInterval(pollTimer); animTimer = pollTimer = null; }
    else if (pane.classList.contains("on")) start();
  });

  function start() {
    t0 = performance.now();
    refresh(true);
    clearInterval(pollTimer);
    pollTimer = setInterval(() => refresh(false), 5000);
  }
  function startAnim() {
    clearInterval(animTimer);
    if (snap && snap.enabled && snap.animations !== false) animTimer = setInterval(paint, 100);
  }

  // ---------------------------------------------------------------- loading --
  async function refresh(force) {
    const [s, m] = await Promise.all([send({ type: "COMPANION_STATE" }), manifest ? Promise.resolve({ ok: true, data: manifest }) : send({ type: "COMPANION_MANIFEST" })]);
    if (!s || !s.ok) { lastSig = null; render({ error: (s && s.error) || "Could not reach the app.", offline: !!(s && s.offline) }); return; }
    if (m && m.ok) manifest = m.data;
    snap = s.data;
    const sig = JSON.stringify(snap) + "|" + (manifest ? 1 : 0) + "|" + (sprite.url ? 1 : 0);
    if (!force && !toastText && sig === lastSig) return;     // nothing changed: do not rebuild the pane
    lastSig = sig;
    if (snap.enabled && manifest) await ensureSprite();
    render();
  }

  async function ensureSprite() {
    const stage = snap.stage;
    const entry = stage === "egg" ? manifest.egg : (manifest.species || []).find((x) => x.id === snap.species) || (manifest.species || [])[0];
    if (!entry) return;
    if (sprite.name !== entry.sheet || !sprite.url) {
      const r = await send({ type: "COMPANION_SPRITE", name: entry.sheet });
      if (r && r.ok) sprite = { name: entry.sheet, url: r.data.dataUrl, entry };
    }
    sprite.entry = entry;
    const key = snap.species + "|" + stage;
    if (key !== animKey) { animator = new A.Animator(entry, manifest.animation); animKey = key; }
  }

  // --------------------------------------------------------------- rendering --
  function need(label, value) {
    return h("div", { class: "vgc-need" }, h("span", { text: label }), h("div", { class: "bar" }, h("i", { style: "width:" + Math.max(0, Math.min(100, value)) + "%" })), h("b", { text: String(value) }));
  }

  function render(problem) {
    pane.textContent = "";
    spriteEl = null;
    clearInterval(animTimer); animTimer = null;
    if (problem) {
      toastText = "";
      pane.append(h("div", { class: "pmsg warn", text: problem.offline ? "The Video Grabber app is not running, so your pal is asleep." : problem.error }));
      return;
    }
    if (!snap.enabled) {
      pane.append(h("div", { class: "vgc-stage" },
        h("div", { class: "vgc-name", text: "Your companion is off" }),
        h("div", { class: "vgc-sub", text: "Turn it on to hatch an egg, earn XP from downloads, and play mini-games on the handheld." }),
        h("button", { class: "pbtn pri", text: "Turn on companion", onclick: () => act("enable") })));
      return;
    }
    spriteEl = h("i", {});
    const nxt = snap.next_stage;
    pane.append(
      h("div", { class: "vgc-stage" },
        h("div", { class: "vgc-sprite" }, spriteEl),
        h("div", { class: "vgc-name", text: snap.name + " the " + snap.stage[0].toUpperCase() + snap.stage.slice(1) }),
        h("div", { class: "vgc-sub", text: "Level " + snap.level + "  \u2022  " + snap.coins + " treats  \u2022  " + snap.streak + "-day streak" + (nxt ? "  \u2022  " + (snap.stage === "egg" ? "hatches" : "grows up") + " at level " + nxt.level : "") }),
        (toastText || snap.bubbles) ? h("div", { class: "vgc-say", text: toastText || snap.speech }) : null,
        h("div", { class: "bar", style: "width:90%" }, h("i", { style: "width:" + Math.round(100 * snap.xp / Math.max(1, snap.xp_needed)) + "%" })),
        h("div", { class: "vgc-sub", text: snap.xp + " / " + snap.xp_needed + " XP" })),
      need("Hunger", snap.hunger), need("Energy", snap.energy), need("Affection", snap.affection),
      h("div", { class: "vgc-acts" },
        h("button", { class: "pbtn", text: "Feed", onclick: () => act("feed") }),
        h("button", { class: "pbtn", text: "Play", onclick: () => act("play") }),
        h("button", { class: "pbtn", text: "Pet", onclick: () => act("pet") }),
        h("button", { class: "pbtn", text: "Nap", onclick: () => act("nap") })),
      h("div", { class: "vgc-note", text: "Finish downloads in the app to earn XP. Play mini-games from the Companion menu: they use the handheld's D-pad." }));
    if (!manifest || !sprite.url) pane.append(h("div", { class: "pmsg warn", text: "Artwork is not available right now. Your pal's stats still update." }));
    toastText = "";
    paint();
    startAnim();
  }

  function paint() {
    if (!spriteEl || !snap || !animator || !sprite.url || !manifest) return;
    const fs = manifest.frame_size || 64, sc = 2, entry = sprite.entry;
    const animate = snap.animations !== false;
    const f = animator.frame(snap.pose, animate ? (performance.now() - t0) / 1000 : 0);
    const cell = A.sheetCell(f.index, entry.columns, fs);
    spriteEl.style.backgroundImage = "url(" + sprite.url + ")";
    spriteEl.style.backgroundSize = (entry.columns * fs * sc) + "px " + (entry.rows * fs * sc) + "px";
    spriteEl.style.backgroundPosition = (-cell.x * sc) + "px " + (-cell.y * sc) + "px";
    spriteEl.style.transform = "translateY(" + (animate ? -f.dy * sc : 0) + "px)";
  }

  async function act(action) {
    const r = await send({ type: "COMPANION_ACTION", action });
    if (!r || !r.ok) toastText = (r && (r.error || (r.data && r.data.error))) || "Not now.";
    await refresh();
  }
})();
