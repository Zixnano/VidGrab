# Building VideoGrabber.exe (Windows)

PyInstaller builds are platform-specific, so the exe must be built on
Windows — either on your own machine or via the GitHub Actions workflow in
`.github/workflows/build-exe.yml` (push to GitHub and use "Run workflow",
or just push; it uploads the built app as an artifact).

## Manual build

`VideoGrabber.ico` is committed in this folder — the build below uses it
for the exe icon automatically. (The SVG masters live in ../icons/; PNG
exports and the .ico are pre-generated, so no conversion step is needed
unless you want to change the artwork.)

```bat
cd backend
pip install -r requirements.txt

REM grab a static ffmpeg build (needed for merging/converting)
curl -L -o ffmpeg.zip https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip
tar -xf ffmpeg.zip
REM copy the extracted ffmpeg.exe AND ffprobe.exe into backend\

REM grab Deno (needed for yt-dlp to solve YouTube's signature challenges)
curl -L -o deno.zip https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip
tar -xf deno.zip
REM copy the extracted deno.exe into backend\

pyinstaller --onedir --noconsole --name VideoGrabber --icon=VideoGrabber.ico ^
    --collect-all yt_dlp --collect-all tkinterdnd2 --collect-all PySide6 ^
    --add-binary "ffmpeg.exe;." --add-binary "ffprobe.exe;." --add-binary "deno.exe;." server.py
```

## Important: the output is a FOLDER, not a single file

`--onedir` produces `backend\dist\VideoGrabber\` containing
`VideoGrabber.exe` **plus** all of its DLLs, yt-dlp data, PySide6 plugins
and `ffmpeg.exe` in the same folder. This is deliberate: the old
`--onefile` mode re-extracted that entire bundle to a temp folder on every
launch, which cost 30 seconds to a few minutes before the window appeared.

**The whole `VideoGrabber` folder must be kept together.** Zip it, ship it,
and unzip it as one unit — moving `VideoGrabber.exe` out of the folder on
its own will not start. To "install", copy the folder anywhere (e.g.
`C:\Program Files\VideoGrabber\`) and make a shortcut to the exe. To get a
truly single-file exe back, you'd re-add `--onefile` and accept the slow
launch.

## Optional: portable mode

Drop a `portable.txt` file next to `VideoGrabber.exe` and settings/jobs
will be stored in a `VideoGrabberData` folder alongside the exe instead of
in `~/Downloads/VideoGrabber`.
