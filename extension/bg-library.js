// bg-library.js (v4.5.1, M1): remembers sniffed media across tabs and sessions.
// Loaded by background.js after bg-features.js. Stores {host: [{url, filename, title,
// size, contentType, firstSeen, lastSeen}]} under vgSniffedLibrary, pruned at 30 days
// and capped at 200 entries (VGCORE.libraryMerge). Only video, audio and stream items
// are kept; images, documents and segments are not.
(function () {
  "use strict";
  const B = self.VGBG, C = self.VGCORE, L = self.VGLOG;
  if (!B || !C || !L || !B.store) return;
  const KEY = "vgSniffedLibrary", KEPT = { video: 1, audio: 1, stream: 1 };
  let pending = new Map(), timer = null;

  // Writes are debounced: a page that sniffs a burst of items causes one write.
  // All writes (flush and forget) use the per-key lock, so a prune can never
  // interleave with a Forget.
  async function flush() {
    timer = null;
    const batch = Array.from(pending.values()); pending = new Map();
    if (!batch.length) return;
    try {
      // v4.5.2: bounded, so a stalled storage write can't hang the worker.
      await Promise.race([
        B.store.update(KEY, {}, (lib) => C.libraryMerge(lib, batch, Date.now())),
        new Promise((_, rej) => setTimeout(() => rej(new Error("library write timed out")), 5000)),
      ]);
    }
    catch (e) { L.warn("library", "write failed", e); }
  }
  B.flushLibrary = flush;

  B.listen("item", async ({ tabId, item }) => {
    try {
      if (!item || !item.url || !/^https?:/i.test(item.url)) return;
      if (!KEPT[C.kindOf(item)]) return;
      if (/^(ts|m4s|m2ts|mts)$/.test(C.extOf(item.url))) return;
      const t = await B.tabInfo(tabId);
      pending.set(item.url, { url: item.url, filename: B.decodeName(item.url), title: t.title || "", size: item.size || 0, contentType: item.contentType || "" });
      if (!timer) timer = setTimeout(flush, 500);
    } catch (e) { L.warn("library", "item handler failed", e); }
  });

  B.on("LIB_FORGET", async (msg) => {
    const spec = { url: msg.url ? String(msg.url) : "", host: msg.host ? String(msg.host).toLowerCase() : "" };
    if (!spec.url && !spec.host) return { ok: false, error: "Nothing to forget." };
    // A pending (not yet written) copy of the forgotten entry must not come back.
    for (const [u, e] of Array.from(pending)) if (u === spec.url || (spec.host && C.hostOf(e.url) === spec.host)) pending.delete(u);
    let removed = 0;
    await B.store.update(KEY, {}, (lib) => { const before = Object.values(lib || {}).flat().length; const out = C.libraryForget(lib, spec); removed = before - Object.values(out).flat().length; return out; });
    return { ok: true, removed };
  });
  L.info("library", "bg-library ready");
})();
