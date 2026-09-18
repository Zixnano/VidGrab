"""QSS generation from the palette. Applied app-wide in launch_gui()."""
from palette import resolve_palette


def build_qss(settings_state=None):
    p = resolve_palette(settings_state)
    return f"""
QMainWindow, QWidget {{
    background: {p['bg_base']};
    color: {p['text']};
    font-family: {p['font']};
}}
QTableView {{
    background: {p['bg_panel']};
    alternate-background-color: {p['bg_base']};
    border: 1px solid {p['border']};
    border-radius: {p['radius']};
    gridline-color: {p['border']};
    selection-background-color: {p['accent_dim']};
    selection-color: {p['text']};
}}
QTableView::item {{
    padding: 4px;
}}
QHeaderView::section {{
    background: {p['bg_elevated']};
    color: {p['text_muted']};
    border: 0;
    border-bottom: 1px solid {p['border']};
    border-right: 1px solid {p['border']};
    padding: 8px;
}}
QPushButton {{
    background: {p['bg_panel']};
    border: 1px solid {p['border']};
    border-radius: {p['radius']};
    padding: 8px 14px;
}}
QPushButton:hover {{
    background: {p['bg_elevated']};
    border-color: {p['border_hi']};
}}
QPushButton:pressed {{
    background: {p['accent_dim']};
}}
QPushButton#primary {{
    background: {p['accent']};
    color: {p['bg_base']};
    border: none;
}}
QPushButton#primary:hover {{
    background: {p['accent_dim']};
}}
QLineEdit, QComboBox, QSpinBox, QTextEdit, QPlainTextEdit {{
    background: {p['bg_panel']};
    border: 1px solid {p['border']};
    border-radius: {p['radius']};
    padding: 6px 10px;
    selection-background-color: {p['accent_dim']};
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QTextEdit:focus, QPlainTextEdit:focus {{
    border-color: {p['accent']};
}}
QComboBox QAbstractItemView {{
    background: {p['bg_elevated']};
    border: 1px solid {p['border']};
    selection-background-color: {p['accent_dim']};
}}
QMenu {{
    background: {p['bg_elevated']};
    border: 1px solid {p['border']};
    border-radius: {p['radius']};
    padding: 6px;
}}
QMenu::item {{
    padding: 8px 24px;
    border-radius: 6px;
}}
QMenu::item:selected {{
    background: {p['accent_dim']};
}}
QMenu::separator {{
    height: 1px;
    background: {p['border']};
    margin: 6px 8px;
}}
QMenuBar {{
    background: {p['bg_base']};
    border-bottom: 1px solid {p['border']};
}}
QMenuBar::item {{
    padding: 6px 12px;
}}
QMenuBar::item:selected {{
    background: {p['bg_elevated']};
    border-radius: 6px;
}}
QStatusBar {{
    background: {p['bg_panel']};
    border-top: 1px solid {p['border']};
    color: {p['text_muted']};
}}
QTabWidget::pane {{
    border: 1px solid {p['border']};
    border-radius: {p['radius']};
    top: -1px;
}}
QTabBar::tab {{
    background: {p['bg_panel']};
    border: 1px solid {p['border']};
    border-bottom: none;
    border-top-left-radius: {p['radius']};
    border-top-right-radius: {p['radius']};
    padding: 8px 16px;
    color: {p['text_muted']};
}}
QTabBar::tab:selected {{
    background: {p['bg_elevated']};
    color: {p['text']};
}}
QTabBar::tab:hover:!selected {{
    background: {p['bg_elevated']};
}}
QDialog {{
    background: {p['bg_base']};
}}
QToolTip {{
    background: {p['bg_elevated']};
    color: {p['text']};
    border: 1px solid {p['border']};
    padding: 6px 8px;
}}
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: {p['border']};
    border-radius: 5px;
    min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{
    background: {p['border_hi']};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}
QScrollBar:horizontal {{
    background: transparent;
    height: 10px;
    margin: 2px;
}}
QScrollBar::handle:horizontal {{
    background: {p['border']};
    border-radius: 5px;
    min-width: 30px;
}}
QScrollBar::handle:horizontal:hover {{
    background: {p['border_hi']};
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0;
}}
QProgressBar {{
    background: {p['bg_elevated']};
    border: 1px solid {p['border']};
    border-radius: {p['radius_pill']};
    text-align: center;
    color: {p['text_muted']};
}}
QProgressBar::chunk {{
    background: {p['accent']};
    border-radius: {p['radius_pill']};
}}
QCheckBox, QRadioButton {{
    spacing: 8px;
}}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 16px;
    height: 16px;
}}
"""
