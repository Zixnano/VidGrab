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
