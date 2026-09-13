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

// Scans the page DOM for downloadable-looking links and media sources.
// Runs inside the page via chrome.scripting.executeScript — keep it
// self-contained (no outside references).
function scanPageForDownloads() {
  const EXT_RE = /\.(mp4|m4v|mov|webm|mkv|avi|mp3|wav|flac|m4a|zip|rar|7z|pdf|docx?|pptx?|exe|msi)(\?|#|$)/i;
  const found = new Map();
  document.querySelectorAll("a[href]").forEach((a) => {
    if (EXT_RE.test(a.href)) found.set(a.href, a.textContent.trim() || a.href);
  });
  document.querySelectorAll("video, audio").forEach((el) => {
    if (el.currentSrc && !el.currentSrc.startsWith("blob:")) found.set(el.currentSrc, document.title);
    el.querySelectorAll("source[src]").forEach((s) => {
      if (s.src && !s.src.startsWith("blob:")) found.set(s.src, document.title);
    });
  });
  return Array.from(found.entries()).map(([url, filename]) => ({ url, filename }));
}

async function grabAll(tab) {
  const resultEl = document.getElementById("grabResult");
  resultEl.textContent = "Scanning page…";
  let results;
  try {
    [{ result: results }] = await chrome.scripting.executeScript({
      target: { tabId: tab.id, allFrames: true },
      func: scanPageForDownloads,
    });
  } catch (e) {
    resultEl.textContent = "Couldn't scan this page.";
    return;
  }
  if (!results || !results.length) {
    resultEl.textContent = "No downloadable links found on this page.";
    return;
  }
  resultEl.textContent = `Found ${results.length} item(s) — sending to Grabber…`;
  chrome.runtime.sendMessage(
    { type: "RELAY_BATCH", items: results, pageUrl: tab.url },
    (resp) => {
      resultEl.textContent = resp && resp.ok
        ? `Sent ${resp.job_ids.length} item(s) to Grabber ✓`
        : (resp && resp.error) || "Failed to send.";
    }
  );
}

(async () => {
  await checkBackend();
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  chrome.runtime.sendMessage({ type: "GET_VIDEOS", tabId: tab.id }, (resp) => {
    render((resp && resp.videos) || [], tab);
  });
  document.getElementById("grabAllBtn").addEventListener("click", () => grabAll(tab));
})();
