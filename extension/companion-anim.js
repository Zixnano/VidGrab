// companion-anim.js: the same frame sequencing the desktop app uses (backend/companion/animation.py).
// Timing comes from the manifest ("animation"), so both sides stay in step. Pure, no DOM.
(function (root) {
  "use strict";
  const DEFAULTS = {
    fps: { idle: 4, content: 4, happy: 4, curious: 3, sleeping: 2, sad: 2, excited: 6, celebrating: 6, wobble: 5, cracked: 4 },
    bob: { idle: [900, 1], content: [900, 1], happy: [500, 1], curious: [700, 1], sleeping: [1600, 1], sad: [0, 0],
           excited: [350, 2], celebrating: [300, 3], wobble: [0, 0], cracked: [500, 1] },
    blink_period_ms: 4700,
    blink_hold_ms: 150,
  };

  function mergeCfg(animation) {
    const out = { fps: { ...DEFAULTS.fps }, bob: { ...DEFAULTS.bob },
                  blink_period_ms: DEFAULTS.blink_period_ms, blink_hold_ms: DEFAULTS.blink_hold_ms };
    if (animation) {
      if (animation.fps) Object.assign(out.fps, animation.fps);
      if (animation.bob) Object.assign(out.bob, animation.bob);
      if (animation.blink_period_ms) out.blink_period_ms = animation.blink_period_ms;
      if (animation.blink_hold_ms) out.blink_hold_ms = animation.blink_hold_ms;
    }
    return out;
  }

  function Animator(entry, animation) {
    this.entry = entry || {};
    this.cfg = mergeCfg(animation);
  }
  Animator.prototype.framesFor = function (pose) {
    const frames = this.entry.frames || {};
    const seq = frames[pose] || frames.idle || [0];
    return seq.map((n) => Number(n) | 0);
  };
  Animator.prototype.blinkActive = function (t) {
    const period = Math.max(1000, Number(this.cfg.blink_period_ms) | 0);
    const hold = Math.max(50, Number(this.cfg.blink_hold_ms) | 0);
    return ((t * 1000) % period) >= period - hold;
  };
  // returns { index, dy }: frame number in the sheet and the vertical bob in sprite pixels
  Animator.prototype.frame = function (pose, t) {
    const seq = this.framesFor(pose);
    const fps = Number(this.cfg.fps[pose]) || 4;
    let index = seq[Math.floor(t * fps) % seq.length];
    const blink = this.entry.blink;
    if (blink !== null && blink !== undefined && (pose === "idle" || pose === "content") && this.blinkActive(t)) index = Number(blink) | 0;
    const bob = this.cfg.bob[pose] || [0, 0];
    let dy = 0;
    if (bob[0] && bob[1]) dy = Math.floor((t * 1000) / bob[0]) % 2 ? (bob[1] | 0) : 0;
    return { index, dy };
  };
  function sheetCell(index, columns, size) {
    const cols = Math.max(1, columns | 0), s = size || 64;
    return { x: (index % cols) * s, y: Math.floor(index / cols) * s };
  }

  const api = { Animator, sheetCell };
  if (typeof module !== "undefined" && module.exports) module.exports = api; else root.VGCAnim = api;
})(typeof self !== "undefined" ? self : this);
