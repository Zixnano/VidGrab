"""Sidebar, SVG icon helpers and tray / status / site icons."""

import time

from PySide6.QtCore import QByteArray, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QBoxLayout, QFrame, QHBoxLayout, QLabel, QSizePolicy, QToolButton, QVBoxLayout

from settings import CATEGORIES

import gui_palette
import gui_common


# --- Pure SVG icon engine: Material-style paths, rendered by QSvgRenderer ---
_SVG_PATHS = {
    "add": '<path d="M19 13h-6v6h-2v-6H5v-2h6V5h2v6h6v2z"/>',
    "batch": '<path d="M4 6h16v2H4zm0 5h16v2H4zm0 5h16v2H4z"/>',
    "playlist": '<path d="M15 6H3v2h12V6zm0 4H3v2h12v-2zM3 16h8v-2H3v2zM17 6v8.18c-.31-.11-.65-.18-1-.18-1.66 0-3 1.34-3 3s1.34 3 3 3 3-1.34 3-3V8h3V6h-5z"/>',
    "play": '<path d="M8 5v14l11-7z"/>',
    "pause": '<path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z"/>',
    "stop": '<path d="M6 6h12v12H6z"/>',
    "delete": '<path d="M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z"/>',
    "stream": '<path d="M3 13h2v-2H3v2zm0 4h2v-2H3v2zm0-8h2V7H3v2zm4 4h14v-2H7v2zm0 4h14v-2H7v2zM7 7v2h14V7H7z"/>',
    "queue_play": '<path d="M3 13h2v-2H3v2zm0 4h2v-2H3v2zm0-8h2V7H3v2zm4 4h14v-2H7v2zm0 4h14v-2H7v2zM7 7v2h14V7H7zm10 7l4-4-4-4v8z"/>',
    "queue_pause": '<path d="M3 13h2v-2H3v2zm0 4h2v-2H3v2zm0-8h2V7H3v2zm4 4h14v-2H7v2zm0 4h14v-2H7v2zM7 7v2h14V7H7zm10 5v4l-4-2 4-2z"/>',
    "settings": '<path d="M19.14 12.94c.04-.3.06-.61.06-.94 0-.32-.02-.64-.07-.94l2.03-1.58c.18-.14.23-.41.12-.61l-1.92-3.32c-.12-.22-.37-.29-.59-.22l-2.39.96c-.5-.38-1.03-.7-1.62-.94l-.36-2.54c-.04-.24-.24-.41-.48-.41h-3.84c-.24 0-.43.17-.47.41l-.36 2.54c-.59.24-1.13.57-1.62.94l-2.39-.96c-.22-.08-.47 0-.59.22L2.74 8.87c-.12.21-.08.47.12.61l2.03 1.58c-.05.3-.09.63-.09.94s.02.64.07.94l-2.03 1.58c-.18.14-.23.41-.12.61l1.92 3.32c.12.22.37.29.59.22l2.39-.96c.5.38 1.03.7 1.62.94l.36 2.54c.05.24.24.41.48.41h3.84c.24 0 .44-.17.47-.41l.36-2.54c.59-.24 1.13-.56 1.62-.94l2.39.96c.22.08.47 0 .59-.22l1.92-3.32c.12-.22.07-.47-.12-.61l-2.01-1.58zM12 15.6c-1.98 0-3.6-1.62-3.6-3.6s1.62-3.6 3.6-3.6 3.6 1.62 3.6 3.6-1.62 3.6-3.6 3.6z"/>',
    "all": '<path d="M4 8h4V4H4v4zm6 12h4v-4h-4v4zm-6 0h4v-4H4v4zm0-6h4v-4H4v4zm6 0h4v-4h-4v4zm6-10v4h4V4h-4zm-6 4h4V4h-4v4zm6 6h4v-4h-4v4zm0 6h4v-4h-4v4z"/>',
    "active": '<path d="M19 9h-4V3H9v6H5l7 7 7-7zM5 18v2h14v-2H5z"/>',
    "finished": '<path d="M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z"/>',
    "unfinished": '<path d="M11.99 2C6.47 2 2 6.48 2 12s4.47 10 9.99 10C17.52 22 22 17.52 22 12S17.52 2 11.99 2zM12 20c-4.42 0-8-3.58-8-8s3.58-8 8-8 8 3.58 8 8-3.58 8-8 8zm.5-13H11v6l5.25 3.15.75-1.23-4.5-2.67z"/>',
    "failed": '<path d="M1 21h22L12 2 1 21zm12-3h-2v-2h2v2zm0-4h-2v-4h2v4z"/>',
    "video": '<path d="M18 4l2 4h-3l-2-4h-2l2 4h-3l-2-4H8l2 4H7L5 4H4c-1.1 0-1.99.9-1.99 2L2 18c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V4h-4z"/>',
    "music": '<path d="M12 3v10.55c-.59-.34-1.27-.55-2-.55-2.21 0-4 1.79-4 4s1.79 4 4 4 4-1.79 4-4V7h4V3h-6z"/>',
    "images": '<path d="M21 19V5c0-1.1-.9-2-2-2H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2zM8.5 13.5l2.5 3.01L14.5 12l4.5 6H5l3.5-4.5z"/>',
    "compressed": '<path d="M20 6h-8l-2-2H4c-1.1 0-1.99.9-1.99 2L2 18c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V8c0-1.1-.9-2-2-2zm-6 10h-2v-2h2v2zm0-4h-2v-2h2v2zm0-4h-2V6h2v2z"/>',
    "documents": '<path d="M14 2H6c-1.1 0-1.99.9-1.99 2L4 20c0 1.1.89 2 1.99 2H18c1.1 0 2-.9 2-2V8l-6-6zm2 16H8v-2h8v2zm0-4H8v-2h8v2zm-3-5V3.5L18.5 9H13z"/>',
    "programs": '<path d="M20 18c1.1 0 1.99-.9 1.99-2L22 6c0-1.1-.9-2-2-2H4c-1.1 0-2 .9-2 2v10c0 1.1.9 2 2 2H0v2h24v-2h-4zM4 6h16v10H4V6z"/>',
    "fonts": '<path d="M9 4v3h5v12h3V7h5V4H9zm-6 8h3v7h3v-7h3V9H3v3z"/>',
    "other": '<path d="M6 10c-1.1 0-2 .9-2 2s.9 2 2 2 2-.9 2-2-.9-2-2-2zm12 0c-1.1 0-2 .9-2 2s.9 2 2 2 2-.9 2-2-.9-2-2-2zm-6 0c-1.1 0-2 .9-2 2s.9 2 2 2 2-.9 2-2-.9-2-2-2z"/>',
}

_ICON_PX = 64  # rendered large, then scaled down by Qt: stays sharp on HiDPI


def _qicon(name, color):
    """Pure-SVG icon for `name` tinted `color` (hex string), or None when the
    name is unknown. Callers show the plain label (or its first letter)."""
    if not name or name not in _SVG_PATHS:
        return None
    key = (name, color)
    if key in gui_palette._SVG_ICON_CACHE:
        return gui_palette._SVG_ICON_CACHE[key]
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
           f'fill="{color}">{_SVG_PATHS[name]}</svg>')
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    pm = QPixmap(_ICON_PX, _ICON_PX)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    renderer.render(p)
    p.end()
    ic = QIcon(pm)
    gui_palette._SVG_ICON_CACHE[key] = ic
    return ic
_SB_ICONS = {
    "All": "all", "Active": "active", "Queue": "stream",
    "Finished": "finished", "Unfinished": "unfinished", "Failed": "failed",
    "Video": "video", "Music": "music", "Images": "images", "Compressed": "compressed",
    "Documents": "documents", "Programs": "programs", "Fonts": "fonts", "Other": "other",
}


def _make_tray_icon(active: bool):
    """Green dot while downloading, grey when idle — like IDM's tray."""
    key = "active" if active else "idle"
    if key in gui_palette._TRAY_ICONS:
        return gui_palette._TRAY_ICONS[key]
    color = QColor(gui_palette.ACCENT if active else gui_palette._PAL["text_dim"])
    pm = QPixmap(32, 32)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(color)
    p.setPen(Qt.NoPen)
    p.drawEllipse(4, 4, 24, 24)
    p.end()
    icon = QIcon(pm)
    gui_palette._TRAY_ICONS[key] = icon
    return icon


def _status_icon(status):
    key = status or "queued"
    if key in gui_palette._ICON_CACHE:
        return gui_palette._ICON_CACHE[key]
    colors = {
        "downloading": gui_palette.SUCCESS, "done": gui_palette.SUCCESS, "paused": gui_palette.WARN,
        "stopped": gui_palette.DANGER, "error": gui_palette.DANGER, "queued": gui_palette.MUTED,
        "skipped": gui_palette._PAL["text_dim"],
    }
    color = QColor(colors.get(key, gui_palette.MUTED))
    pm = QPixmap(14, 14)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(color)
    p.drawEllipse(2, 2, 10, 10)
    p.end()
    gui_palette._ICON_CACHE[key] = pm
    return pm


def _icons_dir():
    try:
        from settings import icons_dir
        return icons_dir()
    except Exception:
        return gui_common.HOME / "icons"


def _site_icon(site):
    """Favicon for a site (letter tile until the extension has sent one)."""
    if not site:
        return None
    now = time.monotonic()
    hit = gui_palette._SITE_ICONS.get(site)
    if hit and (hit[2] or now - hit[1] < 10):
        return hit[0]
    pm = QPixmap(18, 18)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setRenderHint(QPainter.SmoothPixmapTransform)
    fav = QPixmap()
    has_fav = False
    try:
        path = _icons_dir() / (site + ".png")
        has_fav = path.is_file() and fav.load(str(path)) and not fav.isNull()
    except Exception:
        has_fav = False
    if has_fav:
        p.drawPixmap(1, 1, fav.scaled(16, 16, Qt.KeepAspectRatio, Qt.SmoothTransformation))
    else:
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(gui_palette.TRACK))
        p.drawRoundedRect(1, 1, 16, 16, 4, 4)
        f = QFont("Segoe UI", 8)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(gui_palette.ACCENT))
        p.drawText(1, 1, 16, 16, Qt.AlignCenter, site[:1].upper())
    p.end()
    gui_palette._SITE_ICONS[site] = (pm, now, bool(has_fav))
    if len(gui_palette._SITE_ICONS) > 300:
        gui_palette._SITE_ICONS.clear()
    return pm


class Sidebar(QFrame):
    category_selected = Signal(str)
    tool_clicked = Signal(str)   # "settings" | "help" | "about"
    collapsed_changed = Signal(bool)   # v5.5: the user pressed the collapse button

    WIDTH_FULL, WIDTH_COLLAPSED = 200, 56
    TOOL_ICONS = {"settings": "\u2699", "help": "?", "about": "i"}

    def __init__(self):
        super().__init__()
        self.setObjectName("Sidebar")
        self.setFixedWidth(self.WIDTH_FULL)
        self._collapsed = False
        self._counts = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 16, 14, 14)
        layout.setSpacing(4)
        self._layout = layout
        head = QHBoxLayout()
        head.setSpacing(4)
        brand = QLabel("↓  VIDEO GRABBER")
        brand.setObjectName("Brand")
        head.addWidget(brand, 1)
        self._brand = brand
        self.collapse_btn = QToolButton()
        self.collapse_btn.setObjectName("SidebarTool")
        self.collapse_btn.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.collapse_btn.setCursor(Qt.PointingHandCursor)
        self.collapse_btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.collapse_btn.clicked.connect(
            lambda _=False: self.set_collapsed(not self._collapsed, user=True))
        head.addWidget(self.collapse_btn)
        layout.addLayout(head)
        layout.addSpacing(14)
        self.buttons = {}
        for name in ["All", "Active", "Queue", "Finished", "Unfinished", "Failed"]:
            self._add_nav(layout, name)
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setObjectName("SidebarSeparator")
        layout.addWidget(sep)
        label = QLabel("CATEGORIES")
        label.setObjectName("SectionLabel")
        layout.addWidget(label)
        self._cat_label = label
        for name in CATEGORIES:
            self._add_nav(layout, name)
        layout.addStretch()
        tools_label = QLabel("TOOLS")
        tools_label.setObjectName("SectionLabel")
        layout.addWidget(tools_label)
        self._tools_label = tools_label
        tools = QHBoxLayout()
        tools.setSpacing(2)
        self._tools = tools
        self._tool_btns = []     # (button, full text, collapsed glyph)
        # v5.5: Settings is no longer here; it lives on the toolbar cog (and in
        # File > Settings...). Help and About stay.
        for key, text, tip in (("help", "Help", "Tips and shortcuts"),
                               ("about", "About", "About Video Grabber")):
            b = QToolButton()
            b.setObjectName("SidebarTool")
            b.setText(text)
            b.setToolTip(tip)
            b.setToolButtonStyle(Qt.ToolButtonTextOnly)
            b.setCursor(Qt.PointingHandCursor)
            b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            b.clicked.connect(lambda _=False, k=key: self.tool_clicked.emit(k))
            tools.addWidget(b)
            self._tool_btns.append((b, text, self.TOOL_ICONS.get(key, text[:1])))
        layout.addLayout(tools)
        self.set_collapsed(False)

    def _add_nav(self, layout, name):
        btn = QToolButton()
        btn.setText(name)
        btn.setToolButtonStyle(Qt.ToolButtonTextOnly)
        btn.setProperty("nav", True)
        btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        btn.setMinimumHeight(32)
        btn.clicked.connect(lambda _=False, n=name: self.category_selected.emit(n))
        layout.addWidget(btn)
        self.buttons[name] = btn

    def _refresh_btn(self, name):
        """Text, tooltip and style of one nav button for the current mode.
        Called every refresh tick, so it only touches what changed."""
        btn = self.buttons[name]
        n = self._counts.get(name, 0)
        label = f"{name}  ({n})" if n else name
        ic = _qicon(_SB_ICONS.get(name), gui_palette.SOFT) if self._collapsed else None
        if self._collapsed:
            # If icon is missing, use first letter as fallback
            text = "" if ic is not None else name[:1]
            tip = label
            css = "QToolButton { text-align: center; padding: 0; }"
        else:
            text, tip, css = label, "", ""
        mode = f"icon|{gui_palette.SOFT}" if ic is not None else "text"
        if btn.property("_vg_mode") != mode:
            btn.setProperty("_vg_mode", mode)
            if ic is not None:
                btn.setIcon(ic)
                btn.setIconSize(QSize(18, 18))
                btn.setToolButtonStyle(Qt.ToolButtonIconOnly)
            else:
                btn.setIcon(QIcon())
                btn.setToolButtonStyle(Qt.ToolButtonTextOnly)
        if btn.text() != text:
            btn.setText(text)
        if btn.toolTip() != tip:
            btn.setToolTip(tip)
        if btn.styleSheet() != css:
            btn.setStyleSheet(css)

    def set_collapsed(self, flag, user=False):
        """Icons-only (True) or full width (False). `user` marks a press of
        the collapse button, which is what gets saved."""
        flag = bool(flag)
        changed = flag != self._collapsed
        self._collapsed = flag
        self.setFixedWidth(self.WIDTH_COLLAPSED if flag else self.WIDTH_FULL)
        side = 6 if flag else 14
        self._layout.setContentsMargins(side, 16, side, 14)
        self._brand.setVisible(not flag)
        self._cat_label.setVisible(not flag)
        self._tools_label.setVisible(not flag)
        self.collapse_btn.setText("\u00bb" if flag else "\u00ab")
        self.collapse_btn.setToolTip("Expand the sidebar" if flag else "Collapse the sidebar to icons")
        self._tools.setDirection(QBoxLayout.TopToBottom if flag else QBoxLayout.LeftToRight)
        for b, text, icon in self._tool_btns:
            b.setText(icon if flag else text)
        for name in self.buttons:
            self._refresh_btn(name)
        if changed and user:
            self.collapsed_changed.emit(flag)

    def set_counts(self, counts):
        """Show a count next to each entry that has any rows."""
        self._counts = counts
        for name in self.buttons:
            self._refresh_btn(name)
