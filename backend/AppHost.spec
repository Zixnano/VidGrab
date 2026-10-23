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
# already crashed, so this spec enumerates the standard library of
# the build Python from disk instead.
#
# The enumeration is fenced to the TRUE standard library, three
# ways, because the first version of this generator also walked a
# root that contained the build machine's site-packages, named
# third-party submodules as hidden imports, dragged numpy into the
# host (its PyInstaller hook bundles numpy.libs), and tripped the
# pipeline's "host stays thin" check:
#   1. only the sysconfig "stdlib" root is walked (plus DLLs\ for
#      the C accelerators); "platstdlib" is NOT trusted, it can
#      point at the whole installation;
#   2. any "site-packages" directory met during the walk is pruned;
#   3. every surviving name must have its top-level component in
#      sys.stdlib_module_names, and the heavy excluded packages are
#      filtered out by name as a final belt-and-braces pass.
# Test suites and the Tk GUI toolkit are skipped (the app never
# uses them and they bloat or destabilise the build). Unresolvable
# names are warnings to PyInstaller, never errors.
import os
import sys
import sysconfig


def _all_stdlib_hiddenimports():
    skip_dirs = {"__pycache__", "test", "tests", "idlelib",
                 "turtledemo", "tkinter", "turtle", "site-packages"}
    roots = set()
    try:
        p = sysconfig.get_path("stdlib")
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
    # Fence 3a: nothing whose top level is not a genuine stdlib
    # module of THIS Python may survive, wherever the walk went.
    stdlib_tops = getattr(sys, "stdlib_module_names", None)
    if stdlib_tops is not None:
        names = {n for n in names if n.split(".")[0] in stdlib_tops}
    # Fence 3b: the excluded heavyweights, by name, unconditionally.
    heavy = ("numpy", "PIL", "PySide6", "yt_dlp", "streamlink")
    names = {n for n in names if n.split(".")[0] not in heavy}
    # Fence 3c: modules that only drag Tcl/Tk or demo code into the host.
    unwanted = {"turtle", "_tkinter", "tkinter", "idlelib", "turtledemo",
                "ensurepip", "this", "antigravity", "__phello__"}
    names = {n for n in names if n.split(".")[0] not in unwanted}
    return sorted(names)


def _runtime_dlls():
    """DLLs the thin host must carry so PySide6 / shiboken6 can load from
    runtime\\site-packages. PySide6 wheels are abi3 builds that link
    python3.dll (the stable-ABI forwarder), which PyInstaller does not
    bundle on its own (it only ships python3XX.dll), and Qt needs the
    MSVC C++ runtime. PyInstaller never saw these dependencies because
    PySide6 is excluded from the analysis. Existing files only."""
    found = []
    p = os.path.join(sys.base_prefix, "python3.dll")
    if os.path.isfile(p):
        found.append((p, "."))
    sysdir = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
    for n in ("msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll",
              "vcruntime140_1.dll", "concrt140.dll"):
        p = os.path.join(sysdir, n)
        if os.path.isfile(p):
            found.append((p, "."))
    print("AppHost.spec: extra runtime DLLs:", [os.path.basename(x[0]) for x in found])
    return found


a = Analysis(
    ['app_host.py'],
    pathex=[],
    binaries=_runtime_dlls(),
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
