@echo off
REM Same as Launch_Manuscript_Engine.bat, but keeps the console window open
REM and shows every print/error — use this one if the normal launcher
REM seems to do nothing when double-clicked, to see what actually happened.

cd /d "%~dp0"

REM Defensively drop out of any active conda environment (e.g. "base"
REM auto-activating in every new terminal) — see the comment in
REM Launch_Manuscript_Engine.bat for why this matters.
call conda deactivate >nul 2>&1
call conda deactivate >nul 2>&1

if not exist "%~dp0pipeline_env\Scripts\python.exe" (
    echo Could not find pipeline_env\Scripts\python.exe next to this file.
    echo Make sure this file sits in the same folder as manuscript_engine.py
    echo and the pipeline_env folder you created with:
    echo     python -m venv pipeline_env
    pause
    exit /b 1
)

"%~dp0pipeline_env\Scripts\python.exe" "%~dp0manuscript_engine.py"
echo.
echo ---- The app closed (or failed to start). Press any key to close this window. ----
pause >nul
