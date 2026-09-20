# VideoGrabber.spec — thin launcher build (Session 9 restructure).
#
# Deliberately bundles NONE of the heavy deps: no PySide6, yt-dlp,
# streamlink, watchdog, ffmpeg, Deno, and no app code. Those ship in
# runtime\ beside the exe; app code lives in app\ so it can be hot-updated
# with the thin zip. Build: pyinstaller VideoGrabber.spec
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules

# python3.dll: PySide6's extension modules link the stable-ABI DLL. The
# launcher's own interpreter doesn't need it, but PySide6 loaded from
# runtime\ does — bundle it when the build python ships one (harmless
# when absent, e.g. inside a venv without it).
_py3_dll = Path(sys.base_prefix) / "python3.dll"
_binaries = [(str(_py3_dll), ".")] if _py3_dll.is_file() else []

# Bundle the entire stdlib via collect_submodules. sys.stdlib_module_names
# only lists top-level names ("http") and misses submodules
# ("http.cookies"); collect_submodules walks each package and returns the
# full dotted paths, which is what PyInstaller needs to actually include
# them. This is the "bundle it all once, never chase a missing module
# again" approach — costs ~15 MB on the exe, saves a lot of future pain.
_SKIP_STDLIB = {
    'antigravity', 'this', '__main__', '__hello__', '__phello__',
    'idlelib', 'turtledemo', 'turtle', 'tkinter', '_tkinter',
    'test', 'pydoc_data', 'ensurepip', 'venv', 'site', 'sitecustomize',
    'usercustomize', 'distutils', 'lib2to3',
}
_hidden_stdlib = []
for _name in sorted(sys.stdlib_module_names):
    if _name in _SKIP_STDLIB or _name.startswith(('_test', 'xx', '_xx')):
        continue
    _hidden_stdlib.append(_name)
    try:
        _hidden_stdlib += collect_submodules(_name)
    except Exception:
        # Some stdlib entries aren't real importable packages on every
        # build platform; skip silently rather than failing the build.
        pass

_hidden_stdlib += [
    'requests', 'urllib3', 'idna', 'certifi', 'charset_normalizer',
]

a = Analysis(
    ['launcher.py'],
    pathex=[],
    binaries=_binaries,
    datas=[],
    hiddenimports=_hidden_stdlib,
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