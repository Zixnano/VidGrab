"""QSS generation from the palette. Applied app-wide (launch_gui and after
every Settings save), so the main window, dialogs and settings all follow
the same tokens. Nothing in here should hardcode a color."""
from palette import resolve_palette, mix, contrast, luminance


def _shade(hexc, f):
    """Lighten (f > 0) or darken (f < 0) a #rrggbb color by that fraction.
    Anything that is not plain #rrggbb is returned unchanged."""
    h = str(hexc).lstrip("#")
    if len(h) != 6:
        return hexc
    try:
        r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return hexc
    t, a = (255, f) if f > 0 else (0, -f)
    r, g, b = (round(c + (t - c) * a) for c in (r, g, b))
    return f"#{r:02x}{g:02x}{b:02x}"


def build_qss(settings_state=None):
    p = resolve_palette(settings_state)
    # v5.5 layout layer: padding is scaled by the preset's density token and
    # corner radii come from the radius tokens. At the default preset every
    # value is identical to the old hardcoded one.
    _ps = float(p.get("pad_scale", 1.0) or 1.0)
    _rs = p.get("radius_sm", "6px")
    _rc = p.get("radius_chip", "10px")

    def pad(*v):
        return " ".join((f"{round(n * _ps)}px" if round(n * _ps) > 0 else "0") for n in v)

    # v5.5 text size (General): every font size below is scaled; the base
    # font is only set when the size isn't 100%, so 100% looks as before.
    try:
        _ts = max(0.7, min(2.0, float((settings_state or {}).get("text_scale", 100) or 100) / 100.0))
    except (TypeError, ValueError):
        _ts = 1.0

    def fs(px):
        return max(8, round(px * _ts))

    # Disabled text: the faint token pushed toward the background, so it still
    # reads as disabled next to the lifted (AA) secondary text.
    _dis = mix(p["text_dim"], p["bg_base"], 0.25)

    # Only set a base size when scaled (Qt's default is 9pt).
    _base_font = "" if abs(_ts - 1.0) < 0.01 else f"\n    font-size: {9 * _ts:.1f}pt;"

    qss = f"""
QMainWindow, QWidget {{
    background: {p['bg_base']};
    color: {p['text']};
    font-family: {p['font']};{_base_font}
}}
QLabel {{ background: transparent; }}
QWidget#Plain {{ background: transparent; }}
QDialog {{ background: {p['bg_panel']}; }}
QScrollArea {{ background: transparent; border: 0; }}
QToolTip {{
    background: {p['bg_elevated']}; color: {p['text']};
    border: 1px solid {p['border_hi']}; padding: 6px 8px;
}}

/* ---- menus ---- */
QMenuBar {{ background: {p['bg_panel']}; color: {p['text_muted']}; padding: {pad(3, 8)};
    border-bottom: 1px solid {p['border']}; }}
QMenuBar::item {{ padding: {pad(6, 12)}; background: transparent; }}
QMenuBar::item:selected {{ background: {p['bg_hover']}; color: {p['text']}; border-radius: {_rs}; }}
QMenu {{ background: {p['bg_elevated']}; border: 1px solid {p['border_hi']};
    border-radius: {p['radius']}; padding: {pad(6)}; }}
QMenu::item {{ padding: {pad(8, 26, 8, 12)}; border-radius: {_rs}; }}
QMenu::item:selected {{ background: {p['bg_sel']}; }}
QMenu::separator {{ height: 1px; background: {p['border']}; margin: 6px 8px; }}

/* ---- toolbar ---- */
#Toolbar {{ background: {p['bg_panel']}; border-bottom: 1px solid {p['border']}; }}
#ToolbarTitle {{ font-size: {fs(16)}px; font-weight: 600; }}
#ToolbarButton {{
    background: transparent; border: 1px solid transparent;
    border-radius: {p['radius']}; padding: {pad(8, 11)}; color: {p['text_soft']};
}}
#ToolbarButton:hover {{ background: {p['bg_hover']}; border-color: {p['border_hi']}; color: {p['text']}; }}
#ToolbarButton:pressed {{ background: {p['bg_sel']}; }}
#Search {{
    background: {p['bg_input']}; border: 1px solid {p['border']};
    border-top: 2px solid {_shade(p['bg_input'], -0.4)};
    border-radius: {p['radius']}; padding: {pad(7, 12, 8, 12)}; min-width: 200px;
}}
#Search:focus {{ border-color: {p['accent']}; }}

/* ---- sidebar ---- */
#Sidebar {{ background: {p['bg_side']}; border-right: 1px solid {p['border']}; }}
#Brand {{ font-size: {fs(12)}px; font-weight: 700; letter-spacing: 1px; color: {p['text']}; }}
#SectionLabel {{ color: {p['text_dim']}; font-size: {fs(10)}px; font-weight: 700; letter-spacing: 1.4px; }}
#SidebarSeparator {{ color: {p['border']}; background: {p['border']}; max-height: 1px; }}
QToolButton[nav="true"] {{
    text-align: left; background: transparent; border: 0;
    border-radius: {p['radius']}; color: {p['text_soft']}; padding: {pad(0, 10)};
}}
QToolButton[nav="true"]:hover {{ background: {p['bg_hover']}; color: {p['text']}; }}
QToolButton[nav="true"][active="true"] {{ background: {p['bg_sel']}; color: {p['text']}; font-weight: 600; }}

/* ---- download table ---- */
QTableView {{
    background: {p['bg_panel']};
    alternate-background-color: {p['bg_base']};
    border: 1px solid {p['border']};
    border-radius: {p['radius']};
    gridline-color: {p['border']};
    selection-background-color: {p['bg_sel']};
    selection-color: {p['text']};
    outline: 0;
}}
QTableView::item {{ border: 0; padding: {pad(7, 8)}; }}
QTableView::item:hover {{ background: {p['bg_hover']}; }}
QTableView::item:selected {{ background: {p['bg_sel']}; }}
QHeaderView::section {{
    background: {p['bg_elevated']}; color: {p['text_muted']}; border: 0;
    border-bottom: 1px solid {p['border']}; padding: {pad(11, 10)};
    font-size: {fs(11)}px; font-weight: 600;
}}
QHeaderView::section:hover {{ color: {p['text']}; }}
#Details {{
    background: {p['bg_input']}; border: 1px solid {p['border']}; border-radius: {p['radius']};
    border-top: 2px solid {_shade(p['bg_input'], -0.4)};
    padding: {pad(7, 12, 8, 12)}; color: {p['text_soft']}; font-size: {fs(12)}px;
}}
#EmptyState {{ color: {p['text_dim']}; font-size: {fs(14)}px; background: transparent; }}
#LogLine {{
    background: {p['bg_input']}; border: 1px solid {p['border']}; border-radius: {p['radius']};
    border-top: 2px solid {_shade(p['bg_input'], -0.4)};
    padding: {pad(7, 12, 8, 12)}; color: {p['text_muted']}; font-family: {p['font_mono']}; font-size: {fs(11)}px;
}}

/* ---- status bar ---- */
QStatusBar, #BottomStatus {{ background: {p['bg_panel']}; border-top: 1px solid {p['border']}; color: {p['text_muted']}; }}
#StatusLabel {{ color: {p['success']}; padding-left: 10px; }}
#HoldLabel {{ color: {p['warning']}; padding-right: 12px; }}
#UpdateBadge {{ color: {p['accent']}; border: 0; padding: 0 12px; font-weight: 600; background: transparent; }}
#UpdateBadge:hover {{ color: {p['text']}; }}

/* ---- buttons and inputs ---- */
QPushButton {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {_shade(p['bg_elevated'], 0.07)}, stop:1 {p['bg_elevated']});
    border: 1px solid {p['border']}; border-bottom: 2px solid {_shade(p['border'], -0.35)};
    border-radius: {p['radius']}; padding: {pad(8, 14, 7, 14)};
}}
QPushButton:hover {{ background: {p['bg_hover']}; border-color: {p['border_hi']};
    border-bottom-color: {_shade(p['border_hi'], -0.35)}; }}
QPushButton:pressed {{ background: {p['bg_sel']}; border-bottom: 1px solid {p['border']};
    padding: {pad(9, 14, 7, 14)}; }}
QPushButton:disabled {{ color: {p['text_dim']}; background: {p['bg_base']}; border-color: {p['border']}; }}
QPushButton#primary {{ background: {p['accent']}; color: {p['text_on_accent']}; border: none; border-bottom: 2px solid {_shade(p['accent'], -0.3)};
    font-weight: 600; }}
QPushButton#primary:hover {{ background: {p['accent_dim']}; }}
QToolButton {{ background: transparent; border: 1px solid transparent; border-radius: {p['radius']}; padding: 4px; }}
QToolButton:hover {{ background: {p['bg_hover']}; border-color: {p['border']}; }}
QLineEdit, QComboBox, QSpinBox, QTextEdit, QPlainTextEdit {{
    background: {p['bg_input']}; border: 1px solid {p['border']};
    border-top: 2px solid {_shade(p['bg_input'], -0.4)};
    border-radius: {p['radius']}; padding: {pad(5, 10, 6, 10)};
    selection-background-color: {p['accent_dim']};
}}
QLineEdit:hover, QComboBox:hover, QSpinBox:hover {{ border-color: {p['border_hi']}; }}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QTextEdit:focus, QPlainTextEdit:focus {{ border-color: {p['accent']}; }}
QComboBox QAbstractItemView {{
    background: {p['bg_elevated']}; border: 1px solid {p['border']};
    selection-background-color: {p['bg_sel']};
}}
QCheckBox, QRadioButton {{ spacing: 8px; background: transparent; }}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 16px; height: 16px; background: {p['bg_input']};
    border: 1px solid {p['border_hi']}; border-radius: 4px;
}}
QRadioButton::indicator {{ border-radius: 9px; }}
QCheckBox::indicator:hover, QRadioButton::indicator:hover {{ border-color: {p['accent']}; }}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
    background: {p['accent']}; border-color: {p['accent']};
}}
QCheckBox::indicator:disabled, QRadioButton::indicator:disabled {{ border-color: {p['border']}; }}
QProgressBar {{
    background: {p['bg_elevated']}; border: 1px solid {p['border']};
    border-radius: {p['radius_pill']}; text-align: center; color: {p['text_muted']};
}}
QProgressBar::chunk {{ background: {p['accent']}; border-radius: {p['radius_pill']}; }}

/* ---- tabs and lists ---- */
QTabWidget::pane {{ border: 1px solid {p['border']}; border-radius: {p['radius']}; top: -1px; }}
QTabBar::tab {{
    background: {p['bg_panel']}; border: 1px solid {p['border']}; border-bottom: none;
    border-top-left-radius: {p['radius']}; border-top-right-radius: {p['radius']};
    padding: {pad(8, 16)}; color: {p['text_muted']};
}}
QTabBar::tab:selected {{ background: {p['bg_elevated']}; color: {p['text']}; border-bottom: 2px solid {p['accent']}; }}
QTabBar::tab:hover:!selected {{ background: {p['bg_elevated']}; }}
QListWidget {{ background: {p['bg_panel']}; border: 1px solid {p['border']}; border-radius: {p['radius']}; outline: 0; }}
QListWidget::item {{ padding: {pad(8, 12)}; border-radius: {_rs}; }}
QListWidget::item:hover {{ background: {p['bg_hover']}; }}
QListWidget::item:selected {{ background: {p['bg_sel']}; color: {p['text']}; }}
QListWidget#SettingsNav {{ background: {p['bg_side']}; padding: 4px; }}
QListWidget#SettingsNav::item {{ padding: {pad(9, 12)}; color: {p['text_soft']}; }}
QListWidget#SettingsNav::item:selected {{ background: {p['bg_sel']}; color: {p['text']}; }}

/* ---- focus, hover and disabled (v5.5) ---- */
QPushButton:focus {{ border: 1px solid {p['accent']}; border-bottom: 2px solid {p['accent']}; }}
QPushButton#primary:focus {{ border: 2px solid {p['text']}; }}
QPushButton#primary:disabled {{ background: {p['bg_elevated']}; color: {_dis}; border: 1px solid {p['border']}; }}
QToolButton:focus, #ToolbarButton:focus, #SidebarTool:focus {{ border: 1px solid {p['accent']}; }}
QToolButton[nav="true"]:focus {{ border: 1px solid {p['accent']}; }}
QCheckBox:focus, QRadioButton:focus {{ color: {p['text']}; }}
QCheckBox::indicator:focus, QRadioButton::indicator:focus {{ border: 2px solid {p['accent']}; }}
QTabBar::tab:focus {{ border-color: {p['accent']}; }}
QListWidget:focus, QTableView:focus {{ border: 1px solid {p['accent']}; }}
QToolButton:disabled, #ToolbarButton:disabled, #SidebarTool:disabled {{ color: {_dis}; background: transparent; border-color: transparent; }}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QTextEdit:disabled, QPlainTextEdit:disabled {{
    color: {_dis}; background: {p['bg_base']}; border-color: {p['border']}; }}
QCheckBox:disabled, QRadioButton:disabled, QLabel:disabled {{ color: {_dis}; }}
QMenu::item:disabled {{ color: {_dis}; background: transparent; }}

/* ---- drop cue (a link or file dragged over the table) and splitter handle ---- */
QTableView[dropActive="true"] {{ border: 2px solid {p['accent']}; background: {p['bg_sel']}; }}
QSplitter::handle:vertical {{ background: transparent; }}
QSplitter::handle:vertical:hover {{ background: {p['border_hi']}; border-radius: 2px; margin: 2px 40%; }}

/* ---- scrollbars ---- */
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {p['border']}; border-radius: 5px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {p['border_hi']}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {p['border']}; border-radius: 5px; min-width: 30px; }}
QScrollBar::handle:horizontal:hover {{ background: {p['border_hi']}; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}

/* ---- theme page cards ---- */
#Card {{ background: {p['bg_panel']}; border: 1px solid {p['border']}; border-radius: {p['radius']}; }}
#CardTitle {{ font-size: {fs(13)}px; font-weight: 700; }}
#CardHint {{ color: {p['text_muted']}; font-size: {fs(11)}px; }}
#PageTitle {{ font-size: {fs(18)}px; font-weight: 700; }}
#Chip {{ background: {p['bg_elevated']}; border: 1px solid {p['border']}; border-radius: {_rc};
    padding: {pad(2, 10)}; color: {p['text_muted']}; font-size: {fs(11)}px; }}
#Warn {{ color: {p['warning']}; font-size: {fs(11)}px; }}

/* ---- sidebar tools row ---- */
#SidebarTool {{
    background: transparent; border: 0; border-radius: {p['radius']};
    color: {p['text_soft']}; padding: {pad(6, 4)}; font-size: {fs(11)}px;
}}
#SidebarTool:hover {{ background: {p['bg_hover']}; color: {p['text']}; }}

/* ---- download detail pane ---- */
#DetailPane {{ background: {p['bg_panel']}; border: 1px solid {p['border']}; border-radius: {p['radius']}; }}
#DetailTitle {{ font-size: {fs(14)}px; font-weight: 700; }}
#DetailSub, #StatCaption, #DetailKey {{ color: {p['text_muted']}; font-size: {fs(11)}px; }}
#StatValue {{ font-size: {fs(13)}px; font-weight: 600; }}
QProgressBar#DetailBar {{ min-height: 12px; max-height: 12px; }}
"""
    if (settings_state or {}).get("frame_cream"):
        qss += _cream_frame_qss(p)
    return qss


def _cream_frame_qss(p):
    """Window frame (toolbar, menu bar, status bar, margins) around the
    theme's dark panels, painted in the palette's "frame" token (cream by
    default). Scoped to the main window's own widgets so dialogs keep the
    normal theme. Highlight, edge and text colors are derived from the token,
    so one color picker drives all of them."""
    fb = p["frame"]
    _ps = float(p.get("pad_scale", 1.0) or 1.0)    # v5.5 density token

    def pad(*v):
        return " ".join((f"{round(n * _ps)}px" if round(n * _ps) > 0 else "0") for n in v)

    fh = mix(fb, "#ffffff", 0.35)          # light top / hover
    fe = mix(fb, "#000000", 0.22)          # edges
    fd = mix(fb, "#000000", 0.34)          # bottom bevel
    ft = p["bg_base"] if contrast(fb, p["bg_base"]) >= 4.5 else (
        "#000000" if luminance(fb) > 0.4 else "#ffffff")
    fm = mix(ft, fb, 0.35)                 # secondary text
    for _t in (0.3, 0.25, 0.2, 0.15, 0.1, 0.05, 0.0):   # v5.5: keep it AA (4.5:1) on the frame
        if contrast(fm, fb) >= 4.5:
            break
        fm = mix(ft, fb, _t)
    return f"""
/* ---- cream frame ---- */
QMainWindow, #MainRoot, #FrameArea, #Content {{ background: {fb}; }}
QMenuBar {{ background: {fh}; color: {ft}; border-bottom: 1px solid {fe}; }}
QMenuBar::item:selected {{ background: {fb}; color: {ft}; }}
#Toolbar {{ background: {fb}; border-bottom: 1px solid {fe}; }}
#ToolbarTitle {{ color: {ft}; }}
#ToolbarButton {{
    color: {ft};
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 {fh}, stop:1 {fb});
    border: 1px solid {fe}; border-bottom: 2px solid {fd};
    border-radius: {p['radius']}; padding: {pad(8, 11, 7, 11)};
}}
#ToolbarButton:hover {{ background: {fh}; border-color: {fd}; color: {ft}; }}
#ToolbarButton:pressed {{ background: {fe}; border-bottom: 1px solid {fd}; }}
#Content #SectionLabel {{ color: {fm}; }}
QStatusBar, #BottomStatus {{ background: {fb}; color: {ft}; border-top: 1px solid {fe}; }}
QStatusBar QLabel {{ color: {ft}; }}
"""
