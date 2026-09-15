(async () => {
  const tokenInput = document.getElementById("token");
  const statusEl = document.getElementById("status");

  const { apiToken } = await chrome.storage.local.get("apiToken");
  if (apiToken) tokenInput.value = apiToken;

  const interceptCheckbox = document.getElementById("interceptDownloads");
  const { interceptDownloads } = await chrome.storage.local.get("interceptDownloads");
  interceptCheckbox.checked = !!interceptDownloads;
  interceptCheckbox.addEventListener("change", async () => {
    await chrome.storage.local.set({ interceptDownloads: interceptCheckbox.checked });
  });

  document.getElementById("test").addEventListener("click", async () => {
    statusEl.textContent = "Contacting app…";
    statusEl.className = "";
    try {
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
      statusEl.textContent = "App unreachable or pairing already granted this session.";
      statusEl.className = "bad";
    } catch (e) {
      statusEl.textContent = `Error: ${e}`;
      statusEl.className = "bad";
    }
  });

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
