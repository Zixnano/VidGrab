# Video Grabber — extension + desktop app

A two-part downloader: a Chrome extension that finds video on any page
(including the in-page "Download this video" hover button you wanted), and a
local desktop app that does the actual downloading with yt-dlp so large
files never have to pass through the browser's memory.

```
video-grabber/
├── extension/        Chrome (Manifest V3) extension — load unpacked
│   ├── manifest.json
│   ├── background.js   network sniffing + relays downloads to the app
│   ├── content.js      the on-page hover button (direct-download or record)
│   ├── popup.html/js   manual list of everything sniffed on the current tab
└── backend/           Desktop companion app (Python)
    ├── server.py        local HTTP server + yt-dlp + Tkinter window
    ├── requirements.txt
    └── build_exe.md     how to freeze it into VideoGrabber.exe
```

## Setup (development / using it yourself right now)

1. **Backend**: `cd backend && pip install -r requirements.txt && python server.py`
   A small window opens, listening on `http://127.0.0.1:5757`. Leave it running.
2. **Extension**: open `chrome://extensions`, enable Developer Mode, "Load
   unpacked", select the `extension/` folder.
3. Browse normally. When a page has a playable `<video>`, a small pill
   appears over it:
   - **"Download this video"** (green) — the video has a real network URL.
     Clicking sends it to the backend, which runs yt-dlp with the page's
     cookies/referer/user-agent attached, merges audio+video, and saves to
     `~/Downloads/VideoGrabber`.
   - **"Record this video"** (red) — the video is playing from a `blob:` URL
     (MSE-based players), which isn't a downloadable file. Clicking starts
     `MediaRecorder` capture; click again to stop and save what was
     captured so far as a `.webm`.
4. The toolbar popup also lists everything the network sniffer has seen on
   the current tab, in case the hover button doesn't appear (e.g. video
   inside a same-origin iframe the content script didn't get positioned
   over correctly) — "Send to Grabber" does the same handoff.

## To get an actual `VideoGrabber.exe`

PyInstaller builds are platform-specific, so it has to be built on Windows.
Full steps are in `backend/build_exe.md`. Short version:

```
pip install -r requirements.txt
pyinstaller --onefile --noconsole --name VideoGrabber --collect-all yt_dlp server.py
```

## What this will and won't do (being straight with you, same as before)

- **DRM'd content (Netflix, Disney+, most paid streaming platforms)**: won't
  work, on purpose. Those streams are encrypted with Widevine/PlayReady;
  there's no legal or reliable technical way for a general tool to decrypt
  them, and I'm not going to build in DRM circumvention.
- **Direct `.mp4`/`.m3u8`/`.mpd` links**: this is the strong path. The
  sniffer + yt-dlp combo handles the large majority of "normal" video sites
  (this is also exactly why yt-dlp supports 1,800+ sites — that's the engine
  doing the heavy lifting here).
- **`blob:`/MSE players without DRM**: handled via the record button, but
  it's a real-time capture, not a download — it only captures from the
  moment you click forward, at the video's playback speed, and re-encodes
  to webm rather than grabbing the original file bit-for-bit.
- **Sites that scramble/obfuscate the URL in custom JS**: the generic
  sniffer won't catch these. You'd need a site-specific patch (yt-dlp
  itself has hundreds of these hand-written; this tool doesn't attempt to
  replicate that per-site work).
- **Only use this on content you actually have the right to download** —
  your own uploads, permissively-licensed material, or sites whose terms
  allow it. It doesn't check that for you.

## Sensible next steps if you keep building this

- Swap the Tkinter window for `pystray` if you want a tray-icon-only app.
- Add a download-history/queue view to the GUI (the `/jobs` endpoint already
  returns everything needed).
- Add an options page to the extension for changing the backend port/output
  folder without editing code.
