@echo off
cd /d "%~dp0"
echo GrantLens — install Ollama from official HTTPS + pull qwen3:8b
py -3 "%~dp0_install_ollama.py"
echo.
pause
