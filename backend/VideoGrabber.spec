import sys
import re
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules, collect_all

# --- 1. Read requirements.txt ---
# This grabs every package name from requirements.txt and tells PyInstaller
# to collect the binaries, data, and hidden imports for ALL of them.
req_file = Path("requirements.txt")
_collected_binaries = []
_collected_datas = []
_collected_hidden = []

if req_file.is_file():
    for line in req_file.read_text().splitlines():
        line = line.split("#")[0].strip()
        if not line or line.startswith(("-", "git+", "http")):
            continue
        
        # Extract just the package name (e.g., "numpy>=1.26" -> "numpy")
        match = re.match(r"^([A-Za-z0-9_.\-]+)", line)
        if match:
            pkg_name = match.group(1)
            # PyInstaller uses import names, not always pip names
            if pkg_name.lower() == "pillow":
                pkg_name = "PIL"
            elif pkg_name.lower() == "yt-dlp":
                pkg_name = "yt_dlp"
            elif pkg_name.lower() == "pyside6":
                continue  # We handle PySide6 separately in the launcher
                
            try:
                print(f"Bundling dependencies for: {pkg_name}")
                datas, binaries, hidden = collect_all(pkg_name)
                _collected_datas.extend(datas)
                _collected_binaries.extend(binaries)
                _collected_hidden.extend(hidden)
            except Exception as e:
                print(f"Skipping {pkg_name} collection: {e}")

# --- 2. Standard Launcher Setup ---
_py3_dll = Path(sys.base_prefix) / "python3.dll"
_binaries = [(str(_py3_dll), ".")] if _py3_dll.is_file() else []
_binaries.extend(_collected_binaries)

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
        pass

_hidden_stdlib += [
    'requests', 'urllib3', 'idna', 'certifi', 'charset_normalizer',
]
_hidden_stdlib.extend(_collected_hidden)

a = Analysis(
    ['launcher.py'],
    pathex=[],
    binaries=_binaries,
    datas=_collected_datas,
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
    console=False,
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