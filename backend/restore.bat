@echo off
setlocal enableextensions
rem restore.bat — rollback. Moves app.old\, VideoGrabber.exe.old, and
rem VideoGrabber_internal.old back into place, then relaunches.
rem Refuses to run while VideoGrabber.exe is open (Windows file locks).
rem Optional %1 overrides the install root.

set "ROOT=%~1"
if not defined ROOT set "ROOT=C:\Users\Zix Nano\Downloads\Compressed\video 3 grabber"

tasklist /FI "IMAGENAME eq VideoGrabber.exe" 2>NUL | find /I "VideoGrabber.exe" >NUL
if not errorlevel 1 (
    echo restore.bat: VideoGrabber is still running — close VideoGrabber first.
    endlocal
    exit /b 1
)

if not exist "%ROOT%\app.old" (
    echo restore.bat: no app.old in "%ROOT%" — nothing to restore.
    endlocal
    exit /b 1
)

rem The current app\ is the broken one — the rollback copy replaces it.
if exist "%ROOT%\app" rmdir /s /q "%ROOT%\app"
rename "%ROOT%\app.old" "app"
if errorlevel 1 (
    echo restore.bat: restore failed — rename app.old failed.
    endlocal
    exit /b 1
)

rem Launcher exe: rename-then-replace, same pattern as updater.bat.
if exist "%ROOT%\VideoGrabber.exe.old" (
    if exist "%ROOT%\VideoGrabber.exe" del /f /q "%ROOT%\VideoGrabber.exe" 2>NUL
    rename "%ROOT%\VideoGrabber.exe.old" "VideoGrabber.exe"
)

rem The launcher's bundled bits roll back with it.
if exist "%ROOT%\VideoGrabber_internal.old" (
    if exist "%ROOT%\VideoGrabber_internal" rmdir /s /q "%ROOT%\VideoGrabber_internal"
    rename "%ROOT%\VideoGrabber_internal.old" "VideoGrabber_internal"
)

start "" "%ROOT%\VideoGrabber.exe"
endlocal
exit /b 0
