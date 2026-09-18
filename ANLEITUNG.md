# Anleitung für Einsteiger — herunterladen und starten

Diese Anleitung richtet sich an alle, die **keine Computer-Vorkenntnisse**
haben. Sie führt in vier Schritten vom Herunterladen bis zur laufenden
Oberfläche. Nichts davon kann Ihrem Funkgerät schaden — gefährlich ist nur
das Schreiben ins Gerät, und das verlangt immer eine ausdrückliche
Bestätigung.

## Was Sie brauchen

* einen Computer mit **Windows 10/11** oder einen **Mac**
* das Funkgerät **AnyTone AT-D878UV II Plus**
* das **USB-Programmierkabel** (wichtig: ein Kabel mit Datenadern —
  manche reine Ladekabel sehen gleich aus, übertragen aber keine Daten)

---

## Schritt 1: Programm herunterladen

1. Öffnen Sie im Browser die Seite
   <https://github.com/JensHouses/Anytone_programmer>
2. Klicken Sie auf den grünen Knopf **„Code“** und dann auf
   **„Download ZIP“**.
3. Die Datei landet in Ihrem Download-Ordner. Entpacken Sie sie:
   * **Windows:** Rechtsklick auf die ZIP-Datei → **„Alle extrahieren …“**
     → **„Extrahieren“**
   * **Mac:** Doppelklick auf die ZIP-Datei
4. Verschieben Sie den entpackten Ordner (er heißt
   `Anytone_programmer-master`) an einen Ort, an dem Sie ihn wiederfinden —
   zum Beispiel auf den Schreibtisch.

## Schritt 2: Python installieren (nur einmal nötig)

Das Programm braucht **Python** — eine kostenlose Grundausstattung, die
viele Programme nutzen. Falls sie schon installiert ist, überspringt der
Start in Schritt 3 diesen Punkt einfach.

1. Öffnen Sie <https://www.python.org/downloads/> und klicken Sie auf den
   gelben Download-Knopf.
2. Starten Sie die heruntergeladene Datei.
3. **Nur Windows, aber ganz wichtig:** Setzen Sie im ersten Fenster unten
   das Häkchen bei **„Add python.exe to PATH“**, bevor Sie auf
   „Install Now“ klicken. Ohne dieses Häkchen findet der Computer Python
   später nicht.
4. Klicken Sie sich durch die Installation („Install Now“ / „Installieren“,
   am Ende „Close“).

## Schritt 3: Programm starten

Öffnen Sie den Ordner aus Schritt 1 und machen Sie einen **Doppelklick**
auf die passende Startdatei:

| System | Datei |
|---|---|
| Windows | **`Start-Windows.bat`** |
| Mac | **`Start-macOS.command`** |

Was dann passiert:

* Es öffnet sich ein schwarzes Textfenster. **Lassen Sie es offen** — darin
  läuft das Programm. Beim allerersten Start unter Windows wird einmalig
  ein kleines Zusatzpaket für die USB-Verbindung eingerichtet; das dauert
  einen Moment.
* Kurz darauf öffnet sich Ihr Browser mit der Bedienoberfläche. Falls
  nicht: Browser öffnen und `http://127.0.0.1:8787` in die Adresszeile
  tippen.

**Nur beim ersten Start auf dem Mac:** macOS meldet eventuell, die Datei
stamme „von einem nicht verifizierten Entwickler“ und könne nicht geöffnet
werden. Das ist die normale Warnung für alles, was nicht aus dem App Store
kommt. Lösung: **Rechtsklick** (oder Klick mit gedrückter ctrl-Taste) auf
`Start-macOS.command` → **„Öffnen“** → im Fenster noch einmal **„Öffnen“**.
Ab dann genügt der Doppelklick. Fragt der Mac stattdessen nach der
Installation der „Befehlszeilen-Tools“, klicken Sie auf „Installieren“ und
starten danach noch einmal.

## Schritt 4: Die ersten Schritte im Programm

1. Funkgerät **einschalten** und mit dem USB-Kabel an den Computer
   anschließen.
2. In der Oberfläche links auf den Reiter **„Funkgerät“** klicken.
3. Bei „Serieller Port“ sollte ein Eintrag stehen (bei mehreren den mit
   „usbmodem“ bzw. „COM“ wählen). Dann auf **„Gerät erkennen“** klicken —
   erscheint Modell und Firmware, steht die Verbindung.
4. Auf **„Aus Gerät lesen“** klicken. Das dauert etwa zwei Minuten und
   erzeugt automatisch eine **Sicherungskopie** Ihres Geräts — machen Sie
   das immer zuerst. Danach füllen sich die Tabellen (Kanäle, Zonen …)
   und Sie können bearbeiten.

Zum Üben ohne Funkgerät gibt es auf dem Reiter „Funkgerät“ einen
**Simulator** (Knopf „Starten“) — ein virtuelles Gerät, an dem nichts
kaputtgehen kann. Der Simulator funktioniert auf dem Mac und unter Linux,
nicht unter Windows.

## Programm beenden

Browserfenster schließen und das schwarze Textfenster schließen. Fertig.
Beim nächsten Mal reicht wieder der Doppelklick auf die Startdatei —
Schritt 1 und 2 sind nie wieder nötig.

---

## Wenn etwas nicht klappt

| Problem | Lösung |
|---|---|
| Windows: „Python ist nicht installiert“ oder das Fenster schließt sofort | Python wie in Schritt 2 installieren. Wenn es schon installiert ist, wurde vermutlich das Häkchen **„Add python.exe to PATH“** vergessen — Python einfach noch einmal installieren, diesmal mit Häkchen. |
| Mac: „kann nicht geöffnet werden, da es von einem nicht verifizierten Entwickler stammt“ | Rechtsklick auf `Start-macOS.command` → „Öffnen“ → „Öffnen“ (nur beim ersten Mal nötig). |
| Der Browser öffnet sich nicht | Browser selbst öffnen und `http://127.0.0.1:8787` eingeben. |
| „(kein Port gefunden)“ obwohl das Gerät angeschlossen ist | Ist das Gerät eingeschaltet? Steckt das Kabel fest? Häufigste Ursache: ein reines **Ladekabel** ohne Datenadern — anderes USB-Kabel probieren. Danach in der Oberfläche „Ports neu suchen“ klicken. |
| Windows meldet beim ersten Start eine blaue Warnung „Der Computer wurde durch Windows geschützt“ | Auf **„Weitere Informationen“** und dann **„Trotzdem ausführen“** klicken — auch das ist die übliche Warnung für Programme außerhalb des Microsoft Store. |

Für alles Weitere (Bedienung, Kommandozeile, technische Details) siehe die
[README](README.md).
