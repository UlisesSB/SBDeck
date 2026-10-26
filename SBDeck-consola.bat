@echo off
rem Igual que SBDeck.bat pero mostrando los mensajes, para cuando algo no anda.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Primero abri SBDeck.bat para que se instale.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" "sbdeck.py"
pause
