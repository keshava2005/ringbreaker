@echo off
title RingBreaker
cd /d "%~dp0"
set "PY=python"
where py >nul 2>nul && set "PY=py -3"
if not exist ".venv\Scripts\python.exe" ( %PY% -m venv .venv || goto :nopy )
call ".venv\Scripts\activate.bat"
echo Installing packages (first run only)...
python -m pip install -q --disable-pip-version-check -r requirements.txt || goto :fail
if not exist "data\app\overview.json" ( echo Building data, pipeline and model - about 2 minutes... & python -m ringbreaker build || goto :fail )
start "" cmd /c "timeout /t 4 >nul & start http://127.0.0.1:8000"
python -m ringbreaker serve
goto :end
:nopy
echo Python 3.11+ is required: https://www.python.org/downloads/
goto :end
:fail
echo Something failed above. Copy the error into the chat.
:end
pause
