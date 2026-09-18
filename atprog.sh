#!/bin/sh
# Startet atprog ohne Installation aus dem Projektverzeichnis.
DIR=$(cd "$(dirname "$0")" && pwd)
exec python3 "$DIR/run.py" "$@"
