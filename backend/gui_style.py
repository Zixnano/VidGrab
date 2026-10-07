"""QSS generation from the palette. Applied app-wide (launch_gui and after
every Settings save), so the main window, dialogs and settings all follow
the same tokens. Nothing in here should hardcode a color."""
from palette import resolve_palette


def build_qss(settings_state=None):
    p = resolve_palette(settings_state)
    return f"""
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
    border-radius: {p['radius']}; padding: 8px 12px; min-width: 200px;
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
    padding: 8px 12px; color: {p['text_soft']}; font-size: 12px;
}}
#EmptyState {{ color: {p['text_dim']}; font-size: 14px; background: transparent; }}
#LogLine {{
    background: {p['bg_input']}; border: 1px solid {p['border']}; border-radius: {p['radius']};
    padding: 8px 12px; color: {p['text_muted']}; font-family: {p['font_mono']}; font-size: 11px;
}}

/* ---- status bar ---- */
QStatusBar, #BottomStatus {{ background: {p['bg_panel']}; border-top: 1px solid {p['border']}; color: {p['text_muted']}; }}
#StatusLabel {{ color: {p['success']}; padding-left: 10px; }}
#HoldLabel {{ color: {p['warning']}; padding-right: 12px; }}
#UpdateBadge {{ color: {p['accent']}; border: 0; padding: 0 12px; font-weight: 600; background: transparent; }}
#UpdateBadge:hover {{ color: {p['text']}; }}

/* ---- buttons and inputs ---- */
QPushButton {{
    background: {p['bg_elevated']}; border: 1px solid {p['border']};
    border-radius: {p['radius']}; padding: 8px 14px;
}}
QPushButton:hover {{ background: {p['bg_hover']}; border-color: {p['border_hi']}; }}
QPushButton:pressed {{ background: {p['bg_sel']}; }}
QPushButton:disabled {{ color: {p['text_dim']}; background: {p['bg_base']}; border-color: {p['border']}; }}
QPushButton#primary {{ background: {p['accent']}; color: {p['text_on_accent']}; border: none; font-weight: 600; }}
QPushButton#primary:hover {{ background: {p['accent_dim']}; }}
QToolButton {{ background: transparent; border: 1px solid transparent; border-radius: {p['radius']}; padding: 4px; }}
QToolButton:hover {{ background: {p['bg_hover']}; border-color: {p['border']}; }}
QLineEdit, QComboBox, QSpinBox, QTextEdit, QPlainTextEdit {{
    background: {p['bg_input']}; border: 1px solid {p['border']};
    border-radius: {p['radius']}; padding: 6px 10px;
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
"""
