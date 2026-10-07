@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe py -m venv .venv
if errorlevel 1 goto failed
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto failed
.venv\Scripts\python.exe -m streamlit run app.py
goto end
:failed
echo Setup failed. Check Python installation and the error above.
:end
pause
