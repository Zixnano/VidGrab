@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

git rev-parse --is-inside-work-tree >nul 2>&1
if errorlevel 1 (
    echo Not a git repository. Run this from inside the repo.
    pause
    exit /b 1
)

echo.
echo ==========================================
echo   Video Grabber - push to GitHub
echo ==========================================
echo.

git add -A

git diff --cached --quiet
if not errorlevel 1 (
    echo Nothing to commit.
) else (
    git status --short
    set "msg=%~1"
    if not defined msg set /p "msg=Commit message: "
    if not defined msg (
        for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd HH:mm"') do set "msg=Update %%i"
    )
    git commit -m "!msg!"
    if errorlevel 1 (
        echo Commit failed. Nothing was pushed.
        pause
        exit /b 1
    )
)

echo.
echo Pulling latest changes (rebase)...
git pull --rebase origin main
if errorlevel 1 (
    echo Pull failed - resolve conflicts, then: git rebase --continue, and re-run push.bat
    pause
    exit /b 1
)

echo.
echo Pushing to origin main...
git push origin main
if errorlevel 1 (
    echo Push failed.
    pause
    exit /b 1
)

echo.
echo Done.
pause
