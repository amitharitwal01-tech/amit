@echo off
REM One-click launcher for Manuscript Engine.
REM Runs the app using pipeline_env's own Python directly — that already
REM uses that environment's installed packages, so there's no need to
REM run "activate" first. Uses pythonw.exe so no console window opens.

cd /d "%~dp0"

if not exist "%~dp0pipeline_env\Scripts\pythonw.exe" (
    echo Could not find pipeline_env\Scripts\pythonw.exe next to this file.
    echo Make sure Launch_Manuscript_Engine.bat sits in the same folder as
    echo manuscript_engine.py and the pipeline_env folder you created with:
    echo     python -m venv pipeline_env
    pause
    exit /b 1
)

start "" /B "%~dp0pipeline_env\Scripts\pythonw.exe" "%~dp0manuscript_engine.py"
exit
