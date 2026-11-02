@echo off
rem Doble clic para abrir Slime Band. Agrega --fullscreen al final para modo cuadro.
cd /d "%~dp0"
".venv\Scripts\python.exe" main.py %*
if errorlevel 1 pause
