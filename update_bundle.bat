@echo off
setlocal enableextensions

echo Updating everything_bundle.txt...

:: 1. Define the output file and the target folder
set "OUTFILE=everything_bundle.txt"
set "ROOT=%~dp0"
cd /d "%ROOT%"

:: Delete the old bundle so we start fresh
if exist "%OUTFILE%" del /f /q "%OUTFILE%"

:: 2. Add Backend Python and Specs
echo. >> "%OUTFILE%"
echo ========================================================= >> "%OUTFILE%"
echo BACKEND FILES >> "%OUTFILE%"
echo ========================================================= >> "%OUTFILE%"
type backend\*.py >> "%OUTFILE%"
type backend\*.spec >> "%OUTFILE%"
type backend\*.bat >> "%OUTFILE%"
type backend\*.md >> "%OUTFILE%"

:: 3. Add CI/CD Workflow
echo. >> "%OUTFILE%"
echo ========================================================= >> "%OUTFILE%"
echo CI/CD WORKFLOW >> "%OUTFILE%"
echo ========================================================= >> "%OUTFILE%"
type .github\workflows\*.yml >> "%OUTFILE%"

:: 4. Add Extension Files
echo. >> "%OUTFILE%"
echo ========================================================= >> "%OUTFILE%"
echo EXTENSION FILES >> "%OUTFILE%"
echo ========================================================= >> "%OUTFILE%"
type extension\*.js extension\*.html extension\*.json extension\*.css >> "%OUTFILE%"

:: 5. Add a note at the end
echo. >> "%OUTFILE%"
echo === END OF BUNDLE === >> "%OUTFILE%"

echo Done! everything_bundle.txt has been updated.
echo.
pause