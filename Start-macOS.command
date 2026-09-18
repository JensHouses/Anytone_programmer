#!/bin/sh
# Startet atprog auf dem Mac per Doppelklick.
cd "$(dirname "$0")" || exit 1

if ! command -v python3 >/dev/null 2>&1; then
    echo
    echo "Python ist noch nicht installiert."
    echo "Bitte von https://www.python.org/downloads/ laden und installieren,"
    echo "danach diese Datei noch einmal doppelklicken."
    echo
    read -r _
    exit 1
fi

echo "Starte atprog - dieses Fenster bitte offen lassen."
echo "Die Oberflaeche oeffnet sich gleich im Browser (http://127.0.0.1:8787)."
python3 run.py gui
