@echo off
REM Installs the NTPC Power Data Chatbot from this offline package.
REM Right-click this file and choose "Run as administrator".
REM Add  -WithOllama  below if the AI model (Ollama) should run on this same server.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" -InstallDir "C:\NTPC-Chatbot"
pause
