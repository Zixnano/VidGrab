"""QSS generation from the palette. Applied app-wide (launch_gui and after
every Settings save), so the main window, dialogs and settings all follow
the same tokens. Nothing in here should hardcode a color."""
from palette import resolve_palette


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
    qss = f"""
QMainWindow, QWidget {{
    background: {p['bg_base']};
    color: {p['text']};
    font-family: {p['font']};
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
QMenuBar {{ background: {p['bg_panel']}; color: {p['text_muted']}; padding: 3px 8px;
    border-bottom: 1px solid {p['border']}; }}
QMenuBar::item {{ padding: 6px 12px; background: transparent; }}
QMenuBar::item:selected {{ background: {p['bg_hover']}; color: {p['text']}; border-radius: 6px; }}
QMenu {{ background: {p['bg_elevated']}; border: 1px solid {p['border_hi']};
    border-radius: {p['radius']}; padding: 6px; }}
QMenu::item {{ padding: 8px 26px 8px 12px; border-radius: 6px; }}
QMenu::item:selected {{ background: {p['bg_sel']}; }}
QMenu::separator {{ height: 1px; background: {p['border']}; margin: 6px 8px; }}

/* ---- toolbar ---- */
#Toolbar {{ background: {p['bg_panel']}; border-bottom: 1px solid {p['border']}; }}
#ToolbarTitle {{ font-size: 16px; font-weight: 600; }}
#ToolbarButton {{
    background: transparent; border: 1px solid transparent;
    border-radius: {p['radius']}; padding: 8px 11px; color: {p['text_soft']};
}}
#ToolbarButton:hover {{ background: {p['bg_hover']}; border-color: {p['border_hi']}; color: {p['text']}; }}
#ToolbarButton:pressed {{ background: {p['bg_sel']}; }}
#Search {{
    background: {p['bg_input']}; border: 1px solid {p['border']};
    border-top: 2px solid {_shade(p['bg_input'], -0.4)};
    border-radius: {p['radius']}; padding: 7px 12px 8px 12px; min-width: 200px;
}}
#Search:focus {{ border-color: {p['accent']}; }}

/* ---- sidebar ---- */
#Sidebar {{ background: {p['bg_side']}; border-right: 1px solid {p['border']}; }}
#Brand {{ font-size: 12px; font-weight: 700; letter-spacing: 1px; color: {p['text']}; }}
#SectionLabel {{ color: {p['text_dim']}; font-size: 10px; font-weight: 700; letter-spacing: 1.4px; }}
#SidebarSeparator {{ color: {p['border']}; background: {p['border']}; max-height: 1px; }}
QToolButton[nav="true"] {{
    text-align: left; background: transparent; border: 0;
    border-radius: {p['radius']}; color: {p['text_soft']}; padding: 0 10px;
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
QTableView::item {{ border: 0; padding: 7px 8px; }}
QTableView::item:hover {{ background: {p['bg_hover']}; }}
QTableView::item:selected {{ background: {p['bg_sel']}; }}
QHeaderView::section {{
    background: {p['bg_elevated']}; color: {p['text_muted']}; border: 0;
    border-bottom: 1px solid {p['border']}; padding: 11px 10px;
    font-size: 11px; font-weight: 600;
}}
QHeaderView::section:hover {{ color: {p['text']}; }}
#Details {{
    background: {p['bg_input']}; border: 1px solid {p['border']}; border-radius: {p['radius']};
    border-top: 2px solid {_shade(p['bg_input'], -0.4)};
    padding: 7px 12px 8px 12px; color: {p['text_soft']}; font-size: 12px;
}}
#EmptyState {{ color: {p['text_dim']}; font-size: 14px; background: transparent; }}
#LogLine {{
    background: {p['bg_input']}; border: 1px solid {p['border']}; border-radius: {p['radius']};
    border-top: 2px solid {_shade(p['bg_input'], -0.4)};
    padding: 7px 12px 8px 12px; color: {p['text_muted']}; font-family: {p['font_mono']}; font-size: 11px;
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
    border-radius: {p['radius']}; padding: 8px 14px 7px 14px;
}}
QPushButton:hover {{ background: {p['bg_hover']}; border-color: {p['border_hi']};
    border-bottom-color: {_shade(p['border_hi'], -0.35)}; }}
QPushButton:pressed {{ background: {p['bg_sel']}; border-bottom: 1px solid {p['border']};
    padding: 9px 14px 7px 14px; }}
QPushButton:disabled {{ color: {p['text_dim']}; background: {p['bg_base']}; border-color: {p['border']}; }}
QPushButton#primary {{ background: {p['accent']}; color: {p['text_on_accent']}; border: none; border-bottom: 2px solid {_shade(p['accent'], -0.3)};
    font-weight: 600; }}
QPushButton#primary:hover {{ background: {p['accent_dim']}; }}
QToolButton {{ background: transparent; border: 1px solid transparent; border-radius: {p['radius']}; padding: 4px; }}
QToolButton:hover {{ background: {p['bg_hover']}; border-color: {p['border']}; }}
QLineEdit, QComboBox, QSpinBox, QTextEdit, QPlainTextEdit {{
    background: {p['bg_input']}; border: 1px solid {p['border']};
    border-top: 2px solid {_shade(p['bg_input'], -0.4)};
    border-radius: {p['radius']}; padding: 5px 10px 6px 10px;
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
    padding: 8px 16px; color: {p['text_muted']};
}}
QTabBar::tab:selected {{ background: {p['bg_elevated']}; color: {p['text']}; border-bottom: 2px solid {p['accent']}; }}
QTabBar::tab:hover:!selected {{ background: {p['bg_elevated']}; }}
QListWidget {{ background: {p['bg_panel']}; border: 1px solid {p['border']}; border-radius: {p['radius']}; outline: 0; }}
QListWidget::item {{ padding: 8px 12px; border-radius: 6px; }}
QListWidget::item:hover {{ background: {p['bg_hover']}; }}
QListWidget::item:selected {{ background: {p['bg_sel']}; color: {p['text']}; }}
QListWidget#SettingsNav {{ background: {p['bg_side']}; padding: 4px; }}
QListWidget#SettingsNav::item {{ padding: 9px 12px; color: {p['text_soft']}; }}
QListWidget#SettingsNav::item:selected {{ background: {p['bg_sel']}; color: {p['text']}; }}

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
#CardTitle {{ font-size: 13px; font-weight: 700; }}
#CardHint {{ color: {p['text_muted']}; font-size: 11px; }}
#PageTitle {{ font-size: 18px; font-weight: 700; }}
#Chip {{ background: {p['bg_elevated']}; border: 1px solid {p['border']}; border-radius: 10px;
    padding: 2px 10px; color: {p['text_muted']}; font-size: 11px; }}
#Warn {{ color: {p['warning']}; font-size: 11px; }}

/* ---- sidebar tools row ---- */
#SidebarTool {{
    background: transparent; border: 0; border-radius: {p['radius']};
    color: {p['text_soft']}; padding: 6px 4px; font-size: 11px;
}}
#SidebarTool:hover {{ background: {p['bg_hover']}; color: {p['text']}; }}

/* ---- download detail pane ---- */
#DetailPane {{ background: {p['bg_panel']}; border: 1px solid {p['border']}; border-radius: {p['radius']}; }}
#DetailTitle {{ font-size: 14px; font-weight: 700; }}
#DetailSub, #StatCaption, #DetailKey {{ color: {p['text_muted']}; font-size: 11px; }}
#StatValue {{ font-size: 13px; font-weight: 600; }}
QProgressBar#DetailBar {{ min-height: 12px; max-height: 12px; }}
"""
    if (settings_state or {}).get("frame_cream"):
        qss += _cream_frame_qss(p)
    return qss


def _cream_frame_qss(p):
    """Cream window frame (toolbar, menu bar, status bar, margins) around the
    theme's dark panels. Scoped to the main window's own widgets so dialogs
    keep the normal theme."""
    fb, fh, fe, fd = "#d9d3c0", "#e8e4d4", "#a8a28c", "#8f8a78"
    ft, fm = "#1b1f1d", "#4a4d4b"
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
    border-radius: {p['radius']}; padding: 8px 11px 7px 11px;
}}
#ToolbarButton:hover {{ background: {fh}; border-color: {fd}; color: {ft}; }}
#ToolbarButton:pressed {{ background: {fe}; border-bottom: 1px solid {fd}; }}
#Content #SectionLabel {{ color: {fm}; }}
QStatusBar, #BottomStatus {{ background: {fb}; color: {ft}; border-top: 1px solid {fe}; }}
QStatusBar QLabel {{ color: {ft}; }}
"""
