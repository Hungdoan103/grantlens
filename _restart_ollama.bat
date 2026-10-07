@echo off
cd /d "%~dp0"
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :8000 ^| findstr LISTENING') do (
  echo Killing PID %%a on port 8000
  taskkill /F /PID %%a >nul 2>&1
)
timeout /t 1 /nobreak >nul
py -3 "%~dp0_start_ollama.py"
