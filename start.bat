@echo off
rem start.bat — launch the Simple Video Share Flask app.
rem
rem Usage: start.bat
rem   - Creates .venv and installs requirements if missing.
rem   - Seeds config.json / secret key on first run (handled by config.py).
rem   - Initialises the SQLite DB (handled by db.init_db() via create_app()).
rem   - Runs the app on the host/port from config.json.
rem
rem Note: the venv's Scripts dir is on the PATH for this batch file, so the
rem "python" / "pip" below resolve to the environment's interpreters.

setlocal enabledelayedexpansion

rem Resolve the project root (directory containing this script), so it works
rem regardless of the caller's current working directory.
set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

set "VENV_DIR=%SCRIPT_DIR%.venv"

rem Honour PYTHON if the caller set it, otherwise default to python3.
set "PYTHON=%PYTHON%"
if not defined PYTHON set "PYTHON=python"

rem 1. Create a virtual environment if it does not exist yet.
if not exist "%VENV_DIR%\Scripts\python.exe" (
    echo Creating virtual environment at %VENV_DIR% ...
    %PYTHON% -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo ERROR: failed to create the virtual environment. >&2
        exit /b 1
    )
)

rem Bring the venv's Scripts dir to the front of the PATH so that the
rem "python" / "pip" commands below resolve to the environment's tools.
set "PATH=%VENV_DIR%\Scripts;%PATH%"

rem 2. Ensure dependencies are installed (pip is idempotent, so this is a
rem    no-op once the environment is already set up).
python -m pip install -q -r requirements.txt
if errorlevel 1 (
    echo ERROR: failed to install requirements. >&2
    exit /b 1
)

rem 3. Launch the app (host/port/secret come from config.json, auto-seeded).
echo Starting Simple Video Share ...
python main.py

endlocal