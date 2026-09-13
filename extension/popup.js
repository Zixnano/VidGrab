const BACKEND_BASE = "http://127.0.0.1:5757";

async function checkBackend() {
  const statusEl = document.getElementById("status");
  try {
    const res = await fetch(`${BACKEND_BASE}/health`);
    if (res.ok) {
      statusEl.textContent = "Desktop app connected ✓";
      statusEl.className = "ok";
      return true;
    }
    throw new Error("bad status");
  } catch (e) {
    statusEl.textContent = "Desktop app not running — launch it, then reopen this popup.";
    statusEl.className = "bad";
    return false;
  }
}

function render(videos, tab) {
  const list = document.getElementById("list");
  list.innerHTML = "";
  if (!videos.length) {
    list.innerHTML = '<div class="empty">No video traffic detected on this tab yet. Play the video first, or use the on-page "Download this video" button.</div>';
    return;
  }
  videos
    .sort((a, b) => b.timestamp - a.timestamp)
    .forEach((v) => {
      const div = document.createElement("div");
      div.className = "item";
      const urlSpan = document.createElement("span");
      urlSpan.className = "url";
      urlSpan.textContent = v.url.length > 90 ? v.url.slice(0, 90) + "…" : v.url;
      const btn = document.createElement("button");
      btn.textContent = "Send to Grabber";
      btn.addEventListener("click", () => {
        btn.textContent = "Sending…";
        chrome.runtime.sendMessage(
          { type: "RELAY_DOWNLOAD", url: v.url, pageUrl: tab.url, tabId: tab.id, filename: tab.title },
          (resp) => {
            btn.textContent = resp && resp.ok ? "Sent ✓" : (resp && resp.error) || "Failed";
          }
        );
      });
      div.appendChild(urlSpan);
      div.appendChild(btn);
      list.appendChild(div);
    });
}

(async () => {
  await checkBackend();
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  chrome.runtime.sendMessage({ type: "GET_VIDEOS", tabId: tab.id }, (resp) => {
    render((resp && resp.videos) || [], tab);
  });
})();
