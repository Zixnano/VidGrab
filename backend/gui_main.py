"""
gui_qt.py — modern PySide6 frontend for Video Grabber.

Talks to the local Flask backend on 127.0.0.1:5757. Reads the pairing token
from settings.json. Imported and launched by server.py's main().

v11: the module was split into gui_*.py; gui_qt.py is now a thin compatibility shim.
"""

import base64
import ctypes
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

from PySide6.QtCore import QByteArray, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QFont, QIcon
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QDialog, QFrame, QGraphicsDropShadowEffect, QHBoxLayout,
    QHeaderView, QInputDialog, QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox, QPushButton,
    QSizePolicy, QSplitter, QStatusBar, QSystemTrayIcon, QTableWidget, QVBoxLayout, QWidget,
)

from gui_detail import DetailPane, ThumbLoader
# Palette-derived (single source of truth: palette.py).
from palette import resolve_palette
from settings import CATEGORIES, DISPLAY_VERSION
import settings as _settings_mod
from logging_setup import log

import gui_palette
import gui_common
from gui_palette import _sync_palette
from gui_common import (
    _eta_seconds, _path_for, _retry_text, _settings_cache, BACKEND_BASE, fmt_bytes, fmt_ts,
    load_settings,
)
from gui_api import _ApiFetchWorker, ApiClient
from gui_sidebar import _make_tray_icon, _qicon, Sidebar
from gui_table import (
    _BottomPane, _copy_files_to_clipboard, _detail_info, _ffmpeg_dir, _playlist_dir_for,
    DownloadTable, ProgressDelegate,
)
from gui_models import DownloadModel
from gui_add_dialog import AddDownloadDialog
from gui_playlist_picker import PlaylistPickerDialog
from gui_settings_dialog import _COMPONENT_WORKERS, BandwidthProfilesDialog, SettingsDialog
from gui_dialogs import _fade_dialog, _fade_widget, HelpDialog, PairDialog, UpdateDialog, WhatsNewDialog


# --- Supervisor Architecture (contract v3): sentinel markers + state ----
# The supervisor (launcher.py) spawns this app with VG_LAUNCH_ID set.
# On first show we write app_alive.tmp; once our own /health answers
# with the app identity we write startup_ok.tmp. Without VG_LAUNCH_ID
# (dev runs, old launcher) all of this is a no-op.
_VG_LAUNCH_ID = os.environ.get("VG_LAUNCH_ID", "")


try:
    import vg_state as _vg_state_mod
except Exception:
    # An install caught mid-transition (app\ swapped before
    # vg_state.py landed): the hooks degrade to no-ops rather than
    # taking the window down with an ImportError.
    _vg_state_mod = None


def _vg_state_dir():
    if _vg_state_mod is not None:
        return _vg_state_mod.state_dir()
    base = os.environ.get("VG_STATE_DIR") or os.environ.get("LOCALAPPDATA")
    if base and os.environ.get("VG_STATE_DIR"):
        return Path(base)
    if base:
        return Path(base) / "VideoGrabber"
    return Path.home() / "AppData" / "Local" / "VideoGrabber"


def _vg_boot_flag():
    if _vg_state_mod is not None:
        return _vg_state_mod.boot_flag()
    return _vg_state_dir() / "boot_recovery.flag"


def _vg_write_marker(name):
    if not _VG_LAUNCH_ID:
        return
    try:
        d = _vg_state_dir()
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(json.dumps(
            {"launch_id": _VG_LAUNCH_ID, "timestamp": int(time.time())}),
            encoding="utf-8")
    except Exception:
        pass


def _vg_update_state(fn):
    """Read-modify-write of recovery_state.json via the ONE shared
    implementation (vg_state.py, fix 10)."""
    if _vg_state_mod is None:
        return
    try:
        _vg_state_mod.mutate_state(fn)
    except Exception:
        pass


def _title_bar_colors():
    """(caption, text) colors for native title bars, from the live theme, so
    every window matches the app instead of only being light or dark."""
    try:
        return gui_palette._PAL["bg_base"], gui_palette._PAL["text"]
    except Exception:
        return None, None


def _title_bar_dark():
    """True when the current theme is dark, so the native title bar should be."""
    try:
        from palette import luminance
        return luminance(gui_palette._PAL["bg_base"]) < 0.4
    except Exception:
        return True


# Toolbar keys are the toolbar labels (see MainWindow._build_ui); sidebar keys
# are nav names. Values are keys of _SVG_PATHS.
_TB_ICONS = {
    "Add URL": "add", "Batch": "batch", "Playlist": "playlist",
    "Resume": "play", "Pause": "pause", "Stop": "stop",
    "Delete": "delete", "Queue": "stream",
    "Pause Queue": "queue_pause", "Start Queue": "queue_play",
    "Settings": "settings",
}


def _toolbar_icon_color():
    """Icon tint for the toolbar: the secondary text color, or the frame's
    text color when the colored window frame is on."""
    try:
        if load_settings().get("frame_cream"):
            from palette import contrast, luminance
            fb = gui_palette._PAL["frame"]
            return gui_palette.BG if contrast(fb, gui_palette.BG) >= 4.5 else ("#000000" if luminance(fb) > 0.4 else "#ffffff")
    except Exception:
        pass
    return gui_palette.SOFT


def reapply_theme():
    """Rebuild the app stylesheet from saved settings and repaint the main
    window. Called after Settings saves."""
    from gui_style import build_qss
    st = load_settings()
    _sync_palette(st)
    app = QApplication.instance()
    if app is not None:
        app.setStyleSheet(build_qss(st))
        f = getattr(app, "_vg_dark_filter", None)
        if f is not None:
            f.refresh()              # theme may have flipped light <-> dark
    w = globals().get("_window")
    if w is not None:
        w.table.viewport().update()
        w.table.horizontalHeader().viewport().update()
        w.select_category(w.current_filter)
        w.apply_shell()
        w._apply_blanc()


def _app_icon_path():
    """icon.ico next to this module (CI copies it into app\\), else the
    usual places next to the source. None when no icon is shipped."""
    for base in (Path(__file__).resolve().parent, gui_common.HOME):
        for rel in ("icon.ico", "icons/icon.ico", "assets/icon.ico"):
            cand = base / rel
            try:
                if cand.is_file():
                    return cand
            except OSError:
                continue
    return None


def _apply_app_icon(app, window=None):
    """Give the Qt app and window the Video Grabber icon (title bar, taskbar,
    Alt+Tab). Also sets an explicit Windows AppUserModelID so the taskbar
    groups under this icon instead of python's. Returns the QIcon or None."""
    if sys.platform == "win32":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("VideoGrabber.App")
        except Exception:
            pass
    path = _app_icon_path()
    if path is None:
        return None
    ic = QIcon(str(path))
    if ic.isNull():
        return None
    app.setWindowIcon(ic)
    if window is not None:
        window.setWindowIcon(ic)
    return ic


def _is_active(j):
    return j.get("status") == "downloading" or j.get("phase") == "converting"


def _in_queue_view(j):
    """v5.5 Queue view: what is running plus what is waiting to run."""
    return _is_active(j) or j.get("status") in ("queued", "held")


def _sidebar_counts(items, hide_hours=0):
    """Counts for the sidebar. All and Unfinished leave out failed downloads
    older than `hide_hours` (the hide_old_errors_hours setting), exactly like
    the table does in those two views (see MainWindow.apply_filter). The
    Failed view and the category views still show every failure, so their
    counts stay complete."""
    try:
        hrs = float(hide_hours or 0)
    except (TypeError, ValueError):
        hrs = 0
    shown = items
    if hrs > 0:
        cutoff = time.time() - hrs * 3600
        shown = [j for j in items
                 if not (j.get("status") == "error"
                         and (j.get("failed_ts") or j.get("created_ts") or 0) < cutoff)]
    c = {
        "All": len(shown),
        "Active": sum(1 for j in items if _is_active(j)),
        "Queue": sum(1 for j in items if _in_queue_view(j)),
        "Finished": sum(1 for j in items if j.get("status") == "done"),
        "Unfinished": sum(1 for j in shown if j.get("status") != "done"),
        "Failed": sum(1 for j in items if j.get("status") == "error"),
    }
    for cat in CATEGORIES:
        c[cat] = sum(1 for j in items if j.get("category") == cat)
    return c


def _gui_state_path():
    return gui_common.HOME / "gui_state.json"


def _read_gui_state():
    try:
        return json.loads(_gui_state_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_gui_state(patch):
    try:
        st = _read_gui_state()
        st.update(patch)
        _gui_state_path().parent.mkdir(parents=True, exist_ok=True)
        tmp = _gui_state_path().with_suffix(".json.tmp")
        tmp.write_text(json.dumps(st), encoding="utf-8")
        os.replace(tmp, _gui_state_path())
    except Exception:
        pass


def _win_select_in_explorer(path):
    """Highlight a file in Windows Explorer via the shell API — more
    reliable than `explorer /select,`. Falls back to it on any error."""
    shell32 = ctypes.windll.shell32
    ole32 = ctypes.windll.ole32
    ole32.CoInitialize(None)
    try:
        pidl = ctypes.c_void_p()
        shell32.SHParseDisplayName.restype = ctypes.c_long
        shell32.SHParseDisplayName.argtypes = [
            ctypes.c_wchar_p, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p), ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong),
        ]
        hr = shell32.SHParseDisplayName(str(path), None,
                                        ctypes.byref(pidl), 0, None)
        if hr == 0 and pidl:
            shell32.SHOpenFolderAndSelectItems.argtypes = [
                ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p,
                ctypes.c_ulong,
            ]
            shell32.SHOpenFolderAndSelectItems(pidl, 0, None, 0)
            ole32.CoTaskMemFree(pidl)
    except Exception:
        subprocess.Popen(f'explorer /select,"{path}"')
    finally:
        ole32.CoUninitialize()


class MainWindow(QMainWindow):
    # Carries the pill payload from /show-add-dialog to the GUI thread.
    _show_dialog_signal = Signal(object)
    _new_job_signal = Signal(object)
    # Update-check result back from the worker thread (updater runs off
    # the UI thread so the app doesn't freeze while it hits GitHub).
    # Background scheduler found a newer app release (tag string).
    _update_available_signal = Signal(str)

    def __init__(self, api=None):
        super().__init__()
        self.setAcceptDrops(True)     # drop a local video to open the Media Toolkit
        self.api = api or ApiClient()
        self.model = DownloadModel(self)
        self.current_filter = "All"
        self.all_items = []
        self._quitting = False
        self._tray = None
        self._tray_hint_shown = False
        self._done_seen = set()
        self._error_seen = set()
        self._recently_done = {}
        self._sort_column = None
        self._refresh_worker = None
        self._logs_worker = None
        self._diskspace_worker = None
        self._queue_worker = None
        self._pref_workers = set()   # v5.5: short save-setting calls (sidebar collapse)
        self._update_tag = ""
        self._batch = {"seeded": False, "busy": False, "done": 0, "failed": 0,
                       "known_done": set(), "known_err": set()}
        self._update_notified = ""
        # Update prompt + changelog state (see _maybe_show_update_dialog).
        self._update_dialog_open = False
        self._snooze_gen = 0
        self._changelog_worker = None
        # Activity Log feed: changelog lines drain here, one per tick.
        self._activity_feed = deque()
        self._activity_timer = QTimer(self)
        self._activity_timer.setInterval(2200)
        self._activity_timer.timeout.connect(self._activity_tick)
        # Settings snapshot fetched on the worker thread each refresh —
        # the GUI thread never reads settings.json on the hot path (11.2).
        self._gui_settings = {}
        self.animations_enabled = True
        # 12.2: ~30 fps viewport repaint while a download is active so
        # progress interpolation actually renders. Self-guarding no-op.
        self._anim_timer = QTimer(self)
        # 8: ~15 fps is plenty for the progress interpolation; 30 fps
        # repainting the whole table viewport was a source of UI stutter.
        self._anim_timer.setInterval(66)
        self._anim_timer.timeout.connect(self._tick_animations)
        self._anim_timer.start()
        self._sort_ascending = True
        self.setWindowTitle(f"Video Grabber v{DISPLAY_VERSION}")
        self.resize(1200, 760)
        self.setMinimumSize(720, 480)
        self._build_ui()
        self.table.horizontalHeader().sectionClicked.connect(self._on_header_clicked)
        self._show_dialog_signal.connect(self._on_show_dialog, Qt.QueuedConnection)
        self._apply_styles()
        self._build_tray()
        self._new_job_signal.connect(self._on_new_job, Qt.QueuedConnection)
        self._update_available_signal.connect(self._on_update_available, Qt.QueuedConnection)
        try:
            import updater
            updater.register_update_hook(self._update_available_signal.emit)
            _st = updater.get_update_state()
            if _st.get("available") and _st["available"] != _st.get("skipped"):
                self._update_available_signal.emit(_st["available"])
        except Exception:
            pass
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(500)
        self.log_timer = QTimer(self)
        self.log_timer.timeout.connect(self.refresh_logs)
        self.log_timer.start(1500)
        QTimer.singleShot(2000, self._check_version_changelog)
        self.diskspace_timer = QTimer(self)
        self.diskspace_timer.timeout.connect(self.refresh_diskspace)
        self.queue_timer = QTimer(self)
        self.queue_timer.timeout.connect(self.refresh_queue_status)
        self.queue_timer.start(3000)
        self.diskspace_timer.start(15000)  # disk space changes slowly — no need to poll every second
        self.fade_timer = QTimer(self)
        self.fade_timer.timeout.connect(self._tick_fade)
        self.fade_timer.start(50)
        # 6: periodic silent memory trim (Tools > Trim UI memory does it on demand).
        self._trim_timer = QTimer(self)
        self._trim_timer.timeout.connect(lambda: self.trim_ui_memory(silent=True))
        self._trim_timer.start(10 * 60 * 1000)
        # Generate Sounds moved to Settings > Sounds (see the import at the top).
        self.select_category("All")
        self.refresh()
        self.refresh_diskspace()
        self.refresh_queue_status()

    def new_job_hook(self):
        return self._new_job_signal.emit

    def show_dialog_hook(self):
        return self._show_dialog_signal.emit

    def _on_show_dialog(self, payload):
        """Open AddDownloadDialog pre-filled with the pill's payload. Runs
        on the GUI thread via the queued signal connection."""
        try:
            # Task 3: raise the main window first — an AddDownloadDialog
            # parented to a hidden/backgrounded window can still end up
            # behind other apps even with its own StaysOnTop flag. This is
            # unconditional (unlike _on_new_job's force_on_top check) since
            # the user explicitly needs to interact with this dialog.
            if self.isMinimized():
                self.showNormal()
            self.raise_()
            self.activateWindow()
            if payload.get("pick_playlist"):
                # Pill "Pick playlist items..." (or an older extension's
                # "Download entire playlist"): straight to the picker, with
                # the page's login so the probe sees what the browser sees.
                self._open_playlist_picker(
                    prefill_url=payload.get("url", ""),
                    referer=payload.get("referer"),
                    cookie=payload.get("cookie"),
                    user_agent=payload.get("user_agent"))
                return
            _rt = payload.get("_req_ts")
            if _rt:
                try:
                    from settings import log as _log
                    _log(f"add dialog: opening {int((time.time() - _rt) * 1000)} ms after "
                         f"the extension's request reached the app")
                except Exception:
                    pass
            d = AddDownloadDialog(self, api=self.api,
                                  prefill_url=payload.get("url", ""),
                                  prefill_format_id=payload.get("format_id"),
                                  prefill_target_format=payload.get("target_format"),
                                  prefill_download_playlist=payload.get("download_playlist", False))
            _fade_dialog(d, self.animations_enabled)
            result = d.exec()
            if result not in (QDialog.Accepted, 2):
                return
            v = d.values()
            if not v or not v[0]["url"]:
                return
            common = v[0]
            if common.get("remember") and common.get("save_path"):
                try:
                    self.api.save_setting("per_category_dirs",
                                          {common["category"]: common["save_path"]})
                except Exception:
                    pass
            src_url = (payload.get("url") or "").strip()
            if common.get("download_playlist"):
                # "Download entire playlist (choose items)": hand the URL to
                # the picker instead of queueing one job that walks it all.
                if common.get("skip_dialog"):
                    try:
                        self.api.save_setting("skip_add_dialog", True)
                    except Exception:
                        pass
                same = common["url"] == src_url
                self._open_playlist_picker(
                    prefill_url=common["url"],
                    referer=payload.get("referer") if same else None,
                    cookie=payload.get("cookie") if same else None,
                    user_agent=payload.get("user_agent") if same else None)
                return
            total = len(v)
            for item in v:
                # The payload's filename/referer/cookie/UA describe the URL
                # the browser handed over. Forward them whenever the URL is
                # unchanged (so a cookie is never sent to a different site
                # than it came from). This used to be gated on
                # detect_type()=="generic" — which dropped cookies for every
                # yt-dlp job and produced "Sign in to confirm you're not a
                # bot" on YouTube once the user's IP hit the rate limit.
                extra = {}
                if item["url"] == src_url:
                    extra = {"filename": payload.get("filename"),
                             "referer": payload.get("referer"),
                             "cookie": payload.get("cookie"),
                             "user_agent": payload.get("user_agent"),
                             "page_url": payload.get("page_url")}
                self.api.add(item["url"], category=item["category"],
                             description=item["description"],
                             format_id=item.get("format_id"),
                             target_format=item.get("target_format"),
                             resolution=item.get("resolution"),
                             download_playlist=item.get("download_playlist", False),
                             multi=total > 1, **extra)
            if common.get("skip_dialog"):
                try:
                    self.api.save_setting("skip_add_dialog", True)
                except Exception:
                    pass
        except Exception as e:
            QMessageBox.critical(self, "Add failed", str(e))
        self.refresh()

    def _build_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self._tray = QSystemTrayIcon(self)
        self._tray.setIcon(_make_tray_icon(False))
        self._tray.setToolTip("Video Grabber")
        menu = QMenu()
        a_restore = menu.addAction("Restore")
        a_restore.triggered.connect(self._restore_from_tray)
        a_folder = menu.addAction("Open Downloads folder")
        a_folder.triggered.connect(self._open_downloads_folder)
        menu.addSeparator()
        a_pause = menu.addAction("Pause all")
        a_pause.triggered.connect(self._pause_all)
        a_resume = menu.addAction("Resume all")
        a_resume.triggered.connect(self._resume_all)
        menu.addSeparator()
        a_exit = menu.addAction("Exit")
        a_exit.triggered.connect(self._real_quit)
        self._tray.setContextMenu(menu)
        self._tray.activated.connect(self._on_tray_activated)
        self._tray.show()

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.DoubleClick:
            self._restore_from_tray()

    def _restore_from_tray(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _open_downloads_folder(self):
        path = Path(load_settings().get("output_dir", str(gui_common.HOME)))
        path.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(str(path))

    def _open_logs_folder(self):
        path = gui_common.HOME / "logs"
        path.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(str(path))

    def _pause_all(self):
        try:
            for j in self.api.jobs():
                if j.get("status") in ("downloading", "queued"):
                    try:
                        self.api.pause(j["id"])
                    except Exception:
                        pass
        except Exception as e:
            QMessageBox.warning(self, "Pause all", str(e))
        self.refresh()

    def _resume_all(self):
        try:
            for j in self.api.jobs():
                if j.get("status") in ("paused", "stopped"):
                    try:
                        self.api.resume(j["id"])
                    except Exception:
                        pass
        except Exception as e:
            QMessageBox.warning(self, "Resume all", str(e))
        self.refresh()

    # --- Supervisor Architecture hooks --------------------------------
    def showEvent(self, event):
        super().showEvent(event)
        if not getattr(self, "_vg_started", False):
            self._vg_started = True
            _vg_write_marker("app_alive.tmp")          # window painted
            _vg_update_state(lambda st: st.setdefault("last_paths", {}).update(
                {"data_home": str(gui_common.HOME)}))
            threading.Thread(target=self._vg_health_watch, daemon=True).start()
            QTimer.singleShot(1500, self._vg_show_update_result)

    def _vg_show_update_result(self):
        """Fix 13: an update that ran while the app was closed (or
        failed inside the updater window nobody saw) is reported
        here, exactly once: vg_state.pop_update_result clears it in
        the same locked pass that reads it."""
        if _vg_state_mod is None:
            return
        try:
            result = _vg_state_mod.pop_update_result()
        except Exception:
            return
        if not result:
            return
        tag = result.get("tag") or "the update"
        if result.get("ok"):
            QMessageBox.information(
                self, "Update finished",
                f"{result.get('message') or f'Updated to {tag}.'}")
        else:
            QMessageBox.warning(
                self, "Update did not finish",
                f"{result.get('message') or 'The update failed.'}\n\n"
                "The previous version is still installed. You can try "
                "again from Tools > Updates, or open Recovery "
                "(Tools > App) if the app misbehaves.")

    def _vg_health_watch(self):
        """Write startup_ok.tmp once our own backend answers /health
        with the Video Grabber identity (contract §3)."""
        if not _VG_LAUNCH_ID:
            return
        try:
            from settings import APP_PORT
        except Exception:
            APP_PORT = 5757
        import urllib.request
        deadline = time.time() + 120
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{APP_PORT}/health", timeout=2) as r:
                    data = json.loads(r.read().decode("utf-8", "replace"))
                if isinstance(data, dict) and data.get("app") == "videograbber":
                    _vg_write_marker("startup_ok.tmp")
                    return
            except Exception:
                pass
            time.sleep(0.5)

    def _restart_in_recovery(self):
        """Tools > App: set the boot flag and quit; the supervisor sees
        the flag when we exit and boots recovery.exe instead."""
        if QMessageBox.question(
                self, "Restart in Recovery Mode",
                "Video Grabber will close and the Recovery tool will open. "
                "Continue?") != QMessageBox.Yes:
            return
        try:
            flag = _vg_boot_flag()
            flag.parent.mkdir(parents=True, exist_ok=True)
            flag.write_text("gui", encoding="utf-8")
        except Exception:
            pass
        self._real_quit()

    def _request_supervised_update(self):
        """Tools > Updates: hand the install to the supervisor (§5).
        We only set update_pending and exit; the supervisor runs the
        animated updater, with updater.bat as its fallback."""
        if QMessageBox.question(
                self, "Install update (supervised)",
                "Video Grabber will close. The supervisor will back up the "
                "current version, download and install the newest allowed "
                "release, and relaunch. Continue?") != QMessageBox.Yes:
            return
        _vg_update_state(lambda st: st.update({"update_pending": True}))
        self._real_quit()

    def _real_quit(self):
        self._quitting = True
        if self._tray:
            self._tray.hide()
        QApplication.instance().quit()

    def closeEvent(self, event):
        if self._quitting:
            event.accept()
            return
        if (self._tray and self._tray.isVisible()
                and load_settings().get("close_to_tray", True)):
            event.ignore()
            self.hide()
            if not self._tray_hint_shown:
                self._tray_hint_shown = True
                self._tray.showMessage(
                    "Video Grabber",
                    "Still running in the system tray. Right-click the icon to Exit.",
                    QSystemTrayIcon.Information,
                    3000,
                )
            return
        self._quitting = True
        # 11.1: stop in-flight fetch workers — a running QThread at exit
        # hangs the app.
        self._save_header_state()
        for w in (self._refresh_worker, self._logs_worker, self._diskspace_worker,
                  self._queue_worker, *list(self._pref_workers), *list(_COMPONENT_WORKERS)):
            if w is not None:
                w.wait(2000)
        if self._tray:
            self._tray.hide()
        event.accept()
        QApplication.instance().quit()

    def _build_ui(self):
        root = QWidget()
        root.setObjectName("MainRoot")
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)
        toolbar = QFrame()
        toolbar.setObjectName("Toolbar")
        tl = QHBoxLayout(toolbar)
        tl.setContentsMargins(18, 10, 18, 10)
        tl.setSpacing(6)
        title = QLabel("Video Grabber")
        title.setObjectName("ToolbarTitle")
        tl.addWidget(title)
        tl.addSpacing(16)
        self._tb = []  # [button, icon, label]: compact mode swaps text for icon
        for label, action in [
            ("Add URL", self.add_download),
            ("Batch", self.add_batch),
            ("Playlist", self.add_playlist),
            ("Resume", lambda: self._selected("resume")),
            ("Pause", lambda: self._selected("pause")),
            ("Stop", lambda: self._selected("stop")),
            ("Delete", self.remove_selected),
            ("Queue", self.toggle_queue),
            ("Settings", self.open_settings),
        ]:
            b = QPushButton(label)
            b.setObjectName("ToolbarButton")
            b.setToolTip(label)
            b.clicked.connect(action)
            b.setText(label)  # guaranteed fallback if the icon never loads
            ic = _qicon(_TB_ICONS.get(label), _toolbar_icon_color())
            if ic is not None:
                b.setIcon(ic)
                b.setIconSize(QSize(16, 16))
            tl.addWidget(b)
            self._tb.append([b, ic, label])
            if label == "Queue":
                self.queue_btn = b
        self._update_queue_btn()
        tl.addStretch()
        self.search = QLineEdit()
        self.search.setObjectName("Search")
        self.search.setPlaceholderText("Search…")
        self.search.setClearButtonEnabled(True)
        # One filter pass per pause in typing, not one per keystroke: each pass
        # rebuilds the table and used to show/hide the detail pane in between.
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(120)
        self._search_timer.timeout.connect(lambda: self.apply_filter(self.search.text()))
        self.search.textChanged.connect(lambda _t: self._search_timer.start())
        tl.addWidget(self.search)
        root_layout.addWidget(toolbar)
        body = QWidget()
        body.setObjectName("FrameArea")
        bl = QHBoxLayout(body)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(0)
        self.sidebar = Sidebar()
        self.sidebar.category_selected.connect(self.select_category)
        self.sidebar.tool_clicked.connect(self._on_tool)
        self.sidebar.collapsed_changed.connect(self._on_sidebar_collapsed)
        self.sidebar.set_collapsed(bool(load_settings().get("sidebar_collapsed", False)))
        bl.addWidget(self.sidebar)
        content = QWidget()
        content.setObjectName("Content")
        cl = QVBoxLayout(content)
        cl.setContentsMargins(12, 12, 12, 10)
        cl.setSpacing(8)
        self.table = DownloadTable()
        _table_shadow = QGraphicsDropShadowEffect(self.table)
        _table_shadow.setBlurRadius(16)
        _table_shadow.setOffset(0, 4)
        _table_shadow.setColor(QColor(0, 0, 0, 80))
        self.table.setGraphicsEffect(_table_shadow)
        self.table.setModel(self.model)
        self.table.setItemDelegate(ProgressDelegate(self.table))
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.context_menu)
        self.table.verticalHeader().setVisible(False)
        hh = self.table.horizontalHeader()
        # Batch 5: every column, Name included, is user-resizable. The last
        # visual column absorbs spare width, and each column has a floor
        # (_clamp_section) so none can be dragged shut.
        for c in range(len(self.model.HEADERS)):
            hh.setSectionResizeMode(c, QHeaderView.Interactive)
        for c, w in {0: 360, 1: 80, 2: 120, 3: 90, 4: 140, 5: 90, 6: 120, 7: 70, 8: 120}.items():
            self.table.setColumnWidth(c, w)
        hh.setMinimumSectionSize(40)
        hh.setStretchLastSection(True)
        self._hdr_clamping = False
        hh.setSectionsMovable(True)
        hh.moveSection(7, 4)   # ETA shows after Speed; logical order is unchanged
        hh.moveSection(8, 1)   # Source sits right after Name
        self._restore_header_state()
        hh.sectionResized.connect(self._clamp_section)
        self._hdr_timer = QTimer(self)
        self._hdr_timer.setSingleShot(True)
        self._hdr_timer.timeout.connect(self._save_header_state)
        hh.sectionResized.connect(lambda *_: self._hdr_timer.start(800))
        hh.sectionMoved.connect(lambda *_: self._hdr_timer.start(800))
        self.table.setShowGrid(False)
        self.table.setDragDropMode(QAbstractItemView.DragOnly)
        self.table.doubleClicked.connect(self._on_double_click)
        # Batch 4 #2: table and details panel share a vertical splitter, so the
        # divider between them can be dragged (the height is remembered).
        self.split = QSplitter(Qt.Vertical)
        self.split.setChildrenCollapsible(False)
        self.split.setHandleWidth(4)
        self.table.setMinimumHeight(120)
        self.split.addWidget(self.table)
        cl.addWidget(self.split, 1)
        self.empty_label = QLabel("", self.table.viewport())
        self.empty_label.setObjectName("EmptyState")
        self.empty_label.setAlignment(Qt.AlignCenter)
        self.empty_label.setWordWrap(True)
        self.empty_label.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.empty_label.setVisible(False)
        self.table.viewport().installEventFilter(self)
        self.details = QLabel()
        self.details.setObjectName("Details")
        self.details.setTextFormat(Qt.RichText)
        self.details.setWordWrap(True)
        self.details.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.details.setVisible(False)
        self._log_lines = []
        self._thumb_req = None
        self.detail_pane = DetailPane(tabs_open=bool(_read_gui_state().get("detail_tabs_open", False)))
        self.detail_pane.setVisible(False)
        self.detail_pane.pause_clicked.connect(lambda: self._selected("pause"))
        self.detail_pane.resume_clicked.connect(lambda: self._selected("resume"))
        self.detail_pane.stop_clicked.connect(lambda: self._selected("stop"))
        self.detail_pane.open_clicked.connect(lambda: self._open_selected(folder=False))
        self.detail_pane.folder_clicked.connect(lambda: self._open_selected(folder="select"))
        self.detail_pane.copy_clicked.connect(self._copy_selected_url)
        self.detail_pane.tabs_toggled.connect(lambda on: _write_gui_state({"detail_tabs_open": bool(on)}))
        self._thumbs = ThumbLoader(gui_common.HOME / "thumbs", self)
        self._thumbs.ready.connect(self._on_thumb_ready, Qt.QueuedConnection)
        # The details label (several rows selected) and the detail pane share
        # the splitter's bottom half; it is hidden while neither is showing.
        self._bottom = _BottomPane()
        self._bottom.setObjectName("Plain")
        _bl = QVBoxLayout(self._bottom)
        _bl.setContentsMargins(0, 0, 0, 0)
        _bl.setSpacing(8)
        _bl.addWidget(self.details)
        _bl.addWidget(self.detail_pane, 1)
        self.split.addWidget(self._bottom)
        self.split.setStretchFactor(0, 1)
        self.split.setStretchFactor(1, 0)
        self._bottom.setVisible(False)
        self._bottom_shown = False
        self._split_timer = QTimer(self)
        self._split_timer.setSingleShot(True)
        self._split_timer.timeout.connect(self._save_split)
        self.split.splitterMoved.connect(lambda *_: self._split_timer.start(600))
        # While a box drag is live the selection changes on every mouse move;
        # the details panel waits for boxFinished instead of flickering.
        self.table.selectionModel().selectionChanged.connect(
            lambda *_: None if self.table.live_selecting else self._update_details())
        self.table.boxFinished.connect(self._update_details)
        from PySide6.QtGui import QShortcut, QKeySequence
        for seq, act in (("Alt+Up", "up"), ("Alt+Down", "down")):
            sc = QShortcut(QKeySequence(seq), self.table)
            sc.setContext(Qt.WidgetWithChildrenShortcut)
            sc.activated.connect(lambda a=act: self._reorder_selected(a))
        log_label = QLabel("ACTIVITY LOG")
        log_label.setObjectName("SectionLabel")
        self._log_label = log_label
        cl.addWidget(log_label)
        self.log = QLineEdit()
        self.log.setReadOnly(True)
        self.log.setObjectName("LogLine")
        cl.addWidget(self.log)
        bl.addWidget(content, 1)
        root_layout.addWidget(body, 1)
        self._root = root
        self.shell = None
        self.apply_shell()
        status = QStatusBar()
        status.setObjectName("BottomStatus")
        # The bar keeps its own height; the central widget gives way, not the bar.
        status.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        status.setMinimumHeight(max(24, status.sizeHint().height()))
        self.status_label = QLabel("● Connecting…")
        self.status_label.setObjectName("StatusLabel")
        status.addWidget(self.status_label)
        self.diskspace_label = QLabel("")
        self.diskspace_label.setObjectName("DiskSpaceLabel")
        self.diskspace_label.setToolTip("Free space on the downloads drive")
        self.hold_label = QLabel("")
        self.hold_label.setObjectName("HoldLabel")
        status.addPermanentWidget(self.hold_label)
        self.update_btn = QPushButton("")
        self.update_btn.setObjectName("UpdateBadge")
        self.update_btn.setFlat(True)
        self.update_btn.setCursor(Qt.PointingHandCursor)
        self.update_btn.setVisible(False)
        self.update_btn.clicked.connect(self._on_update_badge)
        status.addPermanentWidget(self.update_btn)
        status.addPermanentWidget(self.diskspace_label)
        self.version_label = QLabel(f"v{DISPLAY_VERSION} · PySide6 · Flask API")
        status.addPermanentWidget(self.version_label)
        self.setStatusBar(status)
        self._apply_blanc()
        menu = self.menuBar()
        menu.setNativeMenuBar(False)
        f = menu.addMenu("File")
        a = QAction("Add URL…", self); a.triggered.connect(self.add_download); f.addAction(a)
        a = QAction("Batch add…", self); a.triggered.connect(self.add_batch); f.addAction(a)
        a = QAction("Add playlist…", self); a.triggered.connect(self.add_playlist); f.addAction(a)
        f.addSeparator()
        a = QAction("Settings…", self); a.triggered.connect(self.open_settings); f.addAction(a)
        f.addSeparator()
        a = QAction("Exit", self); a.triggered.connect(self._real_quit); f.addAction(a)
        t = menu.addMenu("Tools")
        # v5.5: grouped into submenus. Every action keeps its handler, and the
        # Toggle handheld shell action keeps its Ctrl+Shift+S shortcut.
        tm = t.addMenu("Updates")
        a = QAction("Check for updates…", self); a.triggered.connect(self.check_for_updates); tm.addAction(a)
        a = QAction("Roll back yt-dlp…", self); a.triggered.connect(self.rollback_ytdlp); tm.addAction(a)
        a = QAction("Install update (supervised)…", self); a.triggered.connect(self._request_supervised_update); tm.addAction(a)
        tm = t.addMenu("Pairing")
        a = QAction("Pair Extension\u2026", self); a.triggered.connect(self.show_pair_dialog); tm.addAction(a)
        a = QAction("Copy pairing token", self); a.triggered.connect(self.copy_token); tm.addAction(a)
        a = QAction("Reload token", self); a.triggered.connect(self.reload_token); tm.addAction(a)
        tm = t.addMenu("Downloads")
        a = QAction("Bandwidth profiles…", self); a.triggered.connect(self.open_bandwidth_profiles); tm.addAction(a)
        a = QAction("Retry all failed", self); a.triggered.connect(self.retry_all_failed); tm.addAction(a)
        a = QAction("Clear failed downloads", self); a.triggered.connect(self.clear_failed); tm.addAction(a)
        a = QAction("Open logs folder", self); a.triggered.connect(self._open_logs_folder); tm.addAction(a)
        tm = t.addMenu("Media")
        a = QAction("Media Toolkit\u2026", self); a.triggered.connect(lambda: self.open_toolkit()); tm.addAction(a)
        a = QAction("Trim UI memory", self); a.triggered.connect(lambda: self.trim_ui_memory()); tm.addAction(a)
        tm = t.addMenu("App")
        a = QAction("Toggle handheld shell", self); a.setShortcut("Ctrl+Shift+S"); a.triggered.connect(self.toggle_shell); tm.addAction(a)
        a = QAction("What's New\u2026", self); a.triggered.connect(self.show_whats_new); tm.addAction(a)
        a = QAction("About Video Grabber…", self); a.triggered.connect(self.show_about); tm.addAction(a)
        a = QAction("Restart in Recovery Mode…", self); a.triggered.connect(self._restart_in_recovery); tm.addAction(a)

    def _apply_styles(self):
        # All styling now comes from gui_style.build_qss (app-wide), so the
        # window follows the Theme settings. Nothing is hardcoded here.
        self.setStyleSheet("")

    # ---------------------------------------------------------------- v5 ---
    _EMPTY_TEXT = {
        "Active": "Nothing is downloading right now.",
        "Queue": "Nothing is active or queued.",
        "Failed": "No failed downloads.",
        "Finished": "Nothing has finished yet.",
        "Unfinished": "No unfinished downloads.",
    }

    def _track_batch(self, items):
        """One tray notification when a run of 2+ downloads finishes, instead
        of one per file. 'Busy' = something is downloading or ready to start
        (jobs waiting out a retry delay don't count)."""
        now = time.time()
        done = {j.get("id") for j in items if j.get("status") == "done"}
        errs = {j.get("id") for j in items if j.get("status") == "error"}
        busy = any(j.get("status") == "downloading"
                   or (j.get("status") == "queued" and not (j.get("retry_after") or 0) > now)
                   for j in items)
        b = self._batch
        if not b["seeded"]:
            b.update(seeded=True, busy=busy, known_done=done, known_err=errs)
            return
        b["done"] += len(done - b["known_done"])
        b["failed"] += len(errs - b["known_err"])
        b["known_done"], b["known_err"] = done, errs
        if b["busy"] and not busy:
            total = b["done"] + b["failed"]
            if (total >= 2 and self._tray
                    and self._gui_settings.get("notify_queue_done", True)):
                msg = f"{b['done']} download(s) finished"
                if b["failed"]:
                    msg += f", {b['failed']} failed"
                self._tray.showMessage("Video Grabber", msg + ".",
                                       QSystemTrayIcon.Information, 4000)
            b["done"] = b["failed"] = 0
        elif not busy:
            b["done"] = b["failed"] = 0   # drift while idle (removals)
        b["busy"] = busy

    def _update_empty_state(self):
        if self.model.rowCount() > 0:
            self.empty_label.setVisible(False)
            return
        if self.search.text().strip():
            text = "No downloads match your search."
        elif self.current_filter in self._EMPTY_TEXT:
            text = self._EMPTY_TEXT[self.current_filter]
        elif self.current_filter == "All":
            text = ("No downloads yet.\nClick the pill on a video in your browser, "
                    "or press Add URL.")
        else:
            text = f"No {self.current_filter.lower()} downloads."
        self.empty_label.setText(text)
        self._place_empty_label()
        self.empty_label.setVisible(True)
        self.empty_label.raise_()

    def _place_empty_label(self):
        vp = self.table.viewport()
        self.empty_label.setGeometry(24, 40, max(60, vp.width() - 48), 90)

    def eventFilter(self, obj, event):
        try:
            if obj is self.table.viewport() and event.type() == event.Type.Resize:
                self._place_empty_label()
            # The table accepts drops for its own row reordering, which would
            # swallow file/URL drops before the window sees them; route those here.
            elif obj is self.table.viewport() and event.type() in (
                    event.Type.DragEnter, event.Type.DragMove, event.Type.Drop):
                md = event.mimeData()
                if md.hasUrls():
                    if event.type() == event.Type.Drop:
                        self._set_drop_cue(False)
                        self.dropEvent(event)
                    else:
                        self._set_drop_cue(True)
                        event.acceptProposedAction()
                    return True
            elif obj is self.table.viewport() and event.type() == event.Type.DragLeave:
                self._set_drop_cue(False)
        except Exception:
            pass
        return super().eventFilter(obj, event)

    # ---- drag and drop: local media -> Media Toolkit, links -> download queue
    def _set_drop_cue(self, on):
        """Batch 4 #5b: outline the table while a link or file is dragged over
        it (QSS: QTableView[dropActive="true"])."""
        on = bool(on)
        if bool(self.table.property("dropActive")) == on:
            return
        self.table.setProperty("dropActive", on)
        self.table.style().unpolish(self.table)
        self.table.style().polish(self.table)
        self.table.update()

    def dragEnterEvent(self, event):
        md = event.mimeData()
        if md.hasUrls() or md.hasText():
            self._set_drop_cue(True)
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        self.dragEnterEvent(event)

    def dragLeaveEvent(self, event):
        self._set_drop_cue(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event):
        self._set_drop_cue(False)
        from gui_toolkit import is_media_file
        md = event.mimeData()
        files, links = [], []
        for u in md.urls():
            if u.isLocalFile():
                files.append(u.toLocalFile())
            elif u.scheme() in ("http", "https"):
                links.append(u.toString())
        if not md.hasUrls() and md.hasText():
            links += [t for t in md.text().split() if t.lower().startswith(("http://", "https://"))]
        event.acceptProposedAction()
        if links:
            queued = 0
            for link in links:
                try:
                    self.api.add(link)
                    queued += 1
                except Exception as e:
                    QMessageBox.critical(self, "Add failed", str(e))
            if queued:
                self.statusBar().showMessage(
                    f"Queued {queued} link{'s' if queued != 1 else ''} from drop", 5000)
                self.refresh()
        media = [f for f in files if is_media_file(f)]
        if media:
            if len(media) > 1:
                self.statusBar().showMessage(
                    f"Opened the first of {len(media)} dropped files in the Media Toolkit", 5000)
            # Deferred: opening a modal dialog inside dropEvent would block the
            # drag source (Explorer) until the dialog closes.
            QTimer.singleShot(0, lambda p=media[0]: self.open_toolkit(path=p))
        elif files and not links:
            self.statusBar().showMessage(
                f"Not a media file: {Path(files[0]).name}", 5000)

    def _apply_blanc(self):
        """Blanc minimal mode (Settings > General). Hides the Activity Log,
        the toolbar text labels (icons stay) and the status-bar extras (hold
        notice and version text); the connection indicator and disk space
        stay. The sidebar, detail pane, table and search are never touched,
        so this can't fight sidebar collapse or the Queue view."""
        on = bool(load_settings().get("blanc_mode", False))
        self._blanc = on
        for w in (self.hold_label, self.version_label):
            w.setVisible(not on)
        # The Activity Log's visibility is decided together with the text
        # scale (Batch 3): either Blanc or a scale above 115% hides it.
        self._apply_text_scale_layout()

    def _apply_text_scale_layout(self):
        """Batch 3: smart responsive scaling. While Text size is above 115%
        the main window hands the space back so the layout still fits at
        150% without a horizontal scrollbar: the toolbar goes icons-only,
        the Activity Log hides, and the two least important table columns
        (ETA, logical 7; Source, logical 8) hide. At or below 115% all of
        it is restored. Blanc mode composes on top of this (it is checked
        again here), never against it. Called from _apply_blanc(), which
        runs at startup and on every theme/settings re-apply."""
        try:
            scale = int(load_settings().get("text_scale", 100) or 100)
        except (TypeError, ValueError):
            scale = 100
        big = scale > 115
        self._scale_compact = big
        log_on = not big and not getattr(self, "_blanc", False)
        for w in (getattr(self, "_log_label", None), getattr(self, "log", None)):
            if w is not None:
                w.setVisible(log_on)
        table = getattr(self, "table", None)
        if table is not None:
            table.setColumnHidden(7, big)   # ETA
            table.setColumnHidden(8, big)   # Source
        self._apply_toolbar_text()

    def _apply_toolbar_text(self):
        extra = 262 if getattr(self, "shell", None) else 0   # the shell eats width
        compact = (getattr(self, "_blanc", False)
                   or getattr(self, "_scale_compact", False)
                   or (self.width() - extra) < 1240)
        col = _toolbar_icon_color()
        for entry in getattr(self, "_tb", []):
            btn, label = entry[0], entry[2]
            ic = _qicon(_TB_ICONS.get(label), col)
            if ic is not None:
                entry[1] = ic
                btn.setIcon(ic)
                btn.setIconSize(QSize(16, 16))
                btn.setText("" if compact else label)
            else:
                # no icon for this label: show text so the button doesn't vanish
                btn.setIcon(QIcon())
                btn.setText(label)
            btn.setToolTip(label)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply_toolbar_text()

    # Minimum width per logical column (Name, Size, Progress, Speed, Status,
    # Category, Completed, ETA, Source).
    _COL_MIN = {0: 160, 1: 60, 2: 100, 3: 70, 4: 110, 5: 70, 6: 100, 7: 60, 8: 80}

    def _clamp_section(self, logical, old, new):
        if getattr(self, "_hdr_clamping", False):
            return
        hh = self.table.horizontalHeader()
        if hh.stretchLastSection() and hh.visualIndex(logical) == hh.count() - 1:
            return      # the stretched column is sized by Qt
        lo = self._COL_MIN.get(logical, 60)
        if new < lo:
            self._hdr_clamping = True
            try:
                hh.resizeSection(logical, lo)
            finally:
                self._hdr_clamping = False

    def _restore_header_state(self):
        # Saved under "table_header_v2": the old "table_header" (Name was a
        # fixed Stretch column) is ignored, which resets the layout once.
        try:
            raw = _read_gui_state().get("table_header_v2")
            if not raw:
                return
            hh = self.table.horizontalHeader()
            if hh.restoreState(QByteArray(base64.b64decode(raw))):
                for c in range(len(self.model.HEADERS)):
                    hh.setSectionResizeMode(c, QHeaderView.Interactive)
                    if hh.sectionSize(c) < self._COL_MIN.get(c, 60):
                        hh.resizeSection(c, self._COL_MIN.get(c, 60))
                hh.setStretchLastSection(True)
        except Exception:
            pass

    def _save_header_state(self):
        try:
            hh = self.table.horizontalHeader()
            _write_gui_state({"table_header_v2": base64.b64encode(
                bytes(hh.saveState())).decode("ascii")})
        except Exception:
            pass

    def _sync_bottom(self):
        """Show the splitter's bottom half only while the details label or the
        detail pane is showing; restore the saved divider position when it
        first appears."""
        show = (self.details.isVisibleTo(self._bottom)
                or self.detail_pane.isVisibleTo(self._bottom))
        self._bottom.setVisible(show)
        if show and not self._bottom_shown:
            self._bottom_shown = True
            QTimer.singleShot(0, self._restore_split)
        elif not show:
            self._bottom_shown = False

    def _restore_split(self):
        h = int(_read_gui_state().get("detail_height", 0) or 0)
        total = self.split.height()
        if h > 0 and total > 0:
            h = max(self._bottom.minimumSizeHint().height(), min(h, total - 120))
            self.split.setSizes([total - h, h])

    def _save_split(self):
        sizes = self.split.sizes()
        if self._bottom.isVisible() and len(sizes) == 2 and sizes[1] > 0:
            _write_gui_state({"detail_height": int(sizes[1])})

    def _update_details(self):
        self._update_details_inner()
        self._sync_bottom()
        sb = self.statusBar()          # safety net: the bottom bar is never left hidden
        if sb is not None and self.isVisible() and not sb.isVisible():
            sb.show()

    def _selection_snapshot(self):
        """(selected job ids, current job id) before a table rebuild."""
        try:
            ids = {j["id"] for j in self._selected_rows()}
        except Exception:
            ids = set()
        ci = self.table.currentIndex()
        cur = None
        if ci.isValid() and ci.row() < len(self.model.items):
            cur = self.model.items[ci.row()].get("id")
        return ids, cur

    def _update_details_inner(self):
        """Panel under the table: the detail pane for one selected row, a
        one-line count for several."""
        try:
            rows = self._selected_rows()
        except Exception:
            rows = []
        if not rows:
            self.details.setVisible(False)
            self.detail_pane.setVisible(False)
            return
        if len(rows) > 1:
            self.detail_pane.setVisible(False)
            self.details.setText(f"{len(rows)} downloads selected")
            self.details.setVisible(True)
            return
        j = rows[0]
        self.details.setVisible(False)
        self.detail_pane.set_job(_detail_info(j))
        self.detail_pane.setVisible(True)
        req = (j.get("id"), j.get("status") == "done")
        if req != self._thumb_req:
            self._thumb_req = req
            if req[1]:
                try:
                    self._thumbs.request(j["id"], _path_for(j), _ffmpeg_dir())
                except Exception:
                    pass
        self._push_job_log(j.get("id"))

    def _push_job_log(self, job_id):
        pat = re.compile(rf"\bjob\s+{re.escape(str(job_id))}\b", re.I)
        self.detail_pane.set_log_lines([ln for ln in self._log_lines if pat.search(ln)][-100:])

    def _on_thumb_ready(self, job_id, path, duration):
        self.detail_pane.set_thumb(job_id, path, duration)

    def _copy_selected_url(self):
        rows = self._selected_rows()
        if len(rows) == 1:
            QApplication.clipboard().setText(str(rows[0].get("url") or ""))

    def rollback_ytdlp(self):
        """Tools > Updates > Roll back yt-dlp (same action as Settings > Updates)."""
        if QMessageBox.question(self, "Roll back yt-dlp",
                                "Go back to the yt-dlp version that shipped with the app? "
                                "This applies the next time Video Grabber starts.",
                                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        w = _ApiFetchWorker(self.api.ytdlp_rollback, self)
        w.result.connect(lambda d: QMessageBox.information(
            self, "Roll back yt-dlp",
            "Override removed. The bundled yt-dlp is used after restart."
            if (d or {}).get("removed") else
            "Nothing to roll back; already on the bundled version."), Qt.QueuedConnection)
        w.failed.connect(lambda: QMessageBox.warning(
            self, "Roll back yt-dlp", "Couldn't reach the app backend."), Qt.QueuedConnection)
        self._rb_worker = w
        w.start()

    def _save_pref(self, key, value):
        """Persist one setting through the backend without blocking the UI."""
        w = _ApiFetchWorker(lambda: self.api.save_setting(key, value), self)
        self._pref_workers.add(w)
        w.finished.connect(lambda w=w: self._pref_workers.discard(w))
        w.start()

    def _on_sidebar_collapsed(self, flag):
        self._save_pref("sidebar_collapsed", bool(flag))

    def _on_tool(self, key):
        {"settings": self.open_settings, "help": self.show_help, "about": self.show_about}[key]()

    def show_help(self):
        # v5.5: sectioned Help dialog (capabilities, FAQ, troubleshooting, guides).
        HelpDialog(self, pair_fn=self.show_pair_dialog, whats_new_fn=self.show_whats_new).exec()

    def show_pair_dialog(self):
        """Pair Extension: Help dialog button and Tools > Pairing."""
        PairDialog(self, getattr(self.api, "token", "")).exec()

    def show_whats_new(self):
        """What's New: Help dialog button and Tools > App."""
        WhatsNewDialog(self, self.api).exec()

    def refresh_queue_status(self):
        if self._queue_worker is not None:
            return
        self._queue_worker = _ApiFetchWorker(self.api.queue_status, self)
        self._queue_worker.result.connect(self._on_queue_status, Qt.QueuedConnection)
        self._queue_worker.finished.connect(lambda: setattr(self, "_queue_worker", None))
        self._queue_worker.start()

    def _on_queue_status(self, data):
        try:
            parts = []
            hold = int((data or {}).get("youtube_hold_s") or 0)
            if hold > 0:
                parts.append(f"\u23f3 YouTube held {max(1, math.ceil(hold / 60))} min")
            if (data or {}).get("low_disk"):
                parts.append("\u26a0 Low disk space: new downloads paused")
            self.hold_label.setText("  \u00b7  ".join(parts))
            self.hold_label.setToolTip(
                "YouTube jobs are held after a bot check or rate limit. Press Resume "
                "on a YouTube job (for example after switching VPN) to retry now."
                if hold > 0 else "")
        except Exception:
            pass

    def _on_update_available(self, tag):
        # The status-bar badge is no longer the prompt; the dialog is.
        self._update_tag = tag
        self.update_btn.setText(f"\u2b06 Update {tag} available")
        self.update_btn.setVisible(False)
        if self._tray and self._update_notified != tag:
            self._update_notified = tag
            self._tray.showMessage(
                "Video Grabber update",
                f"{tag} is available. Open Video Grabber to install.",
                QSystemTrayIcon.Information, 5000)
        self._maybe_show_update_dialog(tag)

    # ---- update prompt (dialog) -------------------------------------------
    def _maybe_show_update_dialog(self, tag, force=False):
        """Show the update prompt for tag, unless that version was skipped or
        a Remind-Me-Later snooze is still running. force=True ignores both."""
        if not tag:
            return
        st = _read_gui_state()
        if not force:
            if st.get("skipped_update_tag") == tag:
                return
            wait = float(st.get("update_snooze_until") or 0) - time.time()
            if wait > 0:
                self._schedule_update_prompt(tag, wait)
                return
        if self._update_dialog_open:
            return
        self._update_dialog_open = True
        try:
            dlg = UpdateDialog(self, tag, DISPLAY_VERSION)
            dlg.exec()
            choice = dlg.choice
        finally:
            self._update_dialog_open = False
        if choice == "now":
            self.check_for_updates()
        elif choice == "skip":
            # Saved in gui_state.json (the /settings route only takes keys
            # it already knows, and the GUI state file is always writable).
            _write_gui_state({"skipped_update_tag": tag})
            try:
                import updater
                updater.skip_version(tag)
            except Exception:
                pass
        else:  # "later": 4-hour snooze, saved so it survives a restart
            _write_gui_state({"update_snooze_until": time.time() + 4 * 3600})
            self._schedule_update_prompt(tag, 4 * 3600)

    def _schedule_update_prompt(self, tag, seconds):
        self._snooze_gen += 1
        gen = self._snooze_gen

        def _fire():
            if gen == self._snooze_gen:  # a newer schedule replaces this one
                self._maybe_show_update_dialog(tag)
        QTimer.singleShot(int(seconds * 1000) + 1000, _fire)

    # ---- changelog after an update -----------------------------------------
    def _check_version_changelog(self):
        """First start after an app update (the version differs from the one
        this GUI saw last): fetch the release notes for the Activity Log.
        A first run only records the version."""
        last = _read_gui_state().get("last_seen_version", "")
        if last == DISPLAY_VERSION:
            return
        if not last:
            _write_gui_state({"last_seen_version": DISPLAY_VERSION})
            return
        self._changelog_worker = _ApiFetchWorker(self.api.changelog, self)
        self._changelog_worker.result.connect(self._on_changelog, Qt.QueuedConnection)
        self._changelog_worker.failed.connect(
            lambda: self._on_changelog({"ok": False, "error": "backend unreachable"}),
            Qt.QueuedConnection)
        self._changelog_worker.start()

    def _on_changelog(self, payload):
        payload = payload or {}
        if payload.get("ok"):
            # Marked seen only once the notes arrive, so an offline start
            # simply tries again on the next launch.
            _write_gui_state({"last_seen_version": DISPLAY_VERSION})
            lines = [f"What's new in v{DISPLAY_VERSION}:"]
            for raw in str(payload.get("body") or "").splitlines():
                t = raw.strip()
                if not t:
                    continue
                t = re.sub(r"^#+\s*", "", t)
                t = re.sub(r"^[-*+]\s+", "\u2022 ", t)
                lines.append(t)
            if len(lines) == 1:
                lines.append("(this release has no notes)")
        else:
            lines = [f"Updated to v{DISPLAY_VERSION}. Couldn't load the release notes "
                     f"({payload.get('error') or 'unknown error'})."]
        self._activity_feed.extend(lines[:25])
        self._activity_tick()
        self._activity_timer.start()

    def _activity_tick(self):
        if not self._activity_feed:
            self._activity_timer.stop()
            return
        self.log.setText(self._activity_feed.popleft())

    def _on_update_badge(self):
        m = QMenu(self)
        a_now = m.addAction("Install now (restarts the app)")
        a_skip = m.addAction("Skip this version")
        chosen = m.exec(self.update_btn.mapToGlobal(self.update_btn.rect().topLeft()))
        if chosen == a_now:
            self.check_for_updates()
        elif chosen == a_skip:
            try:
                import updater
                updater.skip_version(self._update_tag)
            except Exception:
                pass
            self.update_btn.setVisible(False)

    def clear_failed(self):
        n = sum(1 for j in self.all_items if j.get("status") == "error")
        if not n:
            QMessageBox.information(self, "Clear failed", "No failed downloads to clear.")
            return
        if QMessageBox.question(self, "Clear failed",
                                f"Remove {n} failed download(s) from the list?",
                                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return

        def _do():
            try:
                self.api.clear_failed()
            except Exception:
                pass
        threading.Thread(target=_do, daemon=True).start()
        self.log.setText(f"Clearing {n} failed download(s)\u2026")

    def _tick_animations(self):
        if not self.animations_enabled:
            return
        try:
            now = time.time()
            if any(j.get("status") == "downloading"
                   or (j.get("retry_after") or 0) > now for j in self.all_items):
                self.table.viewport().update()
        except RuntimeError:
            self._anim_timer.stop()  # widgets torn down during exit

    def trim_ui_memory(self, silent=False):
        """Drop caches that only grow: favicon pixmaps, done/fade bookkeeping
        for jobs that no longer exist, the yt-dlp format-audio map, and any
        stale QTableWidget rows left in closed dialogs. Safe to run any time;
        everything is rebuilt on demand."""
        import gc
        freed = {}
        try:
            live = {j.get("id") for j in getattr(self, "all_items", [])}
            freed["icons"] = len(gui_palette._SITE_ICONS)
            gui_palette._SITE_ICONS.clear()
            before = len(self._done_seen)
            self._done_seen &= live
            freed["done"] = before - len(self._done_seen)
            for jid in [k for k in self._recently_done if k not in live]:
                self._recently_done.pop(jid, None)
            try:
                import engines
                freed["formats"] = len(engines._FORMAT_AUDIO)
                engines._FORMAT_AUDIO.clear()
            except Exception:
                pass
            # Orphaned QTableWidgets (batch/playlist pickers that were closed
            # but not yet garbage collected) keep every cell item alive.
            for tw in self.findChildren(QTableWidget):
                try:
                    if not tw.isVisible():
                        tw.clearContents()
                        tw.setRowCount(0)
                except RuntimeError:
                    pass
            self.table.viewport().update()
        except Exception as e:
            print(f"trim_ui_memory failed: {e}")
        gc.collect()
        if not silent:
            self.statusBar().showMessage(
                f"UI memory trimmed ({freed.get('icons', 0)} icons, "
                f"{freed.get('formats', 0)} format maps cleared)", 4000)

    def refresh(self):
        # 11.1: HTTP off the GUI thread. If a fetch is still in flight
        # (slow backend), skip this tick instead of piling up requests.
        if self._refresh_worker is not None:
            return

        def fetch():
            return self.api.jobs(), load_settings()

        self._refresh_worker = _ApiFetchWorker(fetch, self)
        self._refresh_worker.result.connect(self._on_refreshed, Qt.QueuedConnection)
        self._refresh_worker.failed.connect(self._on_refresh_failed, Qt.QueuedConnection)
        self._refresh_worker.finished.connect(self._refresh_worker_done)
        self._refresh_worker.start()

    def _refresh_worker_done(self):
        self._refresh_worker = None

    def _on_refresh_failed(self):
        self.status_label.setText("● Backend offline")
        self.status_label.setStyleSheet(f"color: {gui_palette.DANGER}; padding-left: 10px;")

    def _on_refreshed(self, payload):
        items, settings = payload
        self.all_items = items
        self.sidebar.set_counts(_sidebar_counts(items, settings.get("hide_old_errors_hours", 24)))
        self._gui_settings = settings
        self._track_batch(items)
        # 11.3: re-read every tick — a Settings-dialog toggle propagates
        # within ~1s; nothing is cached from startup.
        self._gui_settings = settings
        self.animations_enabled = bool(settings.get("animations_enabled", True))
        self.detail_pane.set_play_prefs(bool(settings.get("inline_play_muted", False)),
                                        settings.get("thumb_click_mode", "inline"))
        delegate = self.table.itemDelegate()
        if delegate is not None:
            delegate.animations_enabled = self.animations_enabled
        self.status_label.setText("● Connected")
        self.status_label.setStyleSheet(f"color: {gui_palette.SUCCESS}; padding-left: 10px;")
        for j in self.all_items:
            jid = j.get("id")
            if j.get("status") == "done" and jid not in self._done_seen:
                self._done_seen.add(jid)
                if getattr(self, "_done_seeded", False):
                    self._recently_done[jid] = 1.0
        self._done_seeded = True
        for j in self.all_items:
            jid = j.get("id")
            if j.get("status") == "error" and jid not in self._error_seen:
                self._error_seen.add(jid)
                if getattr(self, "_error_seeded", False):
                    self._apply_reactive_color("error")
        self._error_seeded = True
        # Tray icon reflects download activity (cached icon swap, ~1/sec).
        if getattr(self, "_tray", None):
            active = any(j.get("status") == "downloading" for j in self.all_items)
            self._tray.setIcon(_make_tray_icon(active))
        # Snapshot the selection before the model reset wipes it, then restore.
        selected_ids, current_id = self._selection_snapshot()
        self.apply_filter(self.search.text())
        self.table.restore_selection(selected_ids, current_id)
        # 12.1: rows inserted/removed -> fade the viewport in (150 ms).
        visible_ids = frozenset(j["id"] for j in self.model.items)
        if visible_ids != getattr(self, "_last_visible_ids", None):
            self._last_visible_ids = visible_ids
            _fade_widget(self.table.viewport(), self.animations_enabled)
        self._update_details()

    def _apply_reactive_color(self, status):
        """Batch 4 reactivity: while a job is in the error state, flash
        the theme accent red, then restore the saved theme after 3 s."""
        if status != "error":
            return
        from gui_style import build_qss
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(build_qss(load_settings(), override_tokens={
                "accent": "#ef5350", "accent_dim": "#b71c1c"}))
        QTimer.singleShot(3000, self._reset_reactive_color)

    def _reset_reactive_color(self):
        try:
            reapply_theme()
        except RuntimeError:
            pass  # window closed before the reset fired

    def _tick_fade(self):
        if not self._recently_done:
            return
        done = []
        for jid, val in list(self._recently_done.items()):
            val -= 0.04
            if val <= 0:
                done.append(jid)
            else:
                self._recently_done[jid] = val
        for jid in done:
            self._recently_done.pop(jid, None)
        self.table.viewport().update()

    def refresh_logs(self):
        if self._logs_worker is not None:
            return
        self._logs_worker = _ApiFetchWorker(self.api.logs, self)
        self._logs_worker.result.connect(self._on_logs_refreshed, Qt.QueuedConnection)
        self._logs_worker.finished.connect(lambda: setattr(self, "_logs_worker", None))
        self._logs_worker.start()

    def _on_logs_refreshed(self, lines):
        try:
            if lines and not self._activity_timer.isActive():
                self.log.setText(lines[-1])
            self._log_lines = [str(x) for x in (lines or [])]
            if self.detail_pane.isVisible() and self.detail_pane.job_id is not None:
                self._push_job_log(self.detail_pane.job_id)
        except Exception:
            pass

    def refresh_diskspace(self):
        if self._diskspace_worker is not None:
            return
        self._diskspace_worker = _ApiFetchWorker(self.api.disk_space, self)
        self._diskspace_worker.result.connect(self._on_diskspace_refreshed, Qt.QueuedConnection)
        self._diskspace_worker.finished.connect(lambda: setattr(self, "_diskspace_worker", None))
        self._diskspace_worker.start()

    def _on_diskspace_refreshed(self, data):
        try:
            free = (data or {}).get("free")
            total = (data or {}).get("total")
            if not free or not total:
                self.diskspace_label.setText("")
                return
            pct_used = 100 * (1 - free / total) if total else 0
            text = f"💾 {fmt_bytes(free)} free"
            self.diskspace_label.setText(text)
            # Low-space warning: red once free space drops under 2 GB or 95%
            # of the drive is used — either one usually means "about to fail".
            low = free < 2 * 1024**3 or pct_used > 95
            self.diskspace_label.setStyleSheet(
                f"color: {gui_palette.DANGER};" if low else f"color: {gui_palette.MUTED};")
            self.diskspace_label.setToolTip(
                f"{fmt_bytes(free)} free of {fmt_bytes(total)} on the downloads drive"
                + ("  —  running low!" if low else ""))
        except Exception:
            pass

    def _on_new_job(self, job):
        # Silent auto-queue (skip_add_dialog) gives no other feedback when the
        # window is minimized or in the background: one tray toast, at most
        # one per 3 seconds.
        try:
            if (self._tray and (self.isMinimized() or not self.isActiveWindow())
                    and time.time() - getattr(self, "_last_queued_toast", 0.0) >= 3.0):
                self._last_queued_toast = time.time()
                name = (job or {}).get("filename") or (job or {}).get("url") or "download"
                self._tray.showMessage("Video Grabber", f"Queued {name}",
                                       QSystemTrayIcon.Information, 3000)
        except Exception:
            pass
        if not self._gui_settings.get("force_on_top", True):
            return
        # Already focused — raising would only cause a flicker.
        if self.isVisible() and not self.isMinimized() and self.isActiveWindow():
            return
        if self.isMinimized():
            self.showNormal()
        self.raise_()
        self.activateWindow()

    def open_bandwidth_profiles(self):
        try:
            dlg = BandwidthProfilesDialog(self, self.api)
            _fade_dialog(dlg, self.animations_enabled)
            dlg.exec()
        except Exception as e:
            QMessageBox.critical(self, "Bandwidth profiles error", str(e))

    def show_about(self):
        QMessageBox.about(
            self, "About Video Grabber",
            f"<h3 style='margin-bottom:2px;'>Video Grabber</h3>"
            f"<p style='color:{gui_palette.MUTED};margin-top:0;'>Version {DISPLAY_VERSION}</p>"
            f"<p>A personal video downloader — a Chrome/Vivaldi extension "
            f"paired with this desktop app, built on yt-dlp and Streamlink.</p>"
            f"<p style='color:{gui_palette.MUTED};font-size:11px;'>PySide6 · Flask · {BACKEND_BASE}</p>")

    def retry_all_failed(self):
        failed = [j["id"] for j in self.all_items if j.get("status") == "error"]
        if not failed:
            QMessageBox.information(self, "Retry all failed",
                                    "No failed downloads to retry.")
            return

        def _do():
            for jid in failed:
                try:
                    self.api.redownload(jid)
                except Exception:
                    pass
        threading.Thread(target=_do, daemon=True).start()
        self.log.setText(f"Retrying {len(failed)} failed download(s)…")

    def check_for_updates(self):
        """All update entry points (Tools menu, the startup prompt's
        "now", the update badge's "Install now") land here — and all of
        them go through the supervisor (§5). The old in-process
        updater download-and-swap path staged a swap with no blacklist
        check, no backup and no manifest verify, so it is gone: the
        supervised updater is blacklist-aware, backs up first and
        verifies the package before swapping. Only works in the frozen
        (installed) build; the source run just tells the user to use
        git."""
        if not getattr(sys, "frozen", False):
            QMessageBox.information(
                self, "Check for updates",
                "Updates only apply to the installed (exe) build.\n"
                "Source runs should pull from git instead.")
            return
        self._request_supervised_update()

    def open_settings(self):
        try:
            dlg = SettingsDialog(self, self.api)
            _fade_dialog(dlg, self.animations_enabled)
            dlg.exec()
        except Exception as e:
            QMessageBox.critical(self, "Settings error", str(e))

    # ---- optional handheld shell (gui_shell.py) ----
    def apply_shell(self):
        """Wrap the main view in the handheld shell, or unwrap it, per the
        shell_enabled / shell_color settings. Safe to call repeatedly."""
        st = load_settings()
        sig = None
        if st.get("shell_enabled"):
            # The shell bakes the theme accent in at construction, so a
            # changed accent also counts as "no longer matches".
            sig = (st.get("shell_color", "cream"), resolve_palette(st)["accent"])
            if self.shell is not None and getattr(self, "_shell_sig", None) == sig:
                # Same shell: just refresh the parts that have setters.
                self.shell.set_overrides(st.get("shell_colors"))
                self.shell.set_backlight(bool(st.get("shell_backlight", True)))
                self._root.show()
                self._apply_toolbar_text()
                return
        old = self.takeCentralWidget()
        if old is not None and old is not self._root:
            old.layout().removeWidget(self._root)
            self._root.setParent(None)
            old.deleteLater()
        if st.get("shell_enabled"):
            from gui_shell import ShellFrame
            self.shell = ShellFrame(self._root, st.get("shell_color", "cream"),
                                    backlight=bool(st.get("shell_backlight", True)),
                                    overrides=st.get("shell_colors"), theme=st)
            self.shell.dpad.connect(self._shell_dpad)
            self.shell.button.connect(self._shell_button)
            self.setCentralWidget(self.shell)
            self._shell_sig = sig
        else:
            self.shell = None
            self._shell_sig = None
            self.setCentralWidget(self._root)
        self._root.show()
        self._apply_toolbar_text()

    def _shell_dpad(self, d):
        if d in ("up", "down"):
            n = self.model.rowCount()
            if not n:
                return
            cur = self.table.currentIndex().row()
            self.table.selectRow(max(0, min(n - 1, (0 if cur < 0 else cur) + (-1 if d == "up" else 1))))
        else:
            names = list(self.sidebar.buttons.keys())
            i = names.index(self.current_filter) if self.current_filter in names else 0
            self.select_category(names[(i + (-1 if d == "left" else 1)) % len(names)])

    def _shell_button(self, name):
        if name == "start":
            self.toggle_queue()
        elif name == "select":
            self.search.setFocus()
            self.search.selectAll()
        elif name == "power":
            self._toggle_backlight()
        elif self._selected_rows():
            self._selected("resume" if name == "a" else "pause")

    def _toggle_backlight(self):
        """Power light click: flip the shell's backlight and remember it."""
        if not self.shell:
            return
        on = not self.shell.backlight
        if self.api:
            try:
                self.api.save_setting("shell_backlight", on)
            except Exception as e:
                QMessageBox.warning(self, "Error", str(e))
                return
        self.shell.set_backlight(on)

    def toggle_queue(self):
        """Flip the dispatcher's queue_running flag via /queue-running."""
        running = not bool(load_settings().get("queue_running", True))
        if self.api:
            try:
                self.api.set_queue_running(running)
            except Exception as e:
                QMessageBox.warning(self, "Error", str(e))
        # Pass the value we just set — re-reading from disk here can race
        # the server's save and show the stale label until next refresh.
        self._update_queue_btn(running)

    def _update_queue_btn(self, running=None):
        if not hasattr(self, "queue_btn"):
            return
        if running is None:
            running = bool(load_settings().get("queue_running", True))
        for entry in getattr(self, "_tb", []):
            if entry[0] is self.queue_btn:
                entry[2] = "Pause Queue" if running else "Start Queue"
                entry[1] = _qicon(_TB_ICONS.get(entry[2]), _toolbar_icon_color())
        self._apply_toolbar_text()
        self.queue_btn.setToolTip(
            "Pause the whole download queue" if running
            else "Resume the whole download queue")

    def _on_header_clicked(self, col):
        if col == self._sort_column:
            self._sort_ascending = not self._sort_ascending
        else:
            self._sort_column = col
            self._sort_ascending = True
        self.model.headerDataChanged.emit(Qt.Horizontal, 0, len(self.model.HEADERS) - 1)
        self.apply_filter(self.search.text())

    def apply_filter(self, text):
        text = (text or "").lower().strip()
        src = self.all_items
        f = self.current_filter
        if f in ("All", "Unfinished") and not text:
            try:
                hrs = float(self._gui_settings.get("hide_old_errors_hours", 24) or 0)
            except (TypeError, ValueError):
                hrs = 0
            if hrs > 0:
                cutoff = time.time() - hrs * 3600
                src = [x for x in src
                       if not (x.get("status") == "error"
                               and (x.get("failed_ts") or x.get("created_ts") or 0) < cutoff)]
        if f == "Active":
            src = [x for x in src if _is_active(x)]
        elif f == "Queue":
            src = [x for x in src if _in_queue_view(x)]
        elif f == "Failed":
            src = [x for x in src if x["status"] == "error"]
        elif f == "Finished":
            src = [x for x in src if x["status"] == "done"]
        elif f == "Unfinished":
            src = [x for x in src if x["status"] != "done"]
        elif f != "All":
            src = [x for x in src if x["category"] == f]
        if text:
            def matches(x):
                hay = " ".join([
                    x.get("filename", ""),
                    x.get("category", ""),
                    x.get("status", ""),
                    x.get("url", ""),
                    x.get("playlist_title") or "",
                ]).lower()
                return text in hay
            src = [x for x in src if matches(x)]
        # Sort runs after filter+search so it only reorders visible rows.
        if self._sort_column is not None:
            def sort_key(x):
                c = self._sort_column
                if c == 0:
                    return x.get("filename", "").lower()
                if c == 1:
                    return x.get("size_total", 0) or 0
                if c == 2:
                    total = x.get("size_total", 0) or 0
                    return (x.get("size_done", 0) / total) if total else 0
                if c == 3:
                    m = re.match(r"([\d.]+)", x.get("speed", "") or "")
                    return float(m.group(1)) if m else 0
                if c == 4:
                    return x.get("status", "")
                if c == 5:
                    return x.get("category", "").lower()
                if c == 6:
                    return x.get("completed_ts", 0) or 0
                if c == 7:
                    eta = _eta_seconds(x)
                    return eta if eta is not None else 1e12
                if c == 8:
                    return (x.get("source_site") or "").lower()
                return 0
            src = sorted(src, key=sort_key, reverse=not self._sort_ascending)
        else:
            # A playlist batch shares one created_ts: playlist_index keeps it
            # in playlist order (001 first) instead of arbitrary.
            src = sorted(src, key=lambda x: (-(x.get("created_ts") or 0),
                                             x.get("playlist_index") or 0))
        _ids, _cur = self._selection_snapshot()
        self.model.update_items(src)
        self.table.restore_selection(_ids, _cur)   # a rebuild clears the selection and the Shift anchor
        self._update_empty_state()
        self._update_details()                     # detail pane matches what is still selected

    def select_category(self, name):
        self.current_filter = name
        for n, b in self.sidebar.buttons.items():
            b.setProperty("active", n == name)
            b.style().unpolish(b)
            b.style().polish(b)
        self.apply_filter(self.search.text())

    def _selected_rows(self):
        sm = self.table.selectionModel()
        rows = sorted({i.row() for i in sm.selectedRows()})
        if not rows:
            # Ctrl+click can select individual cells without whole rows —
            # fall back to any selected index so batch delete still works.
            rows = sorted({i.row() for i in sm.selectedIndexes()})
        return [self.model.items[r] for r in rows if r < len(self.model.items)]

    def _selected(self, action):
        if not self._selected_rows():
            QMessageBox.information(self, "No job selected", "Select a row first.")
            return
        for j in self._selected_rows():
            try:
                getattr(self.api, action)(j["id"])
            except Exception as e:
                QMessageBox.warning(self, "Error", str(e))
        self.refresh()

    def remove_selected(self):
        items = self._selected_rows()
        if not items:
            QMessageBox.information(self, "No job selected", "Select a row first.")
            return
        if QMessageBox.question(self, "Remove", f"Remove {len(items)} job(s)?") != QMessageBox.Yes:
            return
        for j in items:
            try:
                self.api.delete(j["id"])
            except Exception as e:
                QMessageBox.warning(self, "Error", str(e))
        self.refresh()

    def add_download(self):
        d = AddDownloadDialog(self, api=self.api)
        _fade_dialog(d, self.animations_enabled)
        result = d.exec()
        if result not in (QDialog.Accepted, 2):
            return
        v = d.values()
        if not v or not v[0]["url"]:
            return
        common = v[0]
        if common.get("remember") and common.get("save_path"):
            try:
                self.api.save_setting("per_category_dirs",
                                      {common["category"]: common["save_path"]})
            except Exception:
                pass
        if common.get("download_playlist"):
            self._open_playlist_picker(prefill_url=common["url"])
            return
        total = len(v)
        for item in v:
            try:
                self.api.add(item["url"], category=item["category"],
                             description=item["description"],
                             format_id=item.get("format_id"),
                             target_format=item.get("target_format"),
                             resolution=item.get("resolution"),
                             download_playlist=item.get("download_playlist", False),
                             multi=total > 1)
            except Exception as e:
                QMessageBox.critical(self, "Add failed", str(e))
        self.refresh()

    def add_playlist(self):
        self._open_playlist_picker()

    def _open_playlist_picker(self, prefill_url="", referer=None, cookie=None,
                              user_agent=None):
        """Modal playlist picker; queues the checked items as one job each
        through /batch. Called from the pill hook, the Add URL dialog's
        playlist checkbox, the toolbar and the File menu."""
        try:
            if self.isMinimized():
                self.showNormal()
            self.raise_()
            self.activateWindow()
            d = PlaylistPickerDialog(self, api=self.api, prefill_url=prefill_url,
                                     referer=referer, cookie=cookie,
                                     user_agent=user_agent)
            _fade_dialog(d, self.animations_enabled)
            if d.exec() != QDialog.Accepted:
                return
            items = d.values()
            if not items:
                return
            res = self.api.batch(items, **d.request_headers())
            n = len((res or {}).get("job_ids") or [])
            self.statusBar().showMessage(
                f"Queued {n} item{'s' if n != 1 else ''} from \"{d.playlist_title()}\"", 6000)
        except Exception as e:
            QMessageBox.critical(self, "Add failed", str(e))
        self.refresh()

    def add_batch(self):
        text, ok = QInputDialog.getMultiLineText(self, "Batch add", "One URL per line:")
        if not ok or not text.strip():
            return
        for line in text.splitlines():
            line = line.strip()
            if line:
                try:
                    self.api.add(line)
                except Exception:
                    pass
        self.refresh()

    def copy_token(self):
        QApplication.clipboard().setText(self.api.token)
        QMessageBox.information(self, "Token copied", "Pairing token copied to clipboard.")

    def reload_token(self):
        self.api.reload_token()
        QMessageBox.information(self, "Token reloaded", f"Token is now: {self.api.token[:8]}…")

    def context_menu(self, pos):
        # Resolve the row from the click position itself — the selection
        # model may have been wiped by the 1s refresh by the time the user
        # picks a menu item, so handlers key off self._current_ctx instead.
        idx = self.table.indexAt(pos)
        if not idx.isValid() or idx.row() >= len(self.model.items):
            if self.model.items:           # empty space: just offer Select all
                em = QMenu(self)
                if em.addAction("Select all   (Ctrl+A)") == em.exec(
                        self.table.viewport().mapToGlobal(pos)):
                    self.table.selectAll()
            return
        self._current_ctx = self.model.items[idx.row()]["id"]
        j = self.model.items[idx.row()]
        m = QMenu(self)
        a_open = m.addAction("Open")
        a_open_folder = m.addAction("Open folder")
        a_open_location = m.addAction("Open file location")
        m.addSeparator()
        a_resume = m.addAction("Resume")
        a_stop = m.addAction("Stop")
        a_redl = m.addAction("Redownload")
        m.addSeparator()
        q_menu = m.addMenu("Queue position")
        q_actions = {q_menu.addAction("Move to top"): "top",
                     q_menu.addAction("Move up   (Alt+Up)"): "up",
                     q_menu.addAction("Move down   (Alt+Down)"): "down",
                     q_menu.addAction("Move to bottom"): "bottom"}
        q_menu.setEnabled(j["status"] == "queued")
        m.addSeparator()
        cat_menu = m.addMenu("Move to Category")
        cat_actions = {cat_menu.addAction(c): c for c in CATEGORIES}
        m.addSeparator()
        conv_menu = m.addMenu("Convert to")
        conv_actions = {conv_menu.addAction(f): f for f in ["mp4", "mkv", "mp3", "wav", "m4a", "flac", "opus"]}
        m.addSeparator()
        a_remove = m.addAction("Remove from Queue")
        a_delete_file = m.addAction("Delete Job && File")
        a_toolkit = m.addAction("Media Toolkit\u2026")
        a_copy_file = m.addAction("Copy file")
        a_copy_url = m.addAction("Copy URL")
        a_props = m.addAction("Properties")
        m.addSeparator()
        a_select_all = m.addAction("Select all   (Ctrl+A)")
        a_toolkit.setEnabled(j["status"] == "done")
        a_open.setEnabled(j["status"] == "done")
        a_open_folder.setEnabled(j["status"] == "done")
        a_open_location.setEnabled(j["status"] == "done")
        a_copy_file.setEnabled(j["status"] == "done")
        retrying = bool(_retry_text(j))
        if retrying:
            a_resume.setText("Retry now")
        a_resume.setEnabled(j["status"] in ("paused", "stopped", "error") or retrying)
        a_stop.setEnabled(j["status"] in ("downloading", "queued"))
        a_redl.setEnabled(j["status"] in ("done", "error", "stopped"))
        chosen = m.exec(self.table.viewport().mapToGlobal(pos))
        # Re-look-up the row now: a refresh may have replaced it while the
        # menu was open, so every handler below reads the fresh dict.
        j = next((x for x in self.model.items
                  if x["id"] == self._current_ctx), j)
        if chosen == a_select_all:
            self.table.selectAll()
        elif chosen == a_open:
            self._ctx_open()
        elif chosen == a_open_folder:
            self._ctx_open_folder()
        elif chosen == a_open_location:
            self._ctx_open_location()
        elif chosen == a_resume:
            self._ctx_resume()
        elif chosen == a_stop:
            self._ctx_stop()
        elif chosen == a_redl:
            self._ctx_redownload()
        elif chosen in q_actions:
            self._reorder(self._current_ctx, q_actions[chosen])
        elif chosen in cat_actions:
            try:
                self.api.set_category(self._current_ctx, cat_actions[chosen])
            except Exception as e:
                QMessageBox.warning(self, "Error", str(e))
            self.refresh()
        elif chosen in conv_actions:
            try:
                self.api.convert(self._current_ctx, conv_actions[chosen])
            except Exception as e:
                QMessageBox.warning(self, "Convert", str(e))
        elif chosen == a_remove:
            self._ctx_remove()
        elif chosen == a_delete_file:
            self._ctx_delete_file()
        elif chosen == a_toolkit:
            self.open_toolkit(job_id=self._current_ctx)
        elif chosen == a_copy_file:
            # Right-clicked row is part of a multi-selection -> copy all the
            # selected finished files; otherwise just this one.
            picked = self._selected_rows()
            if not any(x["id"] == self._current_ctx for x in picked):
                picked = [j]
            if not _copy_files_to_clipboard(picked):
                QMessageBox.information(self, "Copy file",
                                        "The file no longer exists on disk.")
        elif chosen == a_copy_url:
            QApplication.clipboard().setText(j.get("url", ""))
        elif chosen == a_props:
            # Re-fetch at click time — the row object captured when the menu
            # opened can be stale if a refresh fired while the menu was up.
            fresh = next((x for x in self.model.items
                          if x["id"] == self._current_ctx), j)
            self._show_props(fresh)

    def _reorder(self, jid, action):
        def _do():
            try:
                self.api.reorder(jid, action)
            except Exception:
                pass
        threading.Thread(target=_do, daemon=True).start()
        QTimer.singleShot(400, self.refresh)

    def _reorder_selected(self, action):
        rows = self._selected_rows()
        if len(rows) != 1 or rows[0].get("status") != "queued":
            return
        self._reorder(rows[0]["id"], action)

    def _ctx_open(self):
        jid = getattr(self, "_current_ctx", None)
        if jid:
            self._open_selected(job_id=jid)

    def _ctx_open_folder(self):
        jid = getattr(self, "_current_ctx", None)
        if jid:
            self._open_selected(folder=True, job_id=jid)

    def _ctx_open_location(self):
        jid = getattr(self, "_current_ctx", None)
        if jid:
            self._open_selected(folder="select", job_id=jid)

    def _ctx_resume(self):
        jid = getattr(self, "_current_ctx", None)
        if not jid:
            return
        try:
            self.api.resume(jid)
        except Exception as e:
            QMessageBox.warning(self, "Error", str(e))
        self.refresh()

    def _ctx_stop(self):
        jid = getattr(self, "_current_ctx", None)
        if not jid:
            return
        try:
            self.api.stop(jid)
        except Exception as e:
            QMessageBox.warning(self, "Error", str(e))
        self.refresh()

    def _ctx_redownload(self):
        jid = getattr(self, "_current_ctx", None)
        if not jid:
            return
        try:
            self.api.redownload(jid)
        except Exception as e:
            QMessageBox.warning(self, "Error", str(e))
        self.refresh()

    def _ctx_remove(self):
        jid = getattr(self, "_current_ctx", None)
        if not jid:
            return
        if QMessageBox.question(self, "Remove", "Remove this job?") != QMessageBox.Yes:
            return
        try:
            self.api.delete(jid)
        except Exception as e:
            QMessageBox.warning(self, "Error", str(e))
        self.refresh()

    def _ctx_delete_file(self):
        """Delete the job AND its file. The backend only deletes inside the
        VideoGrabber / download folders and refuses anything else."""
        jid = getattr(self, "_current_ctx", None)
        if not jid:
            return
        j = next((x for x in self.model.items if x["id"] == jid), {})
        if QMessageBox.warning(
                self, "Delete job & file",
                f"Delete this job AND its file from disk?\n\n{j.get('filename', '')}\n\n"
                "This cannot be undone.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        try:
            self.api.delete_file(jid)
        except Exception as e:
            QMessageBox.warning(self, "Delete job & file", str(e))
        self.refresh()

    def open_toolkit(self, job_id=None, path=None):
        """Media Toolkit (visual Trim / Compress / GIF editor). Opens on an
        explicit file path (drag and drop), a finished job, or the selected
        finished job; with none of those it opens empty (Open file / drop)."""
        from gui_toolkit import ToolkitDialog
        if path is None:
            job = None
            if job_id is not None:
                job = next((x for x in self.model.items if x["id"] == job_id), None)
            else:
                done = [x for x in self._selected_rows() if x.get("status") == "done"]
                job = done[0] if done else None
            if job is not None:
                try:
                    p = _path_for(job)
                    path = str(p) if p and Path(p).is_file() else None
                except Exception:
                    path = None
        ToolkitDialog(str(path) if path else None, self).exec()

    def toggle_shell(self):
        """Flip the handheld shell on/off from anywhere (Ctrl+Shift+S): off
        gives a plain, modern window with more room for rows."""
        try:
            on = not bool(load_settings().get("shell_enabled"))
            self.api.save_setting("shell_enabled", on)
            _settings_cache["mtime"] = 0      # force load_settings to re-read
            self.apply_shell()
            self.statusBar().showMessage(
                "Handheld shell on" if on else "Handheld shell off", 3000)
        except Exception as e:
            QMessageBox.warning(self, "Shell", str(e))

    def _open_selected(self, folder=False, job_id=None):
        """Open a job's file (or its containing folder) with proper error
        reporting so failures are visible instead of silent. When job_id is
        given, only that job is opened; otherwise every selected row is."""
        if job_id is not None:
            items = [j for j in self.model.items if j["id"] == job_id]
        else:
            items = self._selected_rows()
        for job in items:
            path = _path_for(job)
            if not path.exists():
                # Legacy whole-playlist job: its own file path never existed
                # (the files live in <category>/<PlaylistTitle>/). Open the
                # playlist folder instead of reporting "File Not Found".
                pl_dir = _playlist_dir_for(job)
                if pl_dir is not None:
                    try:
                        if sys.platform == "win32":
                            os.startfile(str(pl_dir))
                        elif sys.platform == "darwin":
                            subprocess.Popen(["open", str(pl_dir)])
                        else:
                            subprocess.Popen(["xdg-open", str(pl_dir)])
                    except Exception as e:
                        QMessageBox.critical(
                            self, "Error Opening Target",
                            f"Couldn't open, error: {e}\n\nPath:\n{pl_dir}")
                    continue
            # folder: False = open the file; True = open its folder;
            # "select" = open the folder with the file highlighted.
            target = path if folder in (False, "select") else path.parent
            if not target.exists():
                QMessageBox.warning(
                    self, "File Not Found",
                    f"File not found, expected at:\n{target}"
                )
                continue
            try:
                target_str = str(target.resolve())
                if sys.platform == "win32":
                    if folder == "select":
                        _win_select_in_explorer(path.resolve())
                    elif folder:
                        os.startfile(str(path.parent))
                    else:
                        os.startfile(target_str)
                elif sys.platform == "darwin":
                    if folder == "select":
                        subprocess.Popen(["open", "-R", target_str])
                    elif folder:
                        subprocess.Popen(["open", str(path.parent)])
                    else:
                        subprocess.Popen(["open", target_str])
                else:
                    subprocess.Popen(["xdg-open", str(path.parent)])
            except Exception as e:
                QMessageBox.critical(
                    self, "Error Opening Target",
                    f"Couldn't open, error: {e}\n\nPath:\n{target}"
                )

    def _on_double_click(self, index):
        row = index.row()
        if 0 <= row < len(self.model.items):
            job = self.model.items[row]
            if job.get("status") == "done":
                self._open_selected(folder=False, job_id=job["id"])

    def _show_props(self, j):
        path = _path_for(j)
        text = "\n".join([
            f"Name:     {j['filename']}",
            f"URL:      {j['url']}",
            f"Type:     {j['type']}",
            f"Status:   {j['status']}",
            f"Category: {j['category']}",
            f"Size:     {fmt_bytes(j.get('size_total'))}",
            f"Done:     {fmt_bytes(j.get('size_done'))}",
            f"Path:     {path}",
            f"Exists:   {path.exists()}",
            f"Completed:{fmt_ts(j.get('completed_ts'))}",
        ])
        QMessageBox.information(self, "Properties", text)


_app = None
_window = None


def launch_gui(new_job_hook=None, home_dir=None, show_dialog_hook=None):
    """Run the Qt GUI in the current (main) thread. `new_job_hook` is an
    optional callable the server calls whenever a job is queued — it must
    be safe to invoke from any thread. `home_dir` should be the same Path
    server.py resolved for HOME (portable_home()) so this module reads and
    writes the exact settings.json the backend is using."""
    global _app, _window
    # settings.HOME is the single source of truth (server.py sets it through
    # settings.set_paths before calling us). Never diverge from the backend.
    backend_home = Path(_settings_mod.HOME)
    if home_dir is not None and Path(home_dir) != backend_home:
        log(f"launch_gui: home_dir {home_dir} differs from settings.HOME "
            f"{backend_home}; using settings.HOME")
    gui_common.HOME = backend_home
    gui_common.CONFIG_PATH = backend_home / "settings.json"
    _app = QApplication.instance() or QApplication(sys.argv)
    from gui_style import build_qss
    _sync_palette(load_settings())
    _app.setStyleSheet(build_qss(load_settings()))
    _app.setApplicationName("Video Grabber")
    _apply_app_icon(_app)
    _app.setFont(QFont("Segoe UI", 10))
    _app.setQuitOnLastWindowClosed(False)
    # Batch 4 #4: dark native title bars on the main window and every dialog.
    from gui_shell import DarkTitleBarFilter
    _app._vg_dark_filter = DarkTitleBarFilter(_title_bar_dark, _app, _title_bar_colors)
    _app.installEventFilter(_app._vg_dark_filter)
    _window = MainWindow()
    _apply_app_icon(_app, _window)
    _window.show()
    if new_job_hook is not None:
        new_job_hook(_window.new_job_hook())
    if show_dialog_hook is not None:
        show_dialog_hook(_window.show_dialog_hook())
    return _app.exec()
