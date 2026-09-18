@echo off
rem Startet atprog unter Windows per Doppelklick. Prueft, ob Python da ist,
rem richtet beim ersten Mal pyserial ein und oeffnet dann die Oberflaeche.
title atprog
cd /d "%~dp0"

rem Python finden: erst den Windows-Starter "py", sonst "python".
set "PY=py -3"
%PY% --version >nul 2>&1
if errorlevel 1 set "PY=python"
%PY% --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo Python ist noch nicht installiert. Es oeffnet sich jetzt die
    echo Download-Seite www.python.org/downloads - dort den gelben Knopf klicken.
    echo.
    echo WICHTIG: Beim Installieren unten das Haekchen
    echo    "Add python.exe to PATH"
    echo setzen! Danach diese Datei einfach noch einmal doppelklicken.
    echo.
    start "" https://www.python.org/downloads/
    pause
    exit /b 1
)

rem pyserial wird unter Windows fuer die USB-Verbindung gebraucht;
rem einmalige Einrichtung beim ersten Start.
%PY% -c "import serial" >nul 2>&1
if errorlevel 1 (
    echo Richte einmalig die USB-Unterstuetzung ein ...
    %PY% -m pip install --quiet pyserial
)

echo Starte atprog - dieses Fenster bitte offen lassen.
echo Die Oberflaeche oeffnet sich gleich im Browser ^(http://127.0.0.1:8787^).
%PY% run.py gui
pause
