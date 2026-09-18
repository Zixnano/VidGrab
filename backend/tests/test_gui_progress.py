"""v4.0.1 Fix 2: numeric progress read from the job dict (helper-level)."""
import ast
from pathlib import Path


def _load_helper():
    src = (Path(__file__).parents[1] / "gui_qt.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
              and n.name == "_progress_pct")
    ns = {}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[])),
                 "gui_qt.py", "exec"), ns)
    return ns["_progress_pct"]


def test_download_pct_from_sizes():
    pct = _load_helper()
    assert pct({"size_done": 37, "size_total": 100}) == 37.0
    assert pct({"size_done": 5, "size_total": 0}) == 0.0  # zero-total guard
    assert pct({}) == 0.0


def test_converting_uses_conversion_progress():
    pct = _load_helper()
    assert pct({"phase": "converting", "conversion_progress": 42}) == 42.0
    assert pct({"phase": "converting"}) == 0.0
