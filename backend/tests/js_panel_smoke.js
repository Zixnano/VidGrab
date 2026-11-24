// Runs extension/companion-panel.js against a tiny fake DOM and a fake background, no browser needed.
const fs = require("fs");
const path = require("path");
const assert = require("assert");

class Node_ {
  constructor(tag) { this.tag = tag; this.children = []; this.attrs = {}; this.handlers = {}; this.style = {}; this._text = ""; this.nodeType = 1; this.parent = null; this.className = ""; this.tabIndex = 0;
    const self = this; this.classList = { toggle(c, on) { const s = new Set(self.className.split(" ").filter(Boolean)); (on === undefined ? !s.has(c) : on) ? s.add(c) : s.delete(c); self.className = [...s].join(" "); }, remove(c) { this.toggle(c, false); }, contains(c) { return self.className.split(" ").includes(c); } }; }
  append(...kids) { for (const k of kids) { if (typeof k === "string") { const t = new Node_("#text"); t.nodeType = 3; t._text = k; t.parent = this; this.children.push(t); } else { k.parent = this; this.children.push(k); } } }
  set textContent(v) { this.children = []; this._text = String(v); }
  get textContent() { return this._text + this.children.map((c) => c.textContent !== undefined ? c.textContent : c._text).join(""); }
  setAttribute(k, v) { this.attrs[k] = String(v); if (k === "id") ids[v] = this; if (k === "class") this.className = String(v); }
  getAttribute(k) { return this.attrs[k]; }
  addEventListener(t, fn) { (this.handlers[t] = this.handlers[t] || []).push(fn); }
  fire(t) { const ev = { target: this }; const chain = []; for (let n = this; n; n = n.parent) chain.unshift(n); for (const n of chain) for (const fn of n.handlers[t] || []) fn(ev); }   // ancestors first, like capture
  closest(sel) { let n = this; while (n) { if (n.tag === sel) return n; n = n.parent; } return null; }
  all(pred, out = []) { for (const c of this.children) { if (c.nodeType === 1) { if (pred(c)) out.push(c); c.all(pred, out); } } return out; }
  querySelectorAll(sel) { return this.all((n) => sel === "button" ? n.tag === "button" : sel === ".pview" ? n.classList.contains("pview") : false); }
}
const ids = {};
const mk = (tag, id) => { const n = new Node_(tag); if (id) n.setAttribute("id", id); return n; };
const nav = mk("div", "vgnav"), mount = mk("div", "vgviews"), mainView = mk("div", "mainView"), settingsView = mk("div", "settingsView"), cog = mk("button", "btnCog");
const other = mk("button", "nav-library"); nav.append(other);
const head = mk("head");
global.document = { getElementById: (id) => ids[id] || null, createElement: (t) => new Node_(t), createTextNode: (s) => ({ nodeType: 3, _text: String(s) }), head, addEventListener() {}, hidden: false };
global.performance = { now: () => Date.now() };

const calls = [];
let mode = "offline";
const sheet = "data:image/png;base64,AAAA";
const manifest = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const snapOn = { enabled: true, species: "puppy", stage: "kid", name: "Mochi", level: 9, xp: 10, xp_needed: 100, coins: 3, streak: 2, hunger: 70, energy: 60, affection: 90, pose: "idle", speech: "All caught up!", bubbles: true, animations: true, next_stage: { stage: "adult", level: 16 } };
global.window = {
  VGCORE: {}, VGCAnim: require(path.join(__dirname, "..", "extension", "companion-anim.js")),
  VGCHROME: { send: async (m) => { calls.push(m.type + (m.action ? ":" + m.action : ""));
    if (m.type === "COMPANION_STATE") return mode === "offline" ? { ok: false, offline: true, error: "off" } : { ok: true, data: mode === "off" ? { ...snapOn, enabled: false } : mode === "quiet" ? { ...snapOn, bubbles: false } : snapOn };
    if (m.type === "COMPANION_MANIFEST") return { ok: true, data: manifest };
    if (m.type === "COMPANION_SPRITE") return { ok: true, data: { dataUrl: sheet } };
    if (m.type === "COMPANION_ACTION") return m.action === "play" ? { ok: false, error: "Too sleepy to play right now" } : { ok: true, data: {} };
    return { ok: false }; } },
};
global.window.VGCAnim = window.VGCAnim; global.VGCORE = window.VGCORE;
Object.assign(global, { VGCAnim: window.VGCAnim });

(async () => {
  const src = fs.readFileSync(path.join(__dirname, "..", "extension", "companion-panel.js"), "utf8");
  new Function("window", "document", "performance", "setInterval", "clearInterval", src)(window, document, performance, setInterval, clearInterval);
  const btn = ids["nav-pal"], pane = ids["pane-pal"];
  assert(btn && pane, "nav button and pane were added");
  const tick = () => new Promise((r) => setTimeout(r, 30));

  btn.fire("click"); await tick();
  assert(pane.classList.contains("on") && mainView.style.display === "none", "opens and hides the main view");
  assert(pane.textContent.includes("not running"), "offline message, no invented progress");

  mode = "off"; btn.fire("click"); await tick();
  assert(pane.textContent.includes("Your companion is off"), "off state");
  const enable = pane.all((n) => n.tag === "button")[0]; enable.fire("click"); await tick();
  assert(calls.includes("COMPANION_ACTION:enable"), "enable action sent");

  mode = "on"; btn.fire("click"); await tick();
  const txt = pane.textContent;
  assert(txt.includes("Mochi the Kid") && txt.includes("Level 9") && txt.includes("grows up at level 16") && txt.includes("All caught up!"), "state rendered: " + txt.slice(0, 120));
  assert(calls.includes("COMPANION_SPRITE"), "sprite fetched through the background");
  const i = pane.all((n) => n.tag === "i" && n.style.backgroundImage);
  assert(i.length === 1 && i[0].style.backgroundSize === "512px 256px", "sprite sheet sized for 4x2 frames at 2x");
  const buttons = pane.all((n) => n.tag === "button");
  const play = buttons.find((b) => b.textContent === "Play"); play.fire("click"); await tick();
  assert(calls.includes("COMPANION_ACTION:play") && pane.textContent.includes("Too sleepy"), "action error is shown");

  // unchanged state must not rebuild the pane (a rebuild drops keyboard focus)
  const feedBefore = pane.all((n) => n.tag === "button").find((b) => b.textContent === "Feed");
  pane.all((n) => n.tag === "button").find((b) => b.textContent === "Pet").fire("click"); await tick();
  const feedAfter = pane.all((n) => n.tag === "button").find((b) => b.textContent === "Feed");
  assert(feedBefore === feedAfter, "unchanged snapshot does not rebuild the pane");

  // with speech bubbles OFF an action error must still be visible
  mode = "quiet"; btn.fire("click"); await tick();
  pane.all((n) => n.tag === "button").find((b) => b.textContent === "Play").fire("click"); await tick();
  assert(pane.textContent.includes("Too sleepy"), "error shown even with bubbles off");
  assert(!pane.textContent.includes("null"), "no stray null text");
  mode = "on"; btn.fire("click"); await tick();

  other.fire("click");
  assert(!pane.classList.contains("on") && btn.getAttribute("aria-selected") === "false", "steps aside when another tab is chosen");
  process.stdout.write("panel ok");
  process.exit(0);
})().catch((e) => { console.error(e); process.exit(1); });
