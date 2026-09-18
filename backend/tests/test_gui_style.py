from gui_style import build_qss


def test_qss_contains_resolved_tokens():
    qss = build_qss({"theme_preset": "dark_gray"})
    assert "#" in qss and "QMainWindow" in qss and "QPushButton" in qss


def test_qss_reflects_token_override():
    qss_default = build_qss({})
    qss_red = build_qss({"theme_tokens": {"bg_base": "#ff0000"}})
    assert "#ff0000" in qss_red
    assert qss_red != qss_default
