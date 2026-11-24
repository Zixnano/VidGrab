#!/usr/bin/env python3
"""Tiny test runner for machines without pytest. `python tests/run_tests.py`

Provides the same `tmp_path` fixture the tests use under pytest. With pytest installed,
`python -m pytest tests` works too. With PySide6 installed the Qt tests run for real
(offscreen); without it they run against a stub. Node.js is needed for the JavaScript tests.
"""
import importlib.util
import inspect
import sys
import tempfile
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "backend"))
sys.path.insert(0, str(HERE))


def main():
    failed = passed = 0
    for path in sorted(HERE.glob("test_*.py")):
        spec = importlib.util.spec_from_file_location(path.stem, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        for name, fn in inspect.getmembers(mod, inspect.isfunction):
            if not name.startswith("test_") or fn.__module__ != mod.__name__:
                continue
            with tempfile.TemporaryDirectory() as tmp:
                kwargs = {"tmp_path": Path(tmp)} if "tmp_path" in inspect.signature(fn).parameters else {}
                try:
                    fn(**kwargs)
                    passed += 1
                except Exception:
                    failed += 1
                    print("FAIL", path.name, name)
                    traceback.print_exc()
    print("passed %d, failed %d" % (passed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
