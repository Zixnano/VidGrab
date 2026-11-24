// bg-companion.js: companion pet messages for the panel. Loaded by background.js after bg-library.js:
//   try { importScripts("bg-companion.js"); } catch (e) { VGLOG.error("boot", "bg-companion.js failed to load", e); }
// The token never leaves the background worker. The panel only sends the four message types below.
(function () {
  "use strict";
  const B = self.VGBG, C = self.VGCORE, L = self.VGLOG;
  if (!B || !C || !L) {
    try { console.warn("bg-companion.js: VGBG/VGCORE/VGLOG are not ready, companion messages are disabled. It must load after bg-library.js."); } catch (e) { /* ignore */ }
    return;
  }

  const ACTIONS = new Set(["pet", "feed", "play", "nap", "enable", "disable"]);
  const SPRITE_NAME = /^[a-z0-9_-]+\.png$/i;
  const spriteCache = new Map();          // sheet name -> data URL (sheets are small and never change)
  let manifest = null, manifestAt = 0;

  async function app(path, opts) {
    opts = opts || {};
    if (!(await B.checkBackend())) return { ok: false, offline: true, error: "Video Grabber app isn't running." };
    try {
      const { res, unpaired } = await B.authedFetch(path, {
        method: opts.method || "GET",
        headers: opts.body ? { "Content-Type": "application/json" } : {},
        body: opts.body ? JSON.stringify(opts.body) : undefined,
      });
      if (unpaired) return { ok: false, error: "Not paired yet.", code: "not_paired" };
      const data = await res.json().catch(() => ({}));
      if (!res.ok) return { ok: false, status: res.status, data, error: (data && data.error) || ("HTTP " + res.status) };
      return { ok: true, status: res.status, data };
    } catch (e) {
      const c = C.classifyError(e);
      return { ok: false, error: c.message, code: c.code };
    }
  }

  B.on("COMPANION_STATE", async () => app("/companion"));

  B.on("COMPANION_MANIFEST", async () => {
    if (manifest && Date.now() - manifestAt < 300000) return { ok: true, data: manifest };
    const r = await app("/companion/manifest");
    if (r.ok) { manifest = r.data; manifestAt = Date.now(); }
    return r;
  });

  B.on("COMPANION_ACTION", async (msg) => {
    const action = String((msg && msg.action) || "");
    if (!ACTIONS.has(action)) return { ok: false, error: "Unknown action." };
    return app("/companion/action", { method: "POST", body: { action } });
  });

  B.on("COMPANION_SPRITE", async (msg) => {
    const name = String((msg && msg.name) || "");
    if (!SPRITE_NAME.test(name)) return { ok: false, error: "Bad sprite name." };
    if (spriteCache.has(name)) return { ok: true, data: { dataUrl: spriteCache.get(name) } };
    if (!(await B.checkBackend())) return { ok: false, offline: true, error: "Video Grabber app isn't running." };
    try {
      const { res, unpaired } = await B.authedFetch("/companion/sprite/" + encodeURIComponent(name));
      if (unpaired) return { ok: false, error: "Not paired yet.", code: "not_paired" };
      if (!res.ok) return { ok: false, error: "HTTP " + res.status };
      const bytes = new Uint8Array(await res.arrayBuffer());
      let bin = "";
      for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
      const dataUrl = "data:image/png;base64," + btoa(bin);
      spriteCache.set(name, dataUrl);
      return { ok: true, data: { dataUrl } };
    } catch (e) {
      const c = C.classifyError(e);
      return { ok: false, error: c.message, code: c.code };
    }
  });
})();
