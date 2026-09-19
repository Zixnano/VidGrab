# PACKAGING - release zip layout (permanent constraint)

Every `VideoGrabber_vX.Y.Z.zip` MUST extract to this layout. Do not ship a
flat layout. Verify before finalizing any release zip:

```
ls <root>/*.py                 # must be EMPTY (no .py at zip root)
ls <root>/backend/*.py | wc -l # must be 10
ls <root>/backend/VideoGrabber.ico   # must exist (CI --icon needs it)
ls <root>/extension/icons/*.png | wc -l  # must be 4
ls <root>/*.md | wc -l         # must be 7+
ls <root>/.github/workflows/build-exe.yml  # must exist (CI trigger)
```

**Gotcha (bit us in v4.0.5–v4.0.9):** if you build the zip with a naive
`zip -r -X archive.zip . -x '.*'`, the `-x '.*'` exclude also matches
`.github` (it starts with a dot too), silently dropping the entire CI
workflow from every release zip with no error. Exclude specific junk
instead: `-x '.git/*' -x '.pytest_cache/*' -x '*__pycache__*' -x '*.pyc'`
— never a bare `.*`.

Placement rules:
- backend module -> backend/ | test -> backend/tests/ | extension file -> extension/
- doc -> repo root | icon -> repo root (extension-only icons -> extension/icons/)
- helper script (e.g. `push.bat`) -> repo root, alongside the docs

History: zips v4.0.0-v4.0.4 shipped .py files at zip root; extraction
required manual `move *.py backend\`. Fixed in the v4.0.5 packaging.
