@echo off
rem Run from the repo root (the folder with .git). Uses git mv so history is kept.
if not exist docs mkdir docs
if not exist docs\notes mkdir docs\notes
if not exist assets mkdir assets

for %%F in (ARCHITECTURE.md PACKAGING.md ICONS.md KNOWN_ISSUES.md DECISIONS.md BUILD_NOTES.md) do (
  if exist %%F git mv %%F docs\%%F
)
for %%F in (BUGS.md FIXES.md FIXES_NOTES.txt CHANGES.md AUDIT.md QUESTIONS.md) do (
  if exist %%F git mv %%F docs\notes\%%F
)
for %%F in (icon16.svg icon32.svg icon48.svg icon128.svg tray-32.png tray-32.svg VideoGrabber-32.svg VideoGrabber-128.png VideoGrabber-128.svg VideoGrabber-1024.png VideoGrabber-1024.svg) do (
  if exist %%F git mv %%F assets\%%F
)
if exist README.txt git rm -q README.txt
echo.
echo Done. Review with: git status
