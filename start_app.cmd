@echo off
rem Starts Job Copilot in its own window. Close this window to stop the app.
cd /d "%~dp0"
.venv\Scripts\python.exe -m streamlit run copilot_app.py --server.port 8502
