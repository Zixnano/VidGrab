"""Video Grabber — bootstrap entry point.

All logic lives in the sibling modules. This file's only job is to:
1. Resolve the portable home directory
2. Load settings and job history
3. Start the Flask server thread
4. Start the dispatcher thread
5. Hand off to the Qt GUI
"""

from pathlib import Path
import sys
import threading
import time

from settings import STATE, HOME, load_settings, _dest_for, start_bandwidth_profile_watcher, set_paths, portable_home
from jobs import JOBS, load_jobs_snapshot, save_jobs_snapshot, register_new_job_hook, register_show_dialog_hook
from downloader import _ytdlp_version_check, start_recording_watcher, dispatcher_loop
from logging_setup import log
import settings
from settings import APP_PORT
from api import app, run_server


def _cleanup_stale_parts(max_age_hours=24):
    """Delete .part files older than max_age_hours with no active job."""
    cutoff = time.time() - max_age_hours * 3600
    active = set()
    for j in JOBS.values():
        try:
            p = _dest_for(j)
            active.add(str(p.with_suffix(p.suffix + ".part")))
        except Exception:
            pass
    count = 0
    roots = {Path(STATE["output_dir"])}
    try:
        for d in load_settings().get("per_category_dirs", {}).values():
            if d:
                roots.add(Path(d))
    except Exception:
        pass
    try:
        for root in roots:
          for part in root.rglob("*.part*"):
            if str(part) in active:
                continue
            try:
                if part.stat().st_mtime < cutoff:
                    part.unlink()
                    count += 1
            except OSError:
                pass
    except Exception as e:
        log(f"stale .part cleanup failed: {e}")
    if count:
        log(f"cleaned up {count} stale partial file(s)")


def _port_in_use(port):
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return False
    except OSError:
        return True
    finally:
        s.close()


def main():
    set_paths(portable_home())
    settings.HOME.mkdir(parents=True, exist_ok=True)
    load_settings()
    _ytdlp_version_check()
    if _port_in_use(APP_PORT):
        log(f"Port {APP_PORT} is already in use — another Video Grabber "
            f"instance may be running. Exiting.")
        sys.exit(1)
    load_jobs_snapshot()
    _cleanup_stale_parts()

    threading.Thread(target=run_server, daemon=True).start()
    threading.Thread(target=dispatcher_loop, daemon=True).start()
    start_recording_watcher()
    start_bandwidth_profile_watcher()
    log(f"Server started on port {APP_PORT}. Saving to {STATE['output_dir']}")

    try:
        from gui_qt import launch_gui
        launch_gui(new_job_hook=register_new_job_hook, home_dir=settings.HOME,
                   show_dialog_hook=register_show_dialog_hook)
    except ImportError as e:
        log(f"PySide6 failed to import ({e}). This build is broken.")
        sys.exit(1)
    save_jobs_snapshot()


if __name__ == "__main__":
    main()
