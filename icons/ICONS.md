ICON SET — Video Grabber
========================
Concept: a green arrow dropping into a light-grey tray, on a near-black
(#0b1117) rounded square. Accent #4caf50, details #e6edf3. Corner radius
and stroke weights are consistent across sizes; 16/32/48 are hand-tuned
with bolder strokes so the concept survives scaling.

Files:
  VideoGrabber-1024.svg  app icon master (1024x1024)
  VideoGrabber-128.svg   app icon, 128px
  VideoGrabber-32.svg    app icon, 32px (taskbar)
  icon16.svg             extension icon 16x16
  icon32.svg             extension icon 32x32 (same as app 32)
  icon48.svg             extension icon 48x48
  icon128.svg            extension icon 128x128 (same as app 128)
  tray-32.svg            tray icon: same shape, single #e6edf3 color,
                         transparent background, reads on light/dark bars

Convert to PNG/ICO (already done — PNG exports live in extension/icons/ and icons/, and backend/VideoGrabber.ico is committed; only redo this if you change the artwork):
  1. SVG -> PNG: any free converter works (e.g. https://cloudconvert.com
     or Inkscape: File > Export PNG, one export per size).
  2. PNG -> ICO: https://icoconvert.com (free one-page tool — upload the
     1024 PNG, it outputs a multi-size .ico), or IcoFX on Windows.
  3. Drop VideoGrabber.ico into backend/ and build with:

     pyinstaller --onedir --noconsole --name VideoGrabber ^
         --icon=VideoGrabber.ico ^
         --collect-all yt_dlp --collect-all tkinterdnd2 --collect-all PySide6 ^
         --add-binary "ffmpeg.exe;." server.py

  4. Manifest wiring (extension): point manifest.json "icons" and
     "action.default_icon" at icon16/32/48/128.png.
