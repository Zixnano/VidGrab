# launcher.py — thin entry point for VideoGrabber.exe
#
# Locates runtime/ and app/, exports VG_RUNTIME so engines.py can find
# ffmpeg/deno inside runtime\, sets up the DLL search path so PySide6
# can load Qt, then hands off to app/server.py.
#
# Works both frozen and from source (python launcher.py).
import os
import shutil
import sys
import threading
import time
from pathlib import Path


def _root() -> Path:
    """Where VideoGrabber.exe (or launcher.py from source) lives."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _log_error(msg: str) -> None:
    """Report a launcher error. Frozen --noconsole builds have
    sys.stderr=None, so a plain sys.stderr.write would raise and the
    user would see nothing — always persist to a log beside the exe,
    and mirror to stderr only when a console actually exists."""
    try:
        log_path = _root() / "launcher-error.log"
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(msg.rstrip() + "\n")
    except OSError:
        pass
    if sys.stderr is not None:
        try:
            sys.stderr.write(msg + "\n")
        except (OSError, ValueError):
            pass


def _setup_dll_search(runtime: Path) -> None:
    """Python 3.8+ requires explicit DLL directories for anything
    outside the default search path. PySide6 ships its Qt DLLs in a
    subfolder that isn't on PATH, so we add it."""
    if sys.platform != "win32":
        return
    candidates = [
        # pip --target layout: PySide6 stays inside site-packages
        runtime / "site-packages" / "PySide6",
        runtime / "site-packages" / "PySide6" / "Qt" / "bin",
        # moved layout, if someone insists on hoisting it to runtime\
        runtime / "PySide6",
        runtime / "PySide6" / "Qt" / "bin",
        runtime,
    ]
    for d in candidates:
        if d.is_dir():
            try:
                os.add_dll_directory(str(d))
            except OSError:
                pass


def _cleanup_update_leftovers(root: Path) -> None:
    """Delete the previous update's rollback leftovers (app.old\\, the
    renamed-out launcher exe, the renamed-out internal folder). Never
    during the swap — only after the new app has been up long enough to
    prove it starts, otherwise restore.bat / the rename pattern have
    nothing to fall back to."""
    leftovers = [
        root / "app.old",
        root / "VideoGrabber.exe.old",
        root / "VideoGrabber_internal.old",
    ]
    leftovers = [p for p in leftovers if p.exists()]
    if not leftovers:
        return

    def _later() -> None:
        time.sleep(60)  # survived this long => new app started fine
        for p in leftovers:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass

    threading.Thread(target=_later, daemon=True).start()


def main() -> int:
    root = _root()
    app_dir = root / "app"
    runtime_dir = root / "runtime"
    server_py = app_dir / "server.py"

    if not server_py.is_file():
        # Fall back to the flat layout (pre-restructure) so dev
        # runs don't explode.
        flat = root / "server.py"
        if flat.is_file():
            server_py = flat
            app_dir = root
        else:
            _log_error(
                "Video Grabber: can't find server.py.\n"
                f"  Looked in: {server_py}\n"
                f"  And:       {flat}\n"
                "Reinstall or extract the update zip again."
            )
            return 1

    if runtime_dir.is_dir():
        # VG_RUNTIME: engines.py lives in app\ and can't derive the runtime\
        # folder from __file__ — export it before any app code imports.
        os.environ["VG_RUNTIME"] = str(runtime_dir)
        _setup_dll_search(runtime_dir)
        sys.path.insert(0, str(runtime_dir))
        site = runtime_dir / "site-packages"
        if site.is_dir():
            sys.path.insert(0, str(site))

    sys.path.insert(0, str(app_dir))

    _cleanup_update_leftovers(root)

    # No os.chdir: sys.path is what makes app/*.py importable. Mutating the
    # process cwd would silently re-base every relative path in future code.
    sys.argv[0] = str(server_py)
    import runpy
    try:
        runpy.run_path(str(server_py), run_name="__main__")
    except Exception:
        import traceback
        _log_error(
            "launcher: unhandled exception while running server.py:\n"
            + traceback.format_exc()
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
