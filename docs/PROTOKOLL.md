# Programmierprotokoll der AT-D868/878-Familie

Kurzreferenz zu dem, was `atprog/protocol.py` implementiert. Die serielle
Schnittstelle ist ein USB-CDC-Port; die Baudrate ist ohne Bedeutung
(115200 8N1 wird gesetzt).

## Ablauf

| Richtung | Bytes | Bedeutung |
|---|---|---|
| PC → | `PROGRAM` | Programmiermodus anfordern |
| → PC | `QX` `0x06` | bestätigt |
| PC → | `0x02` | Kennung abfragen |
| → PC | 16 Byte | Modell (7 Byte) + Version, endet mit `0x06` |
| PC → | `0x06` | Quittung (manche Firmwares antworten nicht) |
| PC → | `R` addr(4, big endian) len(1) | Block lesen, len ≤ 64 |
| → PC | `W` addr(4) len(1) daten cksum(1) `0x06` | Antwort |
| PC → | `W` addr(4) len(1) daten cksum(1) `0x06` | Block schreiben |
| → PC | `0x06` | angenommen (`0x15` = abgelehnt) |
| PC → | `END` | Programmiermodus verlassen, Gerät startet neu |
| → PC | `0x06` | bestätigt |

## Prüfsumme

Summe aller Bytes ab der Adresse bis einschließlich der Nutzdaten, modulo 256.
Das Kommandozeichen (`R`/`W`) zählt nicht mit.

```python
checksum = sum(struct.pack(">IB", addr, length) + data) & 0xFF
```

## Schreiben: warum es lange nicht funktionierte

**Das Gerät sammelt Schreibzugriffe und übernimmt sie erst beim `END`.** Wer in
derselben Sitzung zurückliest, bekommt den alten Flash-Inhalt — und verwirft
dabei den Puffer. Genau das tat atprog anfangs bei jeder Prüfung, weshalb kein
einziger Schreibvorgang wirksam wurde und ganze Bereiche gelöscht zurückblieben.

Der richtige Ablauf sind **drei getrennte Sitzungen**:

1. Vergleichsstand lesen, `END`
2. nur schreiben, kein einziges Zwischenlesen, `END`
3. Neustart abwarten, neu verbinden, prüfen

`Radio.reopen()` erledigt Schritt 2 → 3: `END` senden, gut zwei Sekunden warten,
Port neu öffnen, Handschlag wiederholen.

### Die Messreihe, die dahin geführt hat

An einem AT-D878UV II Plus (Firmware V101) geprüft:

| Vermutung | Prüfung | Ergebnis |
|---|---|---|
| Blockgröße zu groß | 16 / 32 / 64 Byte | nur 16 Byte werden quittiert |
| Angebrochene Blöcke | 17 Byte ab ungerader Adresse | Restkommando bleibt ohne Antwort — **Fehler in atprog, behoben** |
| Zu wenig am Stück | 256 Byte, 16 KB, 128 KB | kein Unterschied |
| Überzähliges ACK nach der Kennung | entfernt (wie qdmr) | **Fehler in atprog, behoben** |
| Zu schnell geschrieben | 0 / 5 / 20 / 50 ms Pause | kein Unterschied |
| Adresse nicht beschreibbar | fünf bekannte Ziele | keine nahm Daten an |
| **Rücklesen in der Schreibsitzung** | schreiben, `END`, neu verbinden, dann prüfen | **alle Ziele übernommen** |

Das Kommandoformat ist Byte für Byte identisch mit qdmr
(`lib/anytone_interface.cc`) und dmrconfig (`serial.c`, `serial_write_region`):
`W`, Adresse big endian, Länge `0x10`, 16 Byte Daten, Prüfsumme, `0x06`;
erwartet wird ein einzelnes `0x06`. Weder qdmr noch dmrconfig senden vorher ein
Freischalt- oder Löschkommando — und beide lesen während einer Schreibsitzung
nicht zurück.

### Was beim Schreiben sonst noch zählt

* **Vollständige 16-Byte-Blöcke.** Angebrochene Blöcke quittiert das Gerät nicht
  einmal. atprog liest die Randblöcke, mischt die neuen Daten hinein und
  schreibt sie komplett zurück.
* **Kontakt-Indextabelle.** Ohne die Tabelle bei `0x02600000` zeigt das Gerät
  nach einem Schreibvorgang nur einen einzigen Kontakt. atprog berechnet sie vor
  jedem Zurückschreiben neu (`rebuild_contact_index`).
* **Zonen-Sichtbarkeit.** Fehlt `0x024C1360`, stehen nach einem Zurücksetzen
  alle Zonen auf „versteckt" und lassen sich am Gerät nicht anwählen.

## Blockgrößen

Lesen und Schreiben vertragen **nicht** dieselbe Blockgröße. Am AT-D878UV II
Plus (Firmware V101) gemessen:

| | angenommen |
|---|---|
| Lesen (`R`) | bis 64 Byte |
| Schreiben (`W`) | **nur 16 Byte** — 32 und 64 werden nicht quittiert |

Größere Schreibblöcke bleiben ohne jede Antwort (Zeitüberschreitung), nicht
etwa mit einer Fehlermeldung. atprog schreibt deshalb in 16-Byte-Blöcken
(`--write-block`) und liest in 64er-Blöcken. Prüfen lässt sich das gefahrlos
mit `atprog writetest`: liest einen Block und schreibt ihn unverändert zurück.

An echter Hardware gemessen: rund **225 Kommandos je Sekunde** (1,65 MB lesen
in etwa zwei Minuten). Das ergibt ~14 kB/s beim Lesen und ~3,5 kB/s beim
Schreiben. atprog misst die Rate vor größeren Schreibvorgängen selbst und
rechnet die Restzeit daraus hoch, statt sie zu schätzen.

## Fehlerbehandlung in atprog

* Jedes Kommando wird bis zu dreimal wiederholt; davor wird der Eingangspuffer
  geleert (Resynchronisation).
* Adresse und Prüfsumme der Antwort werden geprüft, ebenso die abschließende
  Quittung.
* Zeitüberschreitungen sind pro Kommando konfigurierbar (`--timeout`).

## Adressplan

![Adressplan](adressplan.svg)

Die Grafik entsteht aus der Layoutdatei (`python3 tools/adressplan_svg.py`) und
veraltet daher nicht. Grün heißt ausgewertet und bearbeitbar, gelb nur
gesichert, grau noch nicht zugeordnet. Dieselbe Übersicht gibt es im Terminal
mit `python3 run.py map`.

**Zur Benennung**, weil drei verschiedene Zahlen im Umlauf sind:

| Bezeichnung | Beispiel | Was es ist |
|---|---|---|
| Programmversion | atprog 1.00 | diese Software; Iterationen in Hundertstel-Schritten |
| Gerätefirmware | 4.00, Kennung `V101` | was im Funkgerät läuft |
| Adressplan-Revision | r10 vom 2026-09-18 | diese Beschreibung des Speichers |

Geprüft an einem echten AT-D878UV II Plus (Kennung `ID878UV2`); alle
Datensätze dieses Geräts lassen sich byteidentisch rekonstruieren.
Siehe `atprog/layouts/d878uv2plus.json` und `python3 run.py layout`.

| Adresse | Inhalt |
|---|---|
| `0x00800000` + Bank·`0x40000` | Kanäle, 128 je Bank, 64 Byte je Datensatz |
| `0x01000000` | Zonen-Kanallisten, 512 Byte je Zone, `uint16`-Indizes |
| `0x01080000` | Scanlisten, 512 Byte je Liste (Name ab `0x0F`, Mitglieder ab `0x20`) |
| `0x024C1300` | Bitmap Zonen |
| `0x024C1320` | Bitmap Radio-IDs |
| `0x024C1340` | Bitmap Scanlisten |
| `0x024C1360` | Sichtbarkeit der Zonen: gesetztes Bit = versteckt |
| `0x024C1500` | Bitmap Kanäle (4000 Bit) |
| `0x02600000` | Kontakt-Index: je Kontakt ein `uint32`, nach ID sortiert |
| `0x02500000` | Grundeinstellungen |
| `0x02501000` | APRS-Einstellungen (256 Byte) |
| `0x02501200` | APRS-Bakennachricht (64 Byte) |
| `0x02501400` | Erweiterungen, u. a. digitales APRS |
| `0x02501800` | APRS-Empfangsfilter |
| `0x02540000` | Zonennamen, 32 Byte je Zone |
| `0x02580000` | Radio-IDs: ID als BCD, Name ab Offset 5 |
| `0x025C0000` | Statusmeldungen |
| `0x02940000` | DTMF-/Schnellwahlkontakte (gesichert, nicht ausgewertet) |
| `0x02980000` | RX-Gruppenlisten, 512 Byte je Liste |
| `0x02640000` | Bitmap Kontakte — **invertiert**: gelöschtes Bit = belegt |
| `0x02680000` | Kontakte, 100 Byte je Datensatz |

Die Bitmaps entscheiden, welche Datensätze belegt sind. `atprog` liest deshalb
standardmäßig nur die belegten Einträge (`read` ohne `--full`).

### Kanaldatensatz (64 Byte)

| Offset | Inhalt |
|---|---|
| `0x00` | Empfangsfrequenz, **BCD**, 10-Hz-Schritte (`43 91 00 00` = 439,100 MHz) |
| `0x04` | Ablage, BCD, 10-Hz-Schritte (bei Simplex ohne Bedeutung) |
| `0x08` | Bit 0–1 Betriebsart, 2–3 Leistung, 4 Bandbreite, 6–7 Ablagerichtung |
| `0x09` | Bit 0–1 Art der RX-Tonauswertung, 2–3 Art der TX-Tonaussendung |
| `0x0A` / `0x0B` | Index in die CTCSS-/DCS-Tabelle für TX bzw. RX (0 = 62,5 Hz) |
| `0x10` | eigener CTCSS-Ton in 0,1 Hz (`uint16`) |
| `0x14` | Kontaktverweis (`uint16`-Index in die Kontaktliste) |
| `0x1B` | Scanliste (`0xFF` = keine) |
| `0x1C` | RX-Gruppenliste (`0xFF` = keine) |
| `0x20` | Color Code |
| `0x21` | Bit 0 Zeitschlitz (0 = TS1, 1 = TS2) |
| `0x23` | Kanalname, 16 Byte |

### Besonderheiten, die beim Schreiben zählen

* **Namensreste:** Die Original-CPS schreibt Name + Endezeichen und lässt den
  Rest des Feldes stehen (echte Datensätze enthalten z. B. `OV LEV\0 VFO B`).
  `atprog` verfährt genauso — sonst wäre kein Datensatz byteidentisch.
* **Bedeutungslose Altbytes:** Ist die Tonsignalisierung aus, steht im
  Indexbyte oft noch ein alter Wert. Er bleibt unangetastet.
* **Flash-Marken:** Im Kontaktbereich steht am Ende jedes `0x20000`-Blocks die
  Marke `55 55 AA AA`. Datensätze, die darauf liegen, sind keine Kontakte.

### RX-Gruppenliste (512 Byte)

| Offset | Inhalt |
|---|---|
| `0x000` | bis zu 64 Kontaktindizes als `uint32`; `0xFFFFFFFF` beendet die Liste |
| `0x100` | Name der Liste, 16 Byte |

### Spiegelung des Adressraums

Der Speicher erscheint alle `0x20000` gespiegelt: `0x02540000` und `0x02560000`
liefern dieselben Zonennamen, `0x02980000` und `0x029A0000` dieselben
Gruppenlisten. Maßgeblich ist jeweils die niedrigere Adresse.

### DTMF-Kontakt (`0x02940000`)

| Offset | Inhalt |
|---|---|
| `0x00` | Rufnummer, je Nibble eine Ziffer (`12 34 50` = „12345") |
| `0x07` | Anzahl der Ziffern |
| `0x08` | Name, 16 Byte |

Der Abstand zwischen zwei Datensätzen ist unbestimmt: im Prüfgerät existierte
nur der Werkseintrag `Contact1`. Der Bereich wird gesichert, aber nicht
ausgewertet.

## Digitale Kontaktliste (DMR-ID-Datenbank)

Die Datenbank, aus der das Display beim Empfang Rufzeichen und Namen anzeigt,
liegt **außerhalb des Codeplugs**. Sichern und Zurückschreiben des Codeplugs
lassen sie unangetastet; sie wird nur von den `userdb`-Befehlen angefasst.

**Achtung:** Der AT-D878UV II Plus legt die Datenbank *anders* ab als der
ältere D868UV, den qdmr beschreibt. atprog kennt beide und erkennt am Gerät
selbst, welche zutrifft (`GEOMETRIES` in `atprog/userdb.py`).

| Bereich | AT-D878UV II Plus (gemessen) | D868UV (qdmr) |
|---|---|---|
| Index | `0x04000000`, Bänke à `0x1F400`, Abstand `0x40000` | gleich |
| Kopf | **`0x04840000`** | `0x044C0000` |
| Datensätze | **`0x05500000`**, Bänke à `0x186A0`, Abstand `0x40000` | `0x04500000` |

Gegenprobe am Prüfgerät: Der Kopf nennt 298 897 Einträge und als Ende
`0x07F547C1`. Das Bankschema bildet den Stromoffset 16 983 905 auf genau diese
Adresse ab (169 volle Bänke + 83 905 Byte) — das sind 56,8 Byte je Eintrag und
deckt sich mit den gemessenen Satzlängen. Acht über die gesamte Datenbank
verteilte Stichproben ließen sich fehlerfrei decodieren.

Beim Schreiben löscht atprog überzählige Indexeinträge einer größeren
Vorgängerliste und liest anschließend Kopf und Stichproben zurück.

**Indexeintrag (8 Byte), aufsteigend nach ID sortiert:**

| Offset | Inhalt |
|---|---|
| `0x00` | `uint32` = (ID als BCD) « 1, Bit 0 = Gruppenruf |
| `0x04` | `uint32` Byte-Offset des Datensatzes im Datenstrom |

Die Kodierung des Schlüssels wurde an einem echten Gerät bestätigt: alle 2047
geprüften Einträge ergeben nach `>> 1` gültiges BCD.

**Datensatz:** 6 Byte Kopf — Ruftyp (`0x00`), ID (`0x01`, 4 Byte **BCD in
Big-Endian-Folge**), Flags (`0x05`) — danach sechs mit `0x00` abgeschlossene
Zeichenketten in dieser Reihenfolge: Name (16), Ort (15), Rufzeichen (8),
Region (16), Land (16), Bemerkung.

Erster Datensatz des Prüfgeräts bei `0x05500000`:

```
00 | 00 02 34 01 | 00 | "Bradley Brown" 00 "Bristol" 00 "M0JXR" 00 …
Typ   ID = 23401  Flags
```

Die ID `00 02 34 01` entspricht genau dem ersten Indexschlüssel. atprog prüft
das beim Verbinden selbst nach und erkennt daran auch eine abweichende
Kodierung.

Adressen und Bankgrößen entsprechen der Beschreibung im qdmr-Projekt
(`lib/d868uv_callsigndb.hh`); der Index wurde zusätzlich an eigener Hardware
nachgemessen.

### APRS-Einstellungen (`0x02501000`, an eigener Hardware geprüft)

| Offset | Inhalt |
|---|---|
| `0x01` | Sendefrequenz der Bake, BCD, 10-Hz-Schritte |
| `0x05` | Vorlauf, 20-ms-Schritte |
| `0x0A` / `0x0B` | manuelles Intervall / Bakenabstand (30-s-Schritte) |
| `0x16` | Zielrufzeichen (6 Byte), `0x1C` dessen SSID |
| `0x1D` | eigenes Rufzeichen (6 Byte), `0x23` dessen SSID |
| `0x24` | Pfad, z. B. `WIDE1-1WIDE2-2` |
| `0x39` / `0x3A` | Symboltabelle und Symbol |
| `0x3B` | Sendeleistung |

Die Bakennachricht steht als Text bei `0x02501200`.

**Achtung:** Sicherungen, die mit Layout älter als 4.4 erstellt wurden, enthalten
diesen Bereich nicht — damals wurden bei `0x02500000` nur 1024 Byte gelesen.

### Noch offen

* `0x02600000`, `0x02480000`, `0x024E0000` — belegt, Inhalt unbestimmt.
* Grundeinstellungen (`0x02500000`) werden gesichert, aber nicht ausgewertet.

Unbekannte Bereiche findet man mit:

```bash
python3 run.py explore --start 0x02400000 --stop 0x02A00000 --step 0x800
python3 run.py peek 0x02940000 --size 0x1000 -o bereich.atbin
```
