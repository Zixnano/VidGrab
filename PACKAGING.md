# PACKAGING - release zip layout (permanent constraint)

Every `VideoGrabber_vX.Y.Z.zip` MUST extract to this layout. Do not ship a
flat layout. Verify before finalizing any release zip:

```
ls <root>/*.py                 # must be EMPTY (no .py at zip root)
ls <root>/backend/*.py | wc -l # must be 10
ls <root>/backend/VideoGrabber.ico   # must exist (CI --icon needs it)
ls <root>/extension/icons/*.png | wc -l  # must be 4
ls <root>/*.md | wc -l         # must be 7+
```

Placement rules:
- backend module -> backend/ | test -> backend/tests/ | extension file -> extension/
- doc -> repo root | icon -> repo root (extension-only icons -> extension/icons/)

History: zips v4.0.0-v4.0.4 shipped .py files at zip root; extraction
required manual `move *.py backend\`. Fixed in the v4.0.5 packaging.
