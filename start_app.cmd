@echo off
rem Starts Job Copilot in its own window. Close this window to stop the app.
rem Listens on this computer only: your profile and extension token are shown in the app.
rem No file watching: it floods this window with harmless library messages. Restart after code changes.
cd /d "%~dp0"
.venv\Scripts\python.exe -m streamlit run copilot_app.py --server.port 8502 --server.address localhost --server.fileWatcherType none
