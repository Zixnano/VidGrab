# VideoGrabber.spec — thin launcher build (Session 9 restructure).
#
# Deliberately bundles NONE of the heavy deps: no PySide6, yt-dlp,
# streamlink, watchdog, ffmpeg, Deno, and no app code. Those ship in
# runtime\ beside the exe; app code lives in app\ so it can be hot-updated
# with the thin zip. Build: pyinstaller VideoGrabber.spec
import sys
from pathlib import Path

# python3.dll: PySide6's extension modules link the stable-ABI DLL. The
# launcher's own interpreter doesn't need it, but PySide6 loaded from
# runtime\ does — bundle it when the build python ships one (harmless
# when absent, e.g. inside a venv without it).
_py3_dll = Path(sys.base_prefix) / "python3.dll"
_binaries = [(str(_py3_dll), ".")] if _py3_dll.is_file() else []

a = Analysis(
    ['launcher.py'],
    pathex=[],
    binaries=_binaries,
    datas=[],
    hiddenimports=[
        # stdlib modules the app imports at runtime — frozen into the
        # launcher PYZ so they're guaranteed present when server.py runs
        'json', 'os', 're', 'sys', 'time', 'threading', 'queue', 'shutil',
        'subprocess', 'secrets', 'glob', 'socket', 'ssl', 'io', 'base64',
        'hashlib', 'uuid', 'urllib', 'urllib.parse', 'urllib.request',
        'urllib.error', 'http', 'http.server', 'http.client', 'zipfile',
        'tempfile', 'pathlib', 'datetime', 'ctypes', 'ctypes.wintypes',
        'contextlib', 'functools', 'itertools', 'collections', 'dataclasses',
        'enum', 'typing', 'traceback', 'warnings', 'logging',
        'logging.handlers', 'email', 'email.message', 'email.mime',
        'email.mime.text', 'argparse', 'csv', 'sqlite3', 'xml',
        'xml.etree', 'xml.etree.ElementTree',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'PySide6', 'shiboken6', 'PySide2', 'PyQt6', 'PyQt5',
        'yt_dlp', 'streamlink', 'watchdog', 'flask',
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

# Onedir (exclude_binaries + COLLECT): instant start — no onefile
# extraction to %TEMP% on every launch — and updatable, since a plain
# folder can be swapped with the same rename pattern as app\. Output:
# dist-launcher\VideoGrabber\VideoGrabber.exe + VideoGrabber_internal\
# (contents_directory renames PyInstaller 6's default _internal).
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='VideoGrabber',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # --noconsole: no stray CMD window behind the app
    icon='VideoGrabber.ico',
    contents_directory='VideoGrabber_internal',
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='VideoGrabber',
)
