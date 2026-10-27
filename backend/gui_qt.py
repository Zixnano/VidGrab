"""gui_qt.py: compatibility shim. The GUI now lives in the gui_*.py modules (v11
split); everything that used to be importable from here still is.

Color globals and the _app / _window singletons are rebound at runtime, so they
are served live by __getattr__ instead of being copied here at import time."""
import sys

import gui_palette, gui_common, gui_api, gui_sidebar, gui_table, gui_models
import gui_add_dialog, gui_playlist_picker, gui_settings_dialog, gui_dialogs, gui_main
from gui_api import ApiClient, ProbeError, _ApiFetchWorker
from gui_models import DownloadModel
from gui_table import (DownloadTable, ProgressDelegate, _BottomPane,
                       _copy_files_to_clipboard, _detail_info, _progress_pct)
from gui_add_dialog import AddDownloadDialog
from gui_playlist_picker import PlaylistPickerDialog
from gui_settings_dialog import SettingsDialog, _SettingsNav, BandwidthProfilesDialog
from gui_sidebar import Sidebar, _qicon, _make_tray_icon, _status_icon, _site_icon, _icons_dir
from gui_dialogs import WhatsNewDialog, PairDialog, HelpDialog, UpdateDialog
from gui_palette import _sync_palette
from gui_common import (load_settings, load_token, fmt_ts, fmt_bytes, _eta_seconds,
                        _settings_cache, _path_for)
from gui_main import (MainWindow, launch_gui, reapply_theme, _gui_state_path,
                      _read_gui_state, _write_gui_state)

_SOURCES = ("gui_palette", "gui_common", "gui_main", "gui_table", "gui_models", "gui_api",
            "gui_sidebar", "gui_add_dialog", "gui_playlist_picker", "gui_settings_dialog",
            "gui_dialogs")


def __getattr__(name):
    """Any other name: ACCENT, TEXT, _PAL, _app, _window, HOME ... looked up live."""
    for mod in _SOURCES:
        if name in vars(sys.modules[mod]):
            return vars(sys.modules[mod])[name]
    raise AttributeError(f"module 'gui_qt' has no attribute {name!r}")
