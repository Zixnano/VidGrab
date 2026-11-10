# AUDIT.md — v4.0.1 tree audit (read-only pass)

## Gate results (re-run on the exact v4.0.1 zip contents)
```
py_compile backend modules:      10/10 PASS
node --check extension js:       4/4 PASS (background, content, popup, options)
smell grep (TODO/FIXME/XXX/HACK/print():  clean — zero hits
AST sweep:                       previously verified clean for v4.0.1 (no backend
                                 module changed since that sweep)
```

## Consistency cross-checks
| Check | Result |
|---|---|
| ApiClient paths vs api.py routes | ✅ all resolve ( `{jid}` == `<job_id>` placeholder syntax only) |
| Extension message types vs background handlers | ✅ every sent type has a handler |
| requirements.txt vs runtime imports | ✅ complete (flask, flask-cors, yt-dlp, requests, PySide6) + build/test deps |
| popup/options backend paths | ✅ (/pair, /ping for pairing; root ping for health) |

## Findings

### F1 — MEDIUM: recorder notifications not actually permitted (confidence: high that behavior is wrong-or-accidental)
`content.js` `recNotify()` uses the page-level Web `Notification` API. The
extension manifest has **no `"notifications"` permission**, so whether
anything appears depends on *each site's* notification permission, not the
extension's — and most sites are never granted it. In practice Session R's
R.6 notifications will usually do nothing.
**Options:** (a) add `"notifications"` to manifest permissions and switch
`recNotify` to `chrome.notifications.create`; (b) accept site-permission
dependence and document. — **QUESTION for you** (see QUESTIONS.md).

### F2 — RETRACTED in v4.0.2: handlers were LIVE (audit regex miss)
The original claim — `RELAY_BATCH`, `RELAY_DOWNLOAD`, `SAVE_RECORDING`
dead — was wrong for the first two: the sender-detection regex required
`{` immediately after `sendMessage(` and missed multi-line call sites.
Live senders: popup.js RELAY_BATCH ×2 (media-checklist send, Grab all
media), popup.js + content.js RELAY_DOWNLOAD. Both handlers restored
during the v4.0.2 gate. Only `SAVE_RECORDING` was truly dead (removed).

### F3 — LOW: stranded dead global in downloader.py (confidence: high — known)
`_YTDLP_JS_RUNTIME_CACHE = None` at module level; its only consumer moved to
engines.py in Session 7 (which has its own init). Harmless.
**Fix on approval:** delete the line.

### F4 — LOW (informational): 15 silent `except: pass` blocks
Contexts reviewed (downloader 5, gui_qt 6, logging_setup 1, server 2,
settings 1): all are intentional best-effort paths (tray icon, open-folder-
on-complete, PyPI version check, UI refresh fallbacks). No change proposed.

### F5 — OPEN product question carried from v4.0.0: R.7 vs R.8 naming
Recorder saves `.webm` (R.7 pattern, correct container) vs R.8's `.mp4`
pattern. Still needs your ruling; KNOWN_ISSUES.md documents it.

### F6 — DOCUMENTED BY DESIGN: `_job_dest` disambiguation race
Download-start disambiguation covers sequential starts; simultaneous
same-name starts can still race. Registry reservation noted as future work.

## Fixed during this pass (Task 1 — not part of the audit findings)
- `.github/workflows/build-exe.yml`: added **Download and bundle Deno**
  step (mirrors the ffmpeg step: download release zip → expand → copy
  `deno.exe` into `backend/`), placed above **Build exe**.
- PyInstaller line converged byte-for-byte to the specified command
  (yt_dlp + PySide6 + streamlink collect-alls; ffmpeg/ffprobe/deno
  add-binaries; no tkinterdnd2).

## QUESTIONS.md
1. **F1:** switch recorder notifications to `chrome.notifications` (+ add
   the manifest permission)? Or keep Web Notifications and document the
   site-permission caveat?
2. **F2/F3:** prune the three dead handlers and the dead global?
3. Deno step currently tracks `releases/latest` — pin a version instead
   for reproducible builds?
