"""QSS generation from the palette. Applied app-wide (launch_gui and after
every Settings save), so the main window, dialogs and settings all follow
the same tokens. Nothing in here should hardcode a color."""
from palette import resolve_palette, mix, contrast, luminance, scale_px, zoom_factor


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


def build_qss(settings_state=None, override_tokens=None):
    p = resolve_palette(settings_state)
    if override_tokens:
        p.update(override_tokens)
    # v5.5 layout layer: padding is scaled by the preset's density token and
    # corner radii come from the radius tokens. At the default preset every
    # value is identical to the old hardcoded one.
    # Batch 2 true global zoom: General > Text size is a zoom factor for the
    # fonts AND for paddings, margins, radii and control sizes, applied on top
    # of the preset's own density (pad_scale).
    _zoom = zoom_factor(settings_state)
    _ps = float(p.get("pad_scale", 1.0) or 1.0) * _zoom
    _rs = scale_px(p.get("radius_sm", "6px"), _zoom)
    _rc = scale_px(p.get("radius_chip", "10px"), _zoom)
    _rad = scale_px(p.get("radius", "10px"), _zoom)

    def _z(n):
        return max(1, round(n * _zoom))

    def pad(*v):
        return " ".join((f"{round(n * _ps)}px" if round(n * _ps) > 0 else "0") for n in v)

    # v5.5 text size (General): every font size below is scaled; the base
    # font is only set when the size isn't 100%, so 100% looks as before.
    # Fonts follow the zoom and the preset's font_scale (Compact is small).
    try:
        _fm = max(0.5, min(2.0, float(p.get("font_scale", 1.0) or 1.0)))
    except (TypeError, ValueError):
        _fm = 1.0
    _ts = _zoom * _fm

    def fs(px):
        return max(8, round(px * _ts))

    # Disabled text: the faint token pushed toward the background, so it still
    # reads as disabled next to the lifted (AA) secondary text.
    _dis = mix(p["text_dim"], p["bg_base"], 0.25)

    # Only set a base size when scaled (Qt's default is 9pt).
    _base_font = "" if abs(_ts - 1.0) < 0.01 else f"\n    font-size: {10 * _ts:.1f}pt;"

    # Batch 2: "App Font" (General) overrides the preset's font stack.
    _app_font = str((settings_state or {}).get("app_font") or "").replace('"', "").strip()
    _family = f'"{_app_font}", {p["font"]}' if _app_font else p["font"]
    _wt = str(p.get("font_weight") or "normal").strip().lower()
    _weight = "" if _wt in ("", "normal", "400") else f"\n    font-weight: {_wt};"
    # Preset border thickness (Classic LCD and High Contrast are thick).
    # Batch 3: the floor is 0, not 1 — Glass sets border_w=0 and means it
    # (the old `or 1` also turned an explicit 0 back into 1).
    try:
        _bwv = p.get("border_w", 1)
        _bwn = max(0, min(6, int(round(float(1 if _bwv is None else _bwv)))))
    except (TypeError, ValueError):
        _bwn = 1
    _bw = f"{_bwn}px"
    # Batch 3: a preset may carry border_soft (Glass: rgba(255,255,255,0.1))
    # — a faint 1px line used INSTEAD of the _bw border on the panel
    # surfaces and the download table, so a borderless preset's lighter
    # panels still read as separate sheets. Qt's QSS parser takes the CSS
    # rgba() form as-is. Empty for every other preset, where _panel_border
    # and _hdr_border expand to exactly what those rules said before.
    _soft = str(p.get("border_soft") or "").strip()
    _panel_border = f"1px solid {_soft}" if _soft else f"{_bw} solid {p['border']}"
    _hdr_border = f"1px solid {_soft}" if _soft else f"1px solid {p['border']}"
    # A borderless preset still needs a visible keyboard focus ring.
    _bw_focus = _bw if _bwn else "1px"
    _ind = _z(16)       # checkbox / radio indicator size
    _sb = _z(10)        # scrollbar thickness

    qss = f"""
QMainWindow, QWidget {{
    background: {p['bg_base']};
    color: {p['text']};
    font-family: {_family};{_weight}{_base_font}
}}
QLabel {{ background: transparent; }}
QWidget#Plain {{ background: transparent; }}
QDialog {{ background: {p['bg_panel']}; }}
QScrollArea {{ background: transparent; border: 0; }}
QToolTip {{
    background: {p['bg_elevated']}; color: {p['text']};
    border: {_bw} solid {p['border_hi']}; padding: {pad(6, 8)};
}}

/* ---- menus ---- */
QMenuBar {{ background: {p['bg_panel']}; color: {p['text_muted']}; padding: {pad(3, 8)};
    border-bottom: 1px solid {p['border']}; }}
QMenuBar::item {{ padding: {pad(6, 12)}; background: transparent; }}
QMenuBar::item:selected {{ background: {p['bg_hover']}; color: {p['text']}; border-radius: {_rs}; }}
QMenu {{ background: {p['bg_elevated']}; border: {_bw} solid {p['border_hi']};
    border-radius: {_rad}; padding: {pad(6)}; }}
QMenu::item {{ padding: {pad(8, 26, 8, 12)}; border-radius: {_rs}; }}
QMenu::item:selected {{ background: {p['bg_sel']}; }}
QMenu::separator {{ height: 1px; background: {p['border']}; margin: {pad(6, 8)}; }}

/* ---- toolbar ---- */
#Toolbar {{ background: {p['bg_panel']}; border-bottom: 1px solid {p['border']}; }}
#ToolbarTitle {{ font-size: {fs(16)}px; font-weight: 600; }}
#ToolbarButton {{
    background: transparent; border: 1px solid transparent;
    border-radius: {_rad}; padding: {pad(8, 11)}; color: {p['text_soft']};
}}
#ToolbarButton:hover {{ background: {p['bg_hover']}; border-color: {p['border_hi']}; color: {p['text']}; }}
#ToolbarButton:pressed {{ background: {p['bg_sel']}; }}
#Search {{
    background: {p['bg_input']}; border: {_bw} solid {p['border']};
    border-top: 2px solid {_shade(p['bg_input'], -0.4)};
    border-radius: {_rad}; padding: {pad(7, 12, 8, 12)}; min-width: {_z(200)}px;
}}
#Search:focus {{ border-color: {p['accent']}; }}

/* ---- sidebar ---- */
#Sidebar {{ background: {p['bg_side']}; border-right: 1px solid {p['border']}; }}
#Brand {{ font-size: {fs(12)}px; font-weight: 700; letter-spacing: 1px; color: {p['text']}; }}
#SectionLabel {{ color: {p['text_dim']}; font-size: {fs(10)}px; font-weight: 700; letter-spacing: 1.4px; }}
#SidebarSeparator {{ color: {p['border']}; background: {p['border']}; max-height: 1px; }}
QToolButton[nav="true"] {{
    text-align: left; background: transparent; border: 0;
    border-radius: {_rad}; color: {p['text_soft']}; padding: {pad(0, 10)};
}}
QToolButton[nav="true"]:hover {{ background: {p['bg_hover']}; color: {p['text']}; }}
QToolButton[nav="true"][active="true"] {{ background: {p['bg_sel']}; color: {p['text']}; font-weight: 600; }}

/* ---- download table ---- */
QTableView {{
    background: {p['bg_panel']};
    alternate-background-color: {p['bg_base']};
    border: {_panel_border};
    border-radius: {_rad};
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
    border-bottom: {_hdr_border}; padding: {pad(11, 10)};
    font-size: {fs(11)}px; font-weight: 600;
}}
QHeaderView::section:hover {{ color: {p['text']}; }}
#Details {{
    background: {p['bg_input']}; border: {_bw} solid {p['border']}; border-radius: {_rad};
    border-top: 2px solid {_shade(p['bg_input'], -0.4)};
    padding: {pad(7, 12, 8, 12)}; color: {p['text_soft']}; font-size: {fs(12)}px;
}}
#EmptyState {{ color: {p['text_dim']}; font-size: {fs(14)}px; background: transparent; }}
#LogLine {{
    background: {p['bg_input']}; border: {_bw} solid {p['border']}; border-radius: {_rad};
    border-top: 2px solid {_shade(p['bg_input'], -0.4)};
    padding: {pad(7, 12, 8, 12)}; color: {p['text_muted']}; font-family: {p['font_mono']}; font-size: {fs(11)}px;
}}

/* ---- status bar ---- */
QStatusBar, #BottomStatus {{ background: {p['bg_panel']}; border-top: 1px solid {p['border']}; color: {p['text_muted']}; }}
#StatusLabel {{ color: {p['success']}; padding-left: 10px; }}
#HoldLabel {{ color: {p['warning']}; padding-right: 12px; }}
#UpdateBadge {{ color: {p['accent']}; border: 0; padding: {pad(0, 12)}; font-weight: 600; background: transparent; }}
#UpdateBadge:hover {{ color: {p['text']}; }}

/* ---- buttons and inputs ---- */
QPushButton {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {_shade(p['bg_elevated'], 0.07)}, stop:1 {p['bg_elevated']});
    border: {_bw} solid {p['border']}; border-bottom: 2px solid {_shade(p['border'], -0.35)};
    border-radius: {_rad}; padding: {pad(8, 14, 7, 14)};
}}
QPushButton:hover {{ background: {p['bg_hover']}; border-color: {p['border_hi']};
    border-bottom-color: {_shade(p['border_hi'], -0.35)}; }}
QPushButton:pressed {{ background: {p['bg_sel']}; border-bottom: 1px solid {p['border']};
    padding: {pad(9, 14, 7, 14)}; }}
QPushButton:disabled {{ color: {p['text_dim']}; background: {p['bg_base']}; border-color: {p['border']}; }}
QPushButton#primary {{ background: {p['accent']}; color: {p['text_on_accent']}; border: none; border-bottom: 2px solid {_shade(p['accent'], -0.3)};
    font-weight: 600; }}
QPushButton#primary:hover {{ background: {p['accent_dim']}; }}
QToolButton {{ background: transparent; border: 1px solid transparent; border-radius: {_rad}; padding: {pad(4)}; }}
QToolButton:hover {{ background: {p['bg_hover']}; border-color: {p['border']}; }}
QLineEdit, QComboBox, QSpinBox, QTextEdit, QPlainTextEdit {{
    background: {p['bg_input']}; border: {_bw} solid {p['border']};
    border-top: 2px solid {_shade(p['bg_input'], -0.4)};
    border-radius: {_rad}; padding: {pad(5, 10, 6, 10)};
    selection-background-color: {p['accent_dim']};
}}
QLineEdit:hover, QComboBox:hover, QSpinBox:hover {{ border-color: {p['border_hi']}; }}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QTextEdit:focus, QPlainTextEdit:focus {{ border: {_bw_focus} solid {p['accent']}; }}
QComboBox QAbstractItemView {{
    background: {p['bg_elevated']}; border: {_bw} solid {p['border']};
    selection-background-color: {p['bg_sel']};
}}
QCheckBox, QRadioButton {{ spacing: {_z(8)}px; background: transparent; }}
QCheckBox::indicator, QRadioButton::indicator {{
    width: {_ind}px; height: {_ind}px; background: {p['bg_input']};
    border: {_bw} solid {p['border_hi']}; border-radius: {_z(4)}px;
}}
QRadioButton::indicator {{ border-radius: {_ind // 2 + 1}px; }}
QCheckBox::indicator:hover, QRadioButton::indicator:hover {{ border-color: {p['accent']}; }}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
    background: {p['accent']}; border-color: {p['accent']};
}}
QCheckBox::indicator:disabled, QRadioButton::indicator:disabled {{ border-color: {p['border']}; }}
QProgressBar {{
    background: {p['bg_elevated']}; border: {_bw} solid {p['border']};
    border-radius: {p['radius_pill']}; text-align: center; color: {p['text_muted']};
}}
QProgressBar::chunk {{ background: {p['accent']}; border-radius: {p['radius_pill']}; }}

/* ---- tabs and lists ---- */
QTabWidget::pane {{ border: {_panel_border}; border-radius: {_rad}; top: -1px; }}
QTabBar::tab {{
    background: {p['bg_panel']}; border: {_bw} solid {p['border']}; border-bottom: none;
    border-top-left-radius: {_rad}; border-top-right-radius: {_rad};
    padding: {pad(8, 16)}; color: {p['text_muted']};
}}
QTabBar::tab:selected {{ background: {p['bg_elevated']}; color: {p['text']}; border-bottom: 2px solid {p['accent']}; }}
QTabBar::tab:hover:!selected {{ background: {p['bg_elevated']}; }}
QListWidget {{ background: {p['bg_panel']}; border: {_panel_border}; border-radius: {_rad}; outline: 0; }}
QListWidget::item {{ padding: {pad(8, 12)}; border-radius: {_rs}; }}
QListWidget::item:hover {{ background: {p['bg_hover']}; }}
QListWidget::item:selected {{ background: {p['bg_sel']}; color: {p['text']}; }}
QListWidget#SettingsNav {{ background: {p['bg_side']}; padding: 4px; }}
QListWidget#SettingsNav::item {{ padding: {pad(9, 12)}; color: {p['text_soft']}; }}
QListWidget#SettingsNav::item:selected {{ background: {p['bg_sel']}; color: {p['text']}; }}

/* ---- focus, hover and disabled (v5.5) ---- */
QPushButton:focus {{ border: {_bw_focus} solid {p['accent']}; border-bottom: 2px solid {p['accent']}; }}
QPushButton#primary:focus {{ border: 2px solid {p['text']}; }}
QPushButton#primary:disabled {{ background: {p['bg_elevated']}; color: {_dis}; border: {_bw} solid {p['border']}; }}
QToolButton:focus, #ToolbarButton:focus, #SidebarTool:focus {{ border: {_bw_focus} solid {p['accent']}; }}
QToolButton[nav="true"]:focus {{ border: {_bw_focus} solid {p['accent']}; }}
QCheckBox:focus, QRadioButton:focus {{ color: {p['text']}; }}
QCheckBox::indicator:focus, QRadioButton::indicator:focus {{ border: 2px solid {p['accent']}; }}
QTabBar::tab:focus {{ border-color: {p['accent']}; }}
QListWidget:focus, QTableView:focus {{ border: {_bw_focus} solid {p['accent']}; }}
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
QScrollBar:vertical {{ background: transparent; width: {_sb}px; margin: {_z(2)}px; }}
QScrollBar::handle:vertical {{ background: {p['border']}; border-radius: {_sb // 2}px; min-height: {_z(30)}px; }}
QScrollBar::handle:vertical:hover {{ background: {p['border_hi']}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar:horizontal {{ background: transparent; height: {_sb}px; margin: {_z(2)}px; }}
QScrollBar::handle:horizontal {{ background: {p['border']}; border-radius: {_sb // 2}px; min-width: {_z(30)}px; }}
QScrollBar::handle:horizontal:hover {{ background: {p['border_hi']}; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}

/* ---- theme page cards ---- */
#Card {{ background: {p['bg_panel']}; border: {_panel_border}; border-radius: {_rad}; }}
#CardTitle {{ font-size: {fs(13)}px; font-weight: 700; }}
#CardHint {{ color: {p['text_muted']}; font-size: {fs(11)}px; }}
#PageTitle {{ font-size: {fs(18)}px; font-weight: 700; }}
#Chip {{ background: {p['bg_elevated']}; border: {_bw} solid {p['border']}; border-radius: {_rc};
    padding: {pad(2, 10)}; color: {p['text_muted']}; font-size: {fs(11)}px; }}
#Warn {{ color: {p['warning']}; font-size: {fs(11)}px; }}

/* ---- sidebar tools row ---- */
#SidebarTool {{
    background: transparent; border: 0; border-radius: {_rad};
    color: {p['text_soft']}; padding: {pad(6, 4)}; font-size: {fs(11)}px;
}}
#SidebarTool:hover {{ background: {p['bg_hover']}; color: {p['text']}; }}

/* ---- download detail pane ---- */
#DetailPane {{ background: {p['bg_panel']}; border: {_panel_border}; border-radius: {_rad}; }}
#DetailTitle {{ font-size: {fs(14)}px; font-weight: 700; }}
#DetailSub, #StatCaption, #DetailKey {{ color: {p['text_muted']}; font-size: {fs(11)}px; }}
#StatValue {{ font-size: {fs(13)}px; font-weight: 600; }}
QProgressBar#DetailBar {{ min-height: 12px; max-height: 12px; }}
"""
    if (settings_state or {}).get("frame_cream"):
        qss += _cream_frame_qss(p, _zoom)
    return qss


def _cream_frame_qss(p, zoom=1.0):
    """Window frame (toolbar, menu bar, status bar, margins) around the
    theme's dark panels, painted in the palette's "frame" token (cream by
    default). Scoped to the main window's own widgets so dialogs keep the
    normal theme. Highlight, edge and text colors are derived from the token,
    so one color picker drives all of them."""
    fb = p["frame"]
    _ps = float(p.get("pad_scale", 1.0) or 1.0) * zoom    # v5.5 density token, times the Batch 2 zoom
    _rad = scale_px(p.get("radius", "10px"), zoom)

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
    border-radius: {_rad}; padding: {pad(8, 11, 7, 11)};
}}
#ToolbarButton:hover {{ background: {fh}; border-color: {fd}; color: {ft}; }}
#ToolbarButton:pressed {{ background: {fe}; border-bottom: 1px solid {fd}; }}
#Content #SectionLabel {{ color: {fm}; }}
QStatusBar, #BottomStatus {{ background: {fb}; color: {ft}; border-top: 1px solid {fe}; }}
QStatusBar QLabel {{ color: {ft}; }}
"""
