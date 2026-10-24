# VideoGrabber.spec — the SUPERVISOR (launcher.py), frozen as
# VideoGrabber.exe. Onedir, windowed, stdlib only: it must stay tiny and
# must never bundle the app's heavy dependencies (they live in runtime\).
import os

# Icon for the exe (Explorer, taskbar, shortcuts). Put the .ico next to this
# spec as icon.ico, or point ICON_PATH at it. If none is found the build
# still succeeds with PyInstaller's default icon and prints a warning.
ICON_PATH = 'icon.ico'   # change to e.g. 'path/to/icon.ico'
_spec_dir = globals().get('SPECPATH', os.getcwd())
_icon_candidates = [ICON_PATH, os.path.join(_spec_dir, ICON_PATH),
                    os.path.join(_spec_dir, 'icons', 'icon.ico'),
                    os.path.join(_spec_dir, 'assets', 'icon.ico')]
ICON = next((os.path.abspath(p) for p in _icon_candidates if os.path.isfile(p)), None)
if ICON is None:
    print("WARNING: %s: no .ico found (tried %s); building with the default icon"
          % ('VideoGrabber.spec', _icon_candidates))


a = Analysis(
    ['launcher.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=['http.cookies'],
    hookspath=[],
    runtime_hooks=[],
    excludes=['numpy', 'PIL', 'PySide6', 'flask', 'yt_dlp', 'streamlink'],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name='VideoGrabber',
    icon=ICON,
    debug=False, bootloader_ignore_signals=False, strip=False,
    upx=False, console=False,
    contents_directory='VideoGrabber_internal',
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='VideoGrabber')
