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
    global _app, _window, HOME, CONFIG_PATH
    if home_dir is not None:
        HOME = Path(home_dir)
        CONFIG_PATH = HOME / "settings.json"
    _app = QApplication.instance() or QApplication(sys.argv)
    from gui_style import build_qss
    _sync_palette(load_settings())
    _app.setStyleSheet(build_qss(load_settings()))
    _app.setApplicationName("Video Grabber")
    _app.setFont(QFont("Segoe UI", 10))
    _app.setQuitOnLastWindowClosed(False)
    # Batch 4 #4: dark native title bars on the main window and every dialog.
    from gui_shell import DarkTitleBarFilter
    _app._vg_dark_filter = DarkTitleBarFilter(_title_bar_dark, _app)
    _app.installEventFilter(_app._vg_dark_filter)
    _window = MainWindow()
    _window.show()
    if new_job_hook is not None:
        new_job_hook(_window.new_job_hook())
    if show_dialog_hook is not None:
        show_dialog_hook(_window.show_dialog_hook())
    return _app.exec()