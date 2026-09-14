# Building VideoGrabber.exe

1. On a Windows machine (PyInstaller builds are platform-specific — build the
   .exe on Windows, not on this Linux sandbox):

   ```
   cd backend
   python -m venv venv
   venv\Scripts\activate
   pip install -r requirements.txt
   ```

2. Freeze it (drop a static `ffmpeg.exe` next to `server.py` first — see
   "Common build issues" below for where to get one):

   ```
   pyinstaller --onefile --noconsole --name VideoGrabber --collect-all yt_dlp --collect-all tkinterdnd2 --collect-all PySide6 --add-binary "ffmpeg.exe;." server.py
   ```

   - `--collect-all tkinterdnd2` bundles the tkdnd Tcl libraries that the
     drag-and-drop features need — PyInstaller's static analysis misses them.
   - `--collect-all PySide6` bundles the Qt plugins/platform DLLs the new
     GUI needs — without it the frozen exe falls back to the Tkinter GUI
     with a "PySide6 GUI unavailable" log line.
   - `--onefile` gives you a single `VideoGrabber.exe`.
   - `--noconsole` hides the terminal window (the GUI is the only window).
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
  (common on HLS/DASH sites): the GitHub Actions workflow now downloads a
  static ffmpeg build and bundles it into the exe automatically
  (`--add-binary "ffmpeg.exe;."`), and `server.py` points yt-dlp at it via
  `sys._MEIPASS` when frozen — no manual step needed for CI builds. If
  you're building locally by hand instead of via Actions, drop a copy of
  `ffmpeg.exe` next to `server.py` before running PyInstaller, or put it on
  PATH. Get a static build from https://www.gyan.dev/ffmpeg/builds/.
- **Antivirus deletes the exe on build**: PyInstaller onefile binaries
  self-extract at runtime, which some AV heuristics dislike. Add an
  exclusion for your build folder, or switch to `--onedir` (a folder
  instead of a single exe — less flagged, slightly less tidy).
