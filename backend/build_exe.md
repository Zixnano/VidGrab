# Building VideoGrabber.exe

1. On a Windows machine (PyInstaller builds are platform-specific — build the
   .exe on Windows, not on this Linux sandbox):

   ```
   cd backend
   python -m venv venv
   venv\Scripts\activate
   pip install -r requirements.txt
   ```

2. Freeze it:

   ```
   pyinstaller --onefile --noconsole --name VideoGrabber --collect-all yt_dlp server.py
   ```

   - `--onefile` gives you a single `VideoGrabber.exe`.
   - `--noconsole` hides the terminal window (the Tkinter window is the UI).
   - `--collect-all yt_dlp` is important — yt-dlp dynamically loads many
     per-site extractor modules that PyInstaller's static analysis can miss.

3. The exe lands in `dist/VideoGrabber.exe`. Run it once — Windows Defender
   / SmartScreen will likely flag an unsigned new binary the first time
   ("Windows protected your PC" → "More info" → "Run anyway"). Code-signing
   certificates remove that prompt but cost money and aren't required to
   use it yourself.

4. Optional polish:
   - Add `--icon=youricon.ico` for a real taskbar icon.
   - Add it to Windows startup (shell:startup folder) if you want it always
     running in the background instead of launching it manually.
   - If you want it to run invisibly with just a tray icon instead of a
     window, swap the Tkinter window for `pystray` — the Flask/yt-dlp core
     doesn't need to change.

## Common build issues

- **"ffmpeg not found" errors when merging separate video/audio streams**
  (common on HLS/DASH sites): yt-dlp needs `ffmpeg.exe` on PATH, or drop a
  copy of `ffmpeg.exe` next to `VideoGrabber.exe` and add
  `"ffmpeg_location": os.path.dirname(sys.executable)` to `ydl_opts` in
  `server.py`. Get a static build from https://www.gyan.dev/ffmpeg/builds/.
- **Antivirus deletes the exe on build**: PyInstaller onefile binaries
  self-extract at runtime, which some AV heuristics dislike. Add an
  exclusion for your build folder, or switch to `--onedir` (a folder
  instead of a single exe — less flagged, slightly less tidy).
