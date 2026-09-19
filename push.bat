@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ===================================
echo   Video Grabber - push to GitHub
echo ===================================
echo.

git rev-parse --is-inside-work-tree >nul 2>&1
if errorlevel 1 (
    echo ERROR: this folder is not a git repository ^(no .git found^).
    echo Run this from inside your cloned repo, or "git init" first.
    pause
    exit /b 1
)

echo Checking for changes...
git add -A

git diff --cached --quiet
if not errorlevel 1 (
    echo Nothing to commit - working tree already matches the last commit.
    echo.
    set /p pullonly="Pull latest from origin anyway? (y/N): "
    if /i "!pullonly!"=="y" (
        git pull --rebase
    )
    pause
    exit /b 0
)

echo.
echo Changed files:
git status --short
echo.

set "msg=%~1"
if "%msg%"=="" (
    set /p msg="Commit message (Enter for an auto-generated one): "
)
if "%msg%"=="" (
    for /f "tokens=1-3 delims=/ " %%a in ("%date%") do set d=%%a-%%b-%%c
    set "msg=Update %d% %time%"
)

git commit -m "%msg%"
if errorlevel 1 (
    echo.
    echo Commit failed - see the error above. Nothing was pushed.
    pause
    exit /b 1
)

echo.
echo Pulling any remote changes first (rebase, so your commit stays on top)...
git pull --rebase
if errorlevel 1 (
    echo.
    echo Pull/rebase failed - most likely a merge conflict.
    echo Resolve it manually (git status will show what's conflicted), then
    echo run: git rebase --continue
    echo ...and re-run this script to push once that's done.
    pause
    exit /b 1
)

echo.
echo Pushing to origin...
git push
if errorlevel 1 (
    echo.
    echo Push failed. If this is the first push on a new branch, try:
    echo   git push --set-upstream origin main
    echo ^(replace "main" with your branch name if it's different^)
    pause
    exit /b 1
)

echo.
echo Done - pushed successfully.
pause
