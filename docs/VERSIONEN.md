# Versionen und Zählweisen

In diesem Projekt kommen drei Zahlen vor, die nichts miteinander zu tun haben.
Damit sie nicht verwechselt werden, heißen sie verschieden und zählen anders.

| Bezeichnung | Beispiel | Wer zählt | Schritte |
|---|---|---|---|
| **Programmversion** | `atprog 1.00` | dieses Projekt | Hundertstel: 1.00 → 1.01 → 1.02 |
| **Gerätefirmware** | `4.00`, Kennung `V101` | AnyTone | nicht beeinflussbar |
| **Adressplan-Revision** | `r10` vom 2026-09-18 | dieses Projekt | ganze Zahlen: r10 → r11 |

## Programmversion

`atprog/version.py` führt `__version__`. Die Zählung beginnt bei **1.00** — dem
ersten Stand, mit dem Lesen, Auswerten, Bearbeiten und Schreiben an echter
Hardware nachgewiesen sind. Jede weitere Iteration erhöht um 0,01.

Anzeigen: `python3 run.py --version`

## Gerätefirmware

Das Funkgerät meldet beim Verbinden eine Kennung, im Prüfgerät `ID878UV2` mit
`V101`; die CPS nennt denselben Stand `4.00`. Diese Zahl steht im Abbild als
Metadatum und wird bei jedem Verbinden ausgegeben — wichtig, weil der
Adressplan firmwareabhängig sein kann.

Anzeigen: `python3 run.py info`

## Adressplan-Revision

Die Datei `atprog/layouts/d878uv2plus.json` beschreibt, welche Adresse im Gerät
was enthält. Sie trägt `plan_revision` und `plan_revised`. Die Revision steigt,
sobald sich an dieser Beschreibung etwas ändert — unabhängig davon, ob sich am
Programm etwas ändert.

Anzeigen: `python3 run.py map`

## Dateiformat

Abbilddateien (`.atbin`) beginnen mit der Marke `ATPROG\x04\x00`. Das ist eine
**Formatkennung**, keine Programmversion; sie bleibt unverändert, damit ältere
Abbilder lesbar bleiben.
