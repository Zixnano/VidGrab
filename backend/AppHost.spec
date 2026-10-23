# AppHost.spec — the app host (app_host.py, the former launcher),
# frozen as VideoGrabberApp.exe. Onedir, windowed, thin: PySide6 /
# numpy / PIL / yt-dlp are loaded at runtime from
# runtime\site-packages, never bundled here.
#
# hiddenimports is GENERATED, not curated. History: yt_dlp, requests
# and flask are excluded from the analysis and loaded at runtime, so
# PyInstaller never sees their import graphs, and the frozen host
# died one module at a time (http.cookies, logging.handlers,
# optparse, xml.etree, fileinput, ...) as each was hand-added to a
# list. A curated list can only ever contain the modules that have
# already crashed. So the spec now enumerates the ENTIRE standard
# library of the build Python from disk: every module that exists
# is named, and the whack-a-mole class of bug is gone by
# construction. Test suites and the Tk GUI toolkit are skipped (the
# app never uses them and they bloat or destabilise the build).
# Unresolvable names are warnings to PyInstaller, never errors, so
# an over-inclusive list is safe.
import os
import sys
import sysconfig


def _all_stdlib_hiddenimports():
    skip_dirs = {"__pycache__", "test", "tests", "idlelib",
                 "turtledemo", "tkinter", "turtle"}
    roots = set()
    for key in ("stdlib", "platstdlib"):
        try:
            p = sysconfig.get_path(key)
            if p:
                roots.add(p)
        except Exception:
            pass
    # Windows builds keep the C accelerator modules in DLLs\
    roots.add(os.path.join(sys.base_prefix, "DLLs"))
    names = set()
    for base in roots:
        if not base or not os.path.isdir(base):
            continue
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in skip_dirs]
            rel = os.path.relpath(root, base)
            pkg = "" if rel == "." else rel.replace(os.sep, ".")
            if pkg.split(".")[0] in skip_dirs:
                continue
            for f in files:
                if not f.endswith((".py", ".pyd", ".so")):
                    continue
                stem = f.split(".")[0]
                if not stem.isidentifier():
                    continue
                if stem == "__init__":
                    if pkg:
                        names.add(pkg)
                else:
                    names.add(pkg + "." + stem if pkg else stem)
    return sorted(names)


a = Analysis(
    ['app_host.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=_all_stdlib_hiddenimports() + [
        # Third-party modules we are now bundling
        'requests', 'flask',
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=['numpy', 'PIL', 'PySide6', 'yt_dlp', 'streamlink'],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name='VideoGrabberApp',
    debug=False, bootloader_ignore_signals=False, strip=False,
    upx=False, console=False,
    contents_directory='VideoGrabberApp_internal',
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='VideoGrabberApp')
