#!/usr/bin/env python3
"""Erzeugt docs/adressplan.svg aus dem Adressplan.

Die Grafik zeigt, welche Teile des Geraetespeichers atprog auswertet, welche es
nur sichert und welche noch unbestimmt sind. Sie wird aus der Layoutdatei
erzeugt, veraltet also nicht.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from atprog import layout as layout_mod                                  # noqa: E402

FARBEN = {
    "ausgewertet": ("#2f7d4f", "#d7ece0"),
    "gesichert":   ("#9a6b00", "#f4e7c9"),
    "teils":       ("#8a5a00", "#f4e7c9"),
    "unbekannt":   ("#8a8f98", "#e6e8ec"),
}
BREITE, RAND, ZEILE = 900, 24, 30


def balken(x, y, w, h, art, titel, rechts=""):
    rahmen, fuell = FARBEN.get(art, FARBEN["unbekannt"])
    return (
        '<rect x="%.1f" y="%d" width="%.1f" height="%d" rx="3" fill="%s" stroke="%s"/>'
        '<text x="%.1f" y="%d" font-size="12" fill="#15181d">%s</text>'
        '<text x="%.1f" y="%d" font-size="11" fill="#666e7a" text-anchor="end">%s</text>'
        % (x, y, w, h, fuell, rahmen, x + 8, y + 17, titel, x + w - 8, y + 17, rechts))


def erzeuge(lay) -> str:
    teile = []
    y = RAND + 46
    nutz = BREITE - 2 * RAND

    teile.append('<text x="%d" y="%d" font-size="17" font-weight="600" fill="#15181d">'
                 'Adressplan %s &#183; Revision %d</text>'
                 % (RAND, RAND + 10, lay.title, lay.revision))
    teile.append('<text x="%d" y="%d" font-size="12" fill="#666e7a">'
                 'geprueft an %s</text>' % (RAND, RAND + 30, lay.verified_on))

    teile.append('<text x="%d" y="%d" font-size="13" font-weight="600" fill="#15181d">'
                 'Der Adressraum (flaechentreu)</text>' % (RAND, y))
    y += 14
    gesamt = sum(int(b["size"], 0) for b in lay.bereiche_gross)
    x = RAND
    for b in lay.bereiche_gross:
        size = int(b["size"], 0)
        w = max(26.0, nutz * size / gesamt)
        teile.append(balken(x, y, w, 26, b.get("einstufung", "unbekannt"), ""))
        x += w + 2
    y += 34
    for b in lay.bereiche_gross:
        teile.append('<text x="%d" y="%d" font-size="11" fill="#15181d">'
                     '%s &#8212; 0x%08X, %.1f MB</text>'
                     % (RAND + 10, y, b["name"], int(b["addr"], 0),
                        int(b["size"], 0) / 1048576.0))
        y += 15
    y += 16

    teile.append('<text x="%d" y="%d" font-size="13" font-weight="600" fill="#15181d">'
                 'Datensaetze, die atprog decodiert</text>' % (RAND, y))
    y += 10
    for key, o in sorted(lay.objects.items(), key=lambda kv: kv[1].base):
        felder = ", ".join(f.name for f in o.fields[:5])
        teile.append(balken(RAND, y, nutz, 24, "ausgewertet",
                            "0x%08X  %s (%d &#215; %d B)" % (o.base, key, o.count, o.record_size),
                            felder))
        y += ZEILE - 4
    y += 16

    teile.append('<text x="%d" y="%d" font-size="13" font-weight="600" fill="#15181d">'
                 'Bereiche ohne eigene Datensaetze</text>' % (RAND, y))
    y += 10
    for name, r in sorted(lay.regions.items(), key=lambda kv: kv[1].addr):
        teile.append(balken(RAND, y, nutz, 24, r.einstufung,
                            "0x%08X  %s" % (r.addr, name), r.zweck or r.desc))
        y += ZEILE - 4
    y += 16

    if lay.unzugeordnet:
        teile.append('<text x="%d" y="%d" font-size="13" font-weight="600" fill="#15181d">'
                     'Noch nicht zugeordnet</text>' % (RAND, y))
        y += 10
        for u in lay.unzugeordnet:
            teile.append(balken(RAND, y, nutz, 24, "unbekannt",
                                "0x%08X  %.0f KB" % (int(u["addr"], 0),
                                                     int(u["size"], 0) / 1024.0),
                                u["befund"][:78]))
            y += ZEILE - 4
    y += 24

    for i, (art, text) in enumerate((("ausgewertet", "ausgewertet und bearbeitbar"),
                                     ("gesichert", "gesichert, Inhalt nicht gedeutet"),
                                     ("unbekannt", "noch nicht zugeordnet"))):
        rahmen, fuell = FARBEN[art]
        teile.append('<rect x="%d" y="%d" width="14" height="14" rx="2" fill="%s" stroke="%s"/>'
                     '<text x="%d" y="%d" font-size="11" fill="#666e7a">%s</text>'
                     % (RAND + i * 240, y, fuell, rahmen, RAND + i * 240 + 20, y + 12, text))
    hoehe = y + 40

    return ('<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
            'viewBox="0 0 %d %d" font-family="-apple-system,Segoe UI,Roboto,sans-serif">'
            '<rect width="%d" height="%d" fill="#ffffff"/>%s</svg>'
            % (BREITE, hoehe, BREITE, hoehe, BREITE, hoehe, "".join(teile)))


def main() -> int:
    lay = layout_mod.load()
    ziel = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "docs", "adressplan.svg")
    with open(ziel, "w", encoding="utf-8") as fh:
        fh.write(erzeuge(lay))
    print("geschrieben: %s (%d Byte)" % (ziel, os.path.getsize(ziel)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
