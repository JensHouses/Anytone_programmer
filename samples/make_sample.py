#!/usr/bin/env python3
"""Erzeugt ein Beispielprojekt und den dazugehoerigen CSV-Satz."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from atprog.csvio import export_all
from atprog.models import (Channel, Codeplug, RadioID, RxGroupList, ScanList,
                           TalkGroup, Zone, parse_freq)

HERE = os.path.dirname(os.path.abspath(__file__))


def build() -> Codeplug:
    cp = Codeplug(name="Beispiel DL")
    cp.radio_ids.append(RadioID(name="DL0EXAMPLE", radio_id=2621234))

    for name, tg in [("Ortsrunde", 8), ("Lokal 9", 9), ("Welt", 91), ("Europa", 92),
                     ("DL", 262), ("DL Sued", 26212), ("Notfall", 2621)]:
        cp.talkgroups.append(TalkGroup(name=name, tg_id=tg))

    cp.rxgroups.append(RxGroupList(name="DL Standard",
                                   contacts=["Ortsrunde", "Lokal 9", "DL", "Welt"]))

    def dmr(name, rx, offset, tg, slot, cc=1):
        return Channel(name=name, rx_freq=parse_freq(rx), tx_freq=parse_freq(rx) + offset,
                       ch_type="D-Digital", power="High", bandwidth="12.5K",
                       contact=tg, contact_id=cp.talkgroup_by_name(tg).tg_id,
                       radio_id="DL0EXAMPLE", color_code=cc, slot=slot,
                       rx_group="DL Standard", scan_list="Heimat", tx_permit="ChannelFree")

    def fm(name, rx, offset, tone="Off"):
        return Channel(name=name, rx_freq=parse_freq(rx), tx_freq=parse_freq(rx) + offset,
                       ch_type="A-Analog", power="High", bandwidth="12.5K",
                       ctcss_encode=tone, ctcss_decode="Off",
                       squelch_mode="CTCSS/DCS" if tone != "Off" else "Carrier",
                       scan_list="Heimat")

    cp.channels += [
        dmr("DB0XYZ TS1 DL", "438.5000", -7_600_000, "DL", 1),
        dmr("DB0XYZ TS2 Ort", "438.5000", -7_600_000, "Ortsrunde", 2),
        dmr("DB0ABC TS1 Welt", "439.4875", -7_600_000, "Welt", 1),
        dmr("DB0ABC TS2 Lokal", "439.4875", -7_600_000, "Lokal 9", 2),
        dmr("Simplex S0", "433.4500", 0, "Lokal 9", 1),
        fm("DB0FM 70cm", "438.7000", -7_600_000, "88.5"),
        fm("DB0FM 2m", "145.6125", -600_000, "67.0"),
        fm("S20 Simplex", "145.3125", 0),
        fm("Hochrhein Relais", "145.7375", -600_000, "123.0"),
    ]

    heimat = [c.name for c in cp.channels[:6]]
    cp.zones.append(Zone(name="Heimat", channels=heimat,
                         a_channel=heimat[0], b_channel=heimat[5]))
    cp.zones.append(Zone(name="Simplex", channels=["Simplex S0", "S20 Simplex"],
                         a_channel="Simplex S0", b_channel="S20 Simplex"))
    cp.scanlists.append(ScanList(name="Heimat", channels=heimat))
    return cp


def main() -> int:
    cp = build()
    project = os.path.join(HERE, "beispiel-projekt.json")
    cp.save(project)
    files = export_all(os.path.join(HERE, "csv"), cp)
    issues = cp.validate()
    print("Projekt:", project)
    print("CSV    :", ", ".join(os.path.basename(f) for f in files))
    print("Pruefung: %d Fehler, %d Warnungen"
          % (sum(1 for i in issues if i.level == "error"),
             sum(1 for i in issues if i.level == "warning")))
    for issue in issues:
        print("  ", issue)
    return 0


if __name__ == "__main__":
    sys.exit(main())
