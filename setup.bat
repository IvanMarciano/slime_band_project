@echo off
rem Instalacion de Slime Band en Windows. Ejecutar una sola vez (doble clic).
cd /d "%~dp0"

where py >nul 2>nul
if errorlevel 1 (
    echo No se encontro Python. Instala Python 3.12 desde https://www.python.org/downloads/
    echo y marca "Add python.exe to PATH" durante la instalacion.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo Creando entorno virtual con Python 3.12...
    py -3.12 -m venv .venv
    if errorlevel 1 (
        echo Hace falta Python 3.12 ^(3.13+ todavia no es compatible con shazamio^).
        echo Instalalo desde https://www.python.org/downloads/release/python-31210/
        pause
        exit /b 1
    )
)

echo Instalando dependencias...
".venv\Scripts\python.exe" -m pip install --upgrade pip -q
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo Fallo la instalacion de dependencias.
    pause
    exit /b 1
)

if not exist ".env" (
    copy ".env.example" ".env" >nul
    echo.
    echo Se creo el archivo .env: abrilo con el Bloc de notas y completa tus claves.
)

echo.
echo Listo. Abri Slime Band con "Slime Band.bat".
pause
