# VideoGrabber.spec: thin launcher build.
#
# The exe bundles ONLY: the Python interpreter, the full stdlib, and a tiny
# set of pure-Python helpers. It does NOT bundle numpy, Pillow, PySide6,
# yt-dlp, streamlink, watchdog, flask, ffmpeg, Deno or app code. All of
# those live on disk in runtime\site-packages (and app\), put on sys.path by
# launcher.py at startup.
#
# Why numpy/Pillow are NOT collected here: they already ship, with their own
# DLL folders (numpy.libs etc.), in runtime\site-packages via
# `pip install --target`. Bundling a second copy into VideoGrabber_internal
# gives two copies of the same C extension and two sets of DLLs, which is
# the failure mode we are avoiding. PyInstaller only needs to know about
# modules the LAUNCHER imports; modules loaded later from disk import fine.
#
# Build: pyinstaller VideoGrabber.spec
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules

# python3.dll: PySide6's extension modules link the stable-ABI DLL. Bundle
# it when the build python ships one (harmless when absent).
_py3_dll = Path(sys.base_prefix) / "python3.dll"
_binaries = [(str(_py3_dll), ".")] if _py3_dll.is_file() else []

# Whole stdlib, so any package loaded from runtime\site-packages (numpy,
# PIL, PySide6, yt_dlp, flask...) finds every stdlib module it needs inside
# the frozen interpreter. collect_submodules walks packages so dotted
# submodules ("http.cookies") are included too.
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
        # Some stdlib entries aren't importable packages on every build
        # platform; skip silently rather than failing the build.
        pass

# Pure-Python helpers the launcher side may touch (unchanged from before).
_hidden_stdlib += [
    'requests', 'urllib3', 'idna', 'certifi', 'charset_normalizer',
]

# Everything that must come from runtime\site-packages, never from the exe.
_EXTERNAL = [
    'numpy', 'PIL', 'PySide6', 'shiboken6', 'PySide2', 'PyQt6', 'PyQt5',
    'yt_dlp', 'streamlink', 'watchdog', 'flask',
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
    excludes=_EXTERNAL,
    noarchive=False,
)

# Build-time tripwire: fail the build instead of shipping a duplicate copy
# of a package that is supposed to live in runtime\site-packages.
_ext_lower = {n.lower() for n in _EXTERNAL} | {'numpy.libs', 'pillow.libs'}
def _top(name):
    return str(name).replace('\\', '/').split('/')[0].split('.')[0].lower()
_leaks = set()
for _toc in (a.pure, a.binaries, a.datas):
    for _entry in _toc:
        if _top(_entry[0]) in _ext_lower:
            _leaks.add(str(_entry[0]))
if _leaks:
    raise SystemExit(
        "VideoGrabber.spec: these must NOT be bundled in the launcher "
        "(they belong in runtime\\site-packages): "
        + ", ".join(sorted(_leaks)[:20])
    )

pyz = PYZ(a.pure)

# Onedir: instant start (no onefile extraction) and updatable, since a plain
# folder can be swapped with the same rename pattern as app\.
# Output: dist-launcher\VideoGrabber\VideoGrabber.exe + VideoGrabber_internal\
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
    console=False,          # no stray CMD window behind the app
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
