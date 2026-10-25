@echo off
setlocal
cd /d "%~dp0"
title Steam Workshop Downloader

where py >nul 2>nul && (set "PY=py -3") || (set "PY=python")

%PY% -c "import fastapi, uvicorn, requests" >nul 2>nul
if errorlevel 1 (
    echo Instalando dependencias por primera vez...
    %PY% -m pip install --quiet --disable-pip-version-check -r requirements.txt
    if errorlevel 1 (
        echo.
        echo No pude instalar las dependencias. Comprueba que Python esta instalado.
        pause
        exit /b 1
    )
)

%PY% app.py
pause
