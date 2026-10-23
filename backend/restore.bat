@echo off
setlocal enableextensions
rem restore.bat: manual rollback. Moves app.old\, VideoGrabberApp.exe.old
rem and VideoGrabberApp_internal.old back into place, then relaunches
rem (VideoGrabber.exe is the supervisor; it starts the app). Refuses to
rem run while the app host (VideoGrabberApp.exe) is open: Windows file
rem locks. Optional %1 overrides the install root; the default is this
rem script's own folder, which IS the install root (fix 16: no
rem hardcoded user path ships in the zip).
rem Note: Recovery's Restore Backup (AppData backups) is the primary
rem path; this script covers the updater's .old copies.

set "ROOT=%~1"
if not defined ROOT set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

tasklist /FI "IMAGENAME eq VideoGrabberApp.exe" 2>NUL | find /I "VideoGrabberApp.exe" >NUL
if not errorlevel 1 (
    echo restore.bat: Video Grabber is still running - close Video Grabber first.
    endlocal
    exit /b 1
)

if not exist "%ROOT%\app.old" (
    echo restore.bat: no app.old in "%ROOT%" - nothing to restore.
    endlocal
    exit /b 1
)

rem The current app\ is the broken one; the rollback copy replaces it.
if exist "%ROOT%\app" rmdir /s /q "%ROOT%\app"
rename "%ROOT%\app.old" "app"
if errorlevel 1 (
    echo restore.bat: restore failed - rename app.old failed.
    endlocal
    exit /b 1
)

rem App host exe: rename-then-replace, same pattern as updater.bat.
if exist "%ROOT%\VideoGrabberApp.exe.old" (
    if exist "%ROOT%\VideoGrabberApp.exe" del /f /q "%ROOT%\VideoGrabberApp.exe" 2>NUL
    rename "%ROOT%\VideoGrabberApp.exe.old" "VideoGrabberApp.exe"
    if errorlevel 1 (
        echo restore.bat: restore failed - rename VideoGrabberApp.exe.old failed. Not relaunching a mixed install.
        endlocal
        exit /b 1
    )
)

rem The host's bundled bits roll back with it.
if exist "%ROOT%\VideoGrabberApp_internal.old" (
    if exist "%ROOT%\VideoGrabberApp_internal" rmdir /s /q "%ROOT%\VideoGrabberApp_internal"
    rename "%ROOT%\VideoGrabberApp_internal.old" "VideoGrabberApp_internal"
    if errorlevel 1 (
        echo restore.bat: restore failed - rename VideoGrabberApp_internal.old failed. Not relaunching a mixed install.
        endlocal
        exit /b 1
    )
)

start "" "%ROOT%\VideoGrabber.exe"
endlocal
exit /b 0
