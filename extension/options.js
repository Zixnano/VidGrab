(async () => {
  const tokenInput = document.getElementById("token");
  const statusEl = document.getElementById("status");

  const { apiToken } = await chrome.storage.local.get("apiToken");
  if (apiToken) tokenInput.value = apiToken;

  const interceptCheckbox = document.getElementById("interceptDownloads");
  // Batch 3: the switch lives in vgSettings (vgsettings.js) — the same value
  // the popup's Downloads toggle edits, and the one background.js reads.
  // The old standalone "interceptDownloads" key is only a migration
  // fallback: if vgSettings has never carried the switch, adopt the legacy
  // value once (mirrors the migration in background.js).
  {
    const stored = await chrome.storage.local.get([VG_SETTINGS_KEY, "interceptDownloads"]);
    const rawVg = stored[VG_SETTINGS_KEY];
    if (!(rawVg && typeof rawVg.interceptDownloads === "boolean")
        && typeof stored.interceptDownloads === "boolean") {
      const adopted = vgNormalize({ ...(rawVg || {}), interceptDownloads: stored.interceptDownloads });
      try { await chrome.storage.local.set({ [VG_SETTINGS_KEY]: adopted }); } catch (e) {}
      interceptCheckbox.checked = adopted.interceptDownloads;
    } else {
      interceptCheckbox.checked = !!vgNormalize(rawVg).interceptDownloads;
    }
  }
  interceptCheckbox.addEventListener("change", async () => {
    const cur = vgNormalize((await chrome.storage.local.get(VG_SETTINGS_KEY))[VG_SETTINGS_KEY]);
    await chrome.storage.local.set({
      [VG_SETTINGS_KEY]: { ...cur, interceptDownloads: interceptCheckbox.checked },
    });
  });
  // v4.4: the click-required switch and the ignore list live in vgSettings too.
  const gestureCheckbox = document.getElementById("interceptNeedsGesture");
  const ignoreInput = document.getElementById("interceptIgnore");
  {
    const cur = vgNormalize((await chrome.storage.local.get(VG_SETTINGS_KEY))[VG_SETTINGS_KEY]);
    gestureCheckbox.checked = cur.interceptNeedsGesture;
    ignoreInput.value = cur.interceptIgnore;
  }
  async function saveVg(patch) {
    const cur = vgNormalize((await chrome.storage.local.get(VG_SETTINGS_KEY))[VG_SETTINGS_KEY]);
    await chrome.storage.local.set({ [VG_SETTINGS_KEY]: vgNormalize({ ...cur, ...patch }) });
  }
  gestureCheckbox.addEventListener("change", () => saveVg({ interceptNeedsGesture: gestureCheckbox.checked }));
  ignoreInput.addEventListener("change", () => saveVg({ interceptIgnore: ignoreInput.value }));
  // Keep this page honest if the popup toggle flips the switch meanwhile.
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area === "local" && changes[VG_SETTINGS_KEY]) {
      const nv = vgNormalize(changes[VG_SETTINGS_KEY].newValue);
      interceptCheckbox.checked = !!nv.interceptDownloads;
      gestureCheckbox.checked = nv.interceptNeedsGesture;
      if (document.activeElement !== ignoreInput) ignoreInput.value = nv.interceptIgnore;
    }
  });

  const blobHookCheckbox = document.getElementById("blobHookEnabled");
  const { blobHookEnabled } = await chrome.storage.local.get("blobHookEnabled");
  blobHookCheckbox.checked = blobHookEnabled !== false;
  blobHookCheckbox.addEventListener("change", async () => {
    await chrome.storage.local.set({ blobHookEnabled: blobHookCheckbox.checked });
  });

  document.getElementById("test").addEventListener("click", async () => {
    statusEl.textContent = "Contacting app…";
    statusEl.className = "";
    try {
      const ping = await fetch("http://127.0.0.1:5757/ping");
      if (ping.status === 404) {
        statusEl.textContent = "App reachable but outdated — update Video Grabber, then retry.";
        statusEl.className = "bad";
        return;
      }
      if (!ping.ok) throw new Error("no response");
      const state = await ping.json().catch(() => ({}));
      if (state.paired) {
        if (!tokenInput.value.trim()) {
          statusEl.textContent = "App is paired, but this extension has no token — copy it from the app's Tools menu.";
          statusEl.className = "bad";
          return;
        }
        statusEl.textContent = "App reachable ✓ — already paired this session.";
        statusEl.className = "ok";
        return;
      }
      const res = await fetch("http://127.0.0.1:5757/pair", { method: "POST" });
      if (res.ok) {
        const data = await res.json();
        if (data.token) {
          await chrome.storage.local.set({ apiToken: data.token });
          tokenInput.value = data.token;
          statusEl.textContent = "Paired ✓";
          statusEl.className = "ok";
          return;
        }
      }
      statusEl.textContent = "App reachable but pairing refused — restart the app to re-pair.";
      statusEl.className = "bad";
    } catch (e) {
      statusEl.textContent = "App unreachable — is Video Grabber running?";
      statusEl.className = "bad";
    }
  });

  // v5: show the connection state as soon as the page opens (read-only:
  // /ping never pairs, so this can't use up the once-per-session pairing).
  try {
    const ping = await fetch("http://127.0.0.1:5757/ping");
    if (ping.ok) {
      const state = await ping.json().catch(() => ({}));
      const paired = state.paired || !!tokenInput.value.trim();
      statusEl.textContent = paired
        ? "App running" + (state.version ? ` (v${state.version})` : "") + (tokenInput.value.trim() ? " ✓" : " but this extension has no token. Press Test connection.")
        : "App running, not paired yet. Press Test connection.";
      statusEl.className = paired && tokenInput.value.trim() ? "ok" : "bad";
    }
  } catch (e) {
    statusEl.textContent = "App not running. Launch Video Grabber, then press Test connection.";
    statusEl.className = "bad";
  }

  document.getElementById("save").addEventListener("click", async () => {
    const value = tokenInput.value.trim();
    if (!value) {
      statusEl.textContent = "Paste a token first.";
      statusEl.className = "bad";
      return;
    }
    await chrome.storage.local.set({ apiToken: value });
    statusEl.textContent = "Saved ✓ — try downloading something.";
    statusEl.className = "ok";
  });
})();
