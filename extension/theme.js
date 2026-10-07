// theme.js: shared by popup.html and options.html. Paints the saved theme
// immediately (no flash), then refreshes it from the app and saves it so the
// in-page pill can use it too. Falls back to the built-in Handheld colors.
(() => {
  const BASE = "http://127.0.0.1:5757";
  const MAP = {
    bg_base: "--bg", bg_panel: "--panel", bg_elevated: "--elev", bg_sel: "--sel",
    bg_hover: "--hover", border: "--bd", border_hi: "--bd-hi", text: "--tx",
    text_soft: "--tx-soft", text_muted: "--mu", text_dim: "--dim", accent: "--ac",
    accent_dim: "--ac-dim", text_on_accent: "--on-ac", success: "--ok",
    warning: "--warn", error: "--err", info: "--info", radius: "--r", font: "--font",
  };
  function apply(t) {
    if (!t) return;
    const root = document.documentElement.style;
    for (const [k, v] of Object.entries(MAP)) if (t[k]) root.setProperty(v, t[k]);
  }
  async function load() {
    try {
      const { theme, apiToken } = await chrome.storage.local.get(["theme", "apiToken"]);
      apply(theme);
      if (!apiToken) return;
      const r = await fetch(BASE + "/theme", { headers: { "X-API-Token": apiToken } });
      if (!r.ok) return;
      const fresh = await r.json();
      apply(fresh);
      await chrome.storage.local.set({ theme: fresh });
    } catch (e) { /* app not running: keep what we painted */ }
  }
  load();
})();
