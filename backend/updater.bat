@echo off
setlocal enableextensions
rem updater.bat — post-exit swap, spawned by updater.py with:
rem   %1 = install root  (VideoGrabber.exe, VideoGrabber_internal\, app\, runtime\)
rem   %2 = staged pkg    (extracted thin zip: VideoGrabber.exe +
rem                       VideoGrabber_internal\ + app\)
rem   %3 = release tag   (e.g. v43) — stamped into install-version.txt
rem Windows won't touch a running .exe or a loaded DLL, so poll until the
rem process is gone, THEN rename/move. Renames always work where deletes
rem fail. Every rename/move is error-checked; on failure we roll back
rem only what THIS run changed (DID_* flags, reverse order), so a stale
rem leftover from an older aborted cycle can never be swapped in as if
rem it were this run's backup. Never deletes *.old mid-swap — the
rem launcher removes them on its next successful startup.

set "ROOT=%~1"
set "PKG=%~2"
set "NEWTAG=%~3"
set "DID_APP="
set "DID_EXE="
set "DID_INT="

if not defined ROOT (echo usage: updater.bat ^<install-root^> ^<staged-pkg^> ^<tag^> & exit /b 1)
if not defined PKG  (echo usage: updater.bat ^<install-root^> ^<staged-pkg^> ^<tag^> & exit /b 1)

:waitloop
tasklist /FI "IMAGENAME eq VideoGrabber.exe" 2>NUL | find /I "VideoGrabber.exe" >NUL
if not errorlevel 1 (
    timeout /t 1 /nobreak >NUL
    goto waitloop
)

rem Preflight: refuse to touch anything unless BOTH the staged pkg and
rem the current install look like real app folders. Otherwise a bogus
rem pkg would get the good app\ renamed away with nothing to restore.
if not exist "%PKG%\app\server.py" (
    echo updater.bat: staged pkg has no app\server.py — aborting untouched.
    start "" "%ROOT%\VideoGrabber.exe"
    endlocal
    exit /b 1
)
if not exist "%ROOT%\app\server.py" (
    echo updater.bat: no app\server.py at "%ROOT%" — nothing to update.
    endlocal
    exit /b 1
)

rem Clear ALL leftovers from a previous aborted cycle BEFORE renaming
rem anything. A leftover .old would collide with this run's renames, and
rem a rollback would mistake the stale copy for this run's backup.
if exist "%ROOT%\app.old" rmdir /s /q "%ROOT%\app.old"
if exist "%ROOT%\VideoGrabber.exe.old" del /f /q "%ROOT%\VideoGrabber.exe.old" 2>NUL
if exist "%ROOT%\VideoGrabber_internal.old" rmdir /s /q "%ROOT%\VideoGrabber_internal.old"

rem If anything survived the purge, a handle is still holding it —
rem swapping now would fail mid-way. Abort untouched.
if exist "%ROOT%\app.old" goto :blocked
if exist "%ROOT%\VideoGrabber.exe.old" goto :blocked
if exist "%ROOT%\VideoGrabber_internal.old" goto :blocked
goto :swap

:blocked
echo updater.bat: previous update leftovers are locked — aborting untouched.
start "" "%ROOT%\VideoGrabber.exe"
endlocal
exit /b 1

:swap
rename "%ROOT%\app" "app.old"
if errorlevel 1 goto :fail
rem Flag goes up right after the rename: from here the old app\ is the
rem only copy, so a later failure MUST roll this step back.
set "DID_APP=1"
move "%PKG%\app" "%ROOT%\app" >NUL
if errorlevel 1 goto :fail

rem Rename-then-replace for the launcher exe: del on an .exe never works
rem reliably (locks linger after exit), rename always does.
if not exist "%PKG%\VideoGrabber.exe" goto :no_exe
if not exist "%ROOT%\VideoGrabber.exe" goto :move_exe
rename "%ROOT%\VideoGrabber.exe" "VideoGrabber.exe.old"
if errorlevel 1 goto :fail
set "DID_EXE=1"
:move_exe
move "%PKG%\VideoGrabber.exe" "%ROOT%\VideoGrabber.exe" >NUL
if errorlevel 1 goto :fail
set "DID_EXE=1"
:no_exe

rem The launcher's bundled bits ride in the zip too — same rename pattern
rem (its DLLs may still be memory-mapped by the just-exited process).
if not exist "%PKG%\VideoGrabber_internal" goto :no_internal
if not exist "%ROOT%\VideoGrabber_internal" goto :move_internal
rename "%ROOT%\VideoGrabber_internal" "VideoGrabber_internal.old"
if errorlevel 1 goto :fail
set "DID_INT=1"
:move_internal
move "%PKG%\VideoGrabber_internal" "%ROOT%\VideoGrabber_internal" >NUL
if errorlevel 1 goto :fail
set "DID_INT=1"
:no_internal

rem Stamp the installed version so the updater compares against what is
rem actually on disk — not the APP_VERSION baked into the old source.
if defined NEWTAG (echo %NEWTAG%)> "%ROOT%\install-version.txt"

rmdir /s /q "%PKG%" 2>NUL
start "" "%ROOT%\VideoGrabber.exe"
endlocal
exit /b 0

:fail
rem Roll back only what THIS run changed, in reverse order, and only
rem from .old copies this run actually made — never from stale leftovers.
if "%DID_INT%"=="1" (
    if exist "%ROOT%\VideoGrabber_internal.old" (
        if exist "%ROOT%\VideoGrabber_internal" rmdir /s /q "%ROOT%\VideoGrabber_internal"
        rename "%ROOT%\VideoGrabber_internal.old" "VideoGrabber_internal"
    )
)
if "%DID_EXE%"=="1" (
    if exist "%ROOT%\VideoGrabber.exe.old" (
        if exist "%ROOT%\VideoGrabber.exe" del /f /q "%ROOT%\VideoGrabber.exe" 2>NUL
        rename "%ROOT%\VideoGrabber.exe.old" "VideoGrabber.exe"
    )
)
if "%DID_APP%"=="1" (
    if exist "%ROOT%\app.old" (
        if exist "%ROOT%\app" rmdir /s /q "%ROOT%\app"
        rename "%ROOT%\app.old" "app"
    )
)
echo updater.bat: swap failed — rolled back to the previous version. Staged files left in "%PKG%".
start "" "%ROOT%\VideoGrabber.exe"
endlocal
exit /b 1
