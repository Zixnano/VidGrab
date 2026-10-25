// vgsettings.js: shared extension settings (v4.3, extended in v4.4). Loaded by background.js
// (importScripts), content.js (manifest content_scripts) and popup.html.
// Stored in chrome.storage.local under VG_SETTINGS_KEY. Always read through
// vgNormalize() so a missing or damaged value falls back to the defaults.
const VG_SETTINGS_KEY = "vgSettings";
const VG_DEFAULTS = {
  pillVisible: true,       // show the pill over videos
  pillCorner: "",          // default corner for sites with no saved position: "" (free) | tl | tr | bl | br
  microPill: true,         // image hover micro-pill
  microPillPos: "tr",      // Batch 3: corner the micro-pill sits on: tr (the original spot) | tl | br | bl
  microPillSize: false,    // Batch 3: show the file size in the micro-pill menu (fetched when it opens)
  defaultQuality: 0,       // 0 = auto (best); else preferred height (e.g. 1080) when a site has no remembered pick
  maxMenuHeight: 0,        // 0 = no cap; else max dropdown height in px (menu scrolls inside)
  snifferMode: "all",      // "all" tabs | "active" tab only
  notifyRecording: true,   // desktop notifications for recordings
  notifyActions: true,     // desktop notifications for shortcuts and right-click sends
  // Batch 3: hand every browser download to the app. THE one source of truth
  // for the interceptor: the old standalone "interceptDownloads" storage key is
  // read once as a migration fallback (background.js, options.js) and never
  // written again.
  interceptDownloads: false,
  oneClickMode: false,      // split pill: left half sends immediately, menu arrow hidden
  // v4.4: only intercept a download that follows a real click / key press /
  // right-click on a page (or a shortcut / menu action). Downloads the
  // browser starts by itself, such as session restore, resumed downloads and
  // page scripts firing on load, are left alone. This is what stops a
  // startup flood. Turn off only if you start downloads from the browser UI
  // (address bar) and want those caught too.
  interceptNeedsGesture: true,
  // v4.4: never intercept these. Hosts ("example.com" also matches its
  // subdomains) and file extensions (".exe"), separated by spaces, commas or
  // new lines.
  interceptIgnore: "",
};
function vgNormalize(raw) {
  const r = (raw && typeof raw === "object") ? raw : {};
  const d = VG_DEFAULTS;
  const bool = (k) => (typeof r[k] === "boolean" ? r[k] : d[k]);
  const pick = (k, allowed) => (allowed.includes(r[k]) ? r[k] : d[k]);
  const num = (k, min, max) => {
    const n = (typeof r[k] === "number") ? r[k] : NaN;
    return Number.isFinite(n) ? Math.min(max, Math.max(min, Math.round(n))) : d[k];
  };
  const str = (k, max) => (typeof r[k] === "string" ? r[k].slice(0, max) : d[k]);
  return {
    pillVisible: bool("pillVisible"),
    pillCorner: pick("pillCorner", ["", "tl", "tr", "bl", "br"]),
    microPill: bool("microPill"),
    microPillPos: pick("microPillPos", ["tr", "tl", "br", "bl"]),
    microPillSize: bool("microPillSize"),
    defaultQuality: num("defaultQuality", 0, 4320),
    maxMenuHeight: num("maxMenuHeight", 0, 2000),
    snifferMode: pick("snifferMode", ["all", "active"]),
    notifyRecording: bool("notifyRecording"),
    notifyActions: bool("notifyActions"),
    interceptDownloads: bool("interceptDownloads"),
    oneClickMode: bool("oneClickMode"),
    interceptNeedsGesture: bool("interceptNeedsGesture"),
    interceptIgnore: str("interceptIgnore", 2000),
  };
}
