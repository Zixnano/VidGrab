@echo off
setlocal enableextensions
rem updater.bat: the post-exit swapper. Called by the supervisor in
rem two situations:
rem   (a) FALLBACK: updater_ui.py exited non-zero. The bat performs
rem       the whole swap from the pkg the updater staged.
rem   (b) HOST mode (%4 = "host"): updater_ui succeeded but staged
rem       host files (VideoGrabberApp.exe / VideoGrabberApp_internal).
rem       It cannot swap those itself: its own process is running
rem       inside them. The supervisor calls the bat once that process
rem       has exited, and the bat swaps ONLY the host files.
rem Arguments: %1 = install root, %2 = staged pkg, %3 = release tag,
rem %4 = optional mode ("host").
rem
rem Discipline: renames always work where deletes fail; every step is
rem error-checked; on failure we roll back only what THIS run changed
rem (DID_* flags, reverse order). The pkg is COPIED from, never moved
rem or deleted, so a failed rollback can never destroy the only copy
rem of the new files; staging is wiped by the next update run. This
rem script never launches the app: the supervisor relaunches.

set "ROOT=%~1"
set "PKG=%~2"
set "NEWTAG=%~3"
set "MODE=%~4"
set "DID_APP="
set "DID_EXE="
set "DID_INT="

if not defined ROOT (echo usage: updater.bat ^<install-root^> ^<staged-pkg^> ^<tag^> [host] & exit /b 1)
if not defined PKG  (echo usage: updater.bat ^<install-root^> ^<staged-pkg^> ^<tag^> [host] & exit /b 1)

rem Wait for the app host to exit, at most ~30 seconds (fix 9: the
rem old loop waited forever, and a ghost host would hang the whole
rem update with the supervisor blocked inside this call). The match
rem is by full image path under THIS install root where PowerShell
rem can answer; the name-only tasklist check is the fallback for when
rem PowerShell itself cannot run.
set /a TRIES=0
:waitloop
set "RUNNING="
set "PSCHECK="
for /f "delims=" %%P in ('powershell -NoProfile -Command "$p = Get-Process VideoGrabberApp -ErrorAction SilentlyContinue | Where-Object { $_.Path -like '%ROOT%\*' }; if ($p) { 'yes' } else { 'no' }" 2^>NUL') do set "PSCHECK=%%P"
if "%PSCHECK%"=="yes" set "RUNNING=1"
if "%PSCHECK%"=="" (
    tasklist /FI "IMAGENAME eq VideoGrabberApp.exe" 2>NUL | find /I "VideoGrabberApp.exe" >NUL
    if not errorlevel 1 set "RUNNING=1"
)
if defined RUNNING (
    set /a TRIES+=1
    if %TRIES% GEQ 30 (
        echo updater.bat: VideoGrabberApp.exe still running after ~30s - aborting untouched.
        endlocal
        exit /b 1
    )
    timeout /t 1 /nobreak >NUL
    goto waitloop
)

if "%MODE%"=="host" goto :host_preflight

rem Preflight (full mode): refuse to touch anything unless BOTH the
rem staged pkg and the current install look like real app folders.
if not exist "%PKG%\app\server.py" (
    echo updater.bat: staged pkg has no app\server.py - aborting untouched.
    endlocal
    exit /b 1
)
if not exist "%ROOT%\app\server.py" (
    echo updater.bat: no app\server.py at "%ROOT%" - nothing to update.
    endlocal
    exit /b 1
)
goto :clear_leftovers

:host_preflight
if not exist "%PKG%\VideoGrabberApp.exe" if not exist "%PKG%\VideoGrabberApp_internal" (
    echo updater.bat: host mode but the pkg has no host files - aborting untouched.
    endlocal
    exit /b 1
)

:clear_leftovers
rem Clear leftovers from a previous aborted cycle BEFORE renaming
rem anything, so a rollback can never mistake a stale copy for this
rem run's backup. In host mode the app folder is not ours to clear.
if not "%MODE%"=="host" if exist "%ROOT%\app.old" rmdir /s /q "%ROOT%\app.old"
if exist "%ROOT%\VideoGrabberApp.exe.old" del /f /q "%ROOT%\VideoGrabberApp.exe.old" 2>NUL
if exist "%ROOT%\VideoGrabberApp_internal.old" rmdir /s /q "%ROOT%\VideoGrabberApp_internal.old"

if not "%MODE%"=="host" if exist "%ROOT%\app.old" goto :blocked
if exist "%ROOT%\VideoGrabberApp.exe.old" goto :blocked
if exist "%ROOT%\VideoGrabberApp_internal.old" goto :blocked
goto :swap

:blocked
echo updater.bat: previous update leftovers are locked - aborting untouched.
endlocal
exit /b 1

:swap
if "%MODE%"=="host" goto :swap_exe
rename "%ROOT%\app" "app.old"
if errorlevel 1 goto :fail
set "DID_APP=1"
robocopy "%PKG%\app" "%ROOT%\app" /E /NFL /NDL /NJH /NJS /NP >NUL
if errorlevel 8 goto :fail

:swap_exe
rem App host exe: rename-then-replace (del on an .exe is unreliable).
if not exist "%PKG%\VideoGrabberApp.exe" goto :no_exe
if not exist "%ROOT%\VideoGrabberApp.exe" goto :copy_exe
rename "%ROOT%\VideoGrabberApp.exe" "VideoGrabberApp.exe.old"
if errorlevel 1 goto :fail
set "DID_EXE=1"
:copy_exe
copy /y "%PKG%\VideoGrabberApp.exe" "%ROOT%\VideoGrabberApp.exe" >NUL
if errorlevel 1 goto :fail
set "DID_EXE=1"
:no_exe

rem The host's bundled bits ride in the zip too; same rename pattern.
if not exist "%PKG%\VideoGrabberApp_internal" goto :no_internal
if not exist "%ROOT%\VideoGrabberApp_internal" goto :copy_internal
rename "%ROOT%\VideoGrabberApp_internal" "VideoGrabberApp_internal.old"
if errorlevel 1 goto :fail
set "DID_INT=1"
:copy_internal
robocopy "%PKG%\VideoGrabberApp_internal" "%ROOT%\VideoGrabberApp_internal" /E /NFL /NDL /NJH /NJS /NP >NUL
if errorlevel 8 goto :fail
set "DID_INT=1"
:no_internal

rem Supervisor files are never in a thin package (contract: the
rem supervisor ships with the full setup only, like Recovery).

rem Stamp the installed version so update checks compare against
rem what is actually on disk.
if defined NEWTAG (echo %NEWTAG%)> "%ROOT%\install-version.txt"

endlocal
exit /b 0

:fail
rem Roll back only what THIS run changed, in reverse order.
if "%DID_INT%"=="1" (
    if exist "%ROOT%\VideoGrabberApp_internal.old" (
        if exist "%ROOT%\VideoGrabberApp_internal" rmdir /s /q "%ROOT%\VideoGrabberApp_internal"
        rename "%ROOT%\VideoGrabberApp_internal.old" "VideoGrabberApp_internal"
    ) else (
        rem No .old means this run copied the folder onto an install
        rem that had none; rolling back removes the copy, so a failed
        rem swap never leaves half of the new host behind.
        if exist "%ROOT%\VideoGrabberApp_internal" rmdir /s /q "%ROOT%\VideoGrabberApp_internal"
    )
)
if "%DID_EXE%"=="1" (
    if exist "%ROOT%\VideoGrabberApp.exe.old" (
        if exist "%ROOT%\VideoGrabberApp.exe" del /f /q "%ROOT%\VideoGrabberApp.exe" 2>NUL
        rename "%ROOT%\VideoGrabberApp.exe.old" "VideoGrabberApp.exe"
    ) else (
        rem Same rule for the exe: no .old, so delete the copy.
        if exist "%ROOT%\VideoGrabberApp.exe" del /f /q "%ROOT%\VideoGrabberApp.exe" 2>NUL
    )
)
if "%DID_APP%"=="1" (
    if exist "%ROOT%\app.old" (
        if exist "%ROOT%\app" rmdir /s /q "%ROOT%\app"
        rename "%ROOT%\app.old" "app"
    )
)
echo updater.bat: swap failed - rolled back to the previous version. The staged pkg is untouched at "%PKG%".
endlocal
exit /b 1
