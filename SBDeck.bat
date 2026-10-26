@echo off
setlocal
cd /d "%~dp0"
title SBDeck

rem La primera vez instala todo. Las siguientes, solo abre SBDeck.
if not exist ".venv\Scripts\pythonw.exe" goto install
if not exist ".venv\req.txt" goto install
fc /b requirements.txt .venv\req.txt >nul 2>nul || goto install
goto run

:install
echo.
echo  SBDeck - preparando todo. Solo pasa la primera vez y tarda unos minutos.
echo.
set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if exist ".venv\Scripts\python.exe" goto deps

echo  [1/4] Buscando Python...
where py >nul 2>nul && py -3.12 -m venv .venv >nul 2>nul
if not exist ".venv\Scripts\python.exe" where py >nul 2>nul && py -3 -m venv .venv >nul 2>nul
if not exist ".venv\Scripts\python.exe" if exist "%PYEXE%" "%PYEXE%" -m venv .venv
if exist ".venv\Scripts\python.exe" goto deps
echo        No esta instalado. Lo instalo con winget...
winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements
if exist "%PYEXE%" "%PYEXE%" -m venv .venv
if exist ".venv\Scripts\python.exe" goto deps
echo.
echo  No pude instalar Python. Bajalo de python.org - version 3.12 - y volve a abrir SBDeck.bat
pause
exit /b 1

:deps
echo  [2/4] Instalando librerias...
".venv\Scripts\python.exe" -m pip install --upgrade pip -q
".venv\Scripts\python.exe" -m pip install -r requirements.txt -q
if errorlevel 1 (
  echo.
  echo  Hubo un error instalando las librerias. Revisa los mensajes de arriba.
  pause
  exit /b 1
)
copy /y requirements.txt .venv\req.txt >nul

echo  [3/4] Bajando MultiMonitorTool...
if not exist tools mkdir tools
if not exist tools\MultiMonitorTool.exe (
  curl -L -s -o tools\mmt.zip https://www.nirsoft.net/utils/multimonitortool-x64.zip
  tar -xf tools\mmt.zip -C tools >nul 2>nul
  del tools\mmt.zip >nul 2>nul
)
if not exist tools\MultiMonitorTool.exe echo        No se pudo bajar: el boton de monitores no va a andar hasta que lo pongas en la carpeta tools.

echo  [4/4] Abriendo SBDeck...
echo.
echo  Listo. SBDeck queda en la bandeja, al lado del reloj, y se abre solo cada vez que prendas la PC.
start "" ".venv\Scripts\pythonw.exe" "sbdeck.py"
timeout /t 6 >nul
exit /b 0

:run
start "" ".venv\Scripts\pythonw.exe" "sbdeck.py"
exit /b 0
