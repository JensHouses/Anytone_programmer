"""Tests der Rufzeichendatenbank (DMR-ID-Liste)."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from atprog.image import MemoryImage
from atprog.userdb import (ENTRY_BANK_SIZE, GEOMETRIES, INDEX_BANK_SIZE, LIMITS_ADDR,
                           UserDatabase,
                           UserEntry, bcd_from_int, count_by_index, decode_entry,
                           detect_id_encoding, encode_entry, entry_address, index_address,
                           index_key, int_from_bcd, key_to_id, read_limits, read_user_csv,
                           write_user_csv, fetch_user_database, iter_json_users,
                           read_user_file)


class Keys(unittest.TestCase):
    def test_bcd(self):
        self.assertEqual(bcd_from_int(2635224), 0x02635224)
        self.assertEqual(int_from_bcd(0x02635224), 2635224)
        self.assertIsNone(int_from_bcd(0x0263522F))

    def test_index_key_matches_device(self):
        # Kodierung wie im Geraet: BCD-ID um ein Bit nach links, Bit 0 = Gruppenruf
        self.assertEqual(index_key(2635224), 0x02635224 << 1)
        self.assertEqual(index_key(2635224, group=True), (0x02635224 << 1) | 1)
        self.assertEqual(key_to_id(index_key(2635224)), 2635224)
        self.assertEqual(key_to_id(index_key(2635224, group=True)), 2635224)

    def test_keys_are_sorted_like_ids(self):
        ids = [23401, 262999, 2620000, 9999999]
        keys = [index_key(i) for i in ids]
        self.assertEqual(keys, sorted(keys))


class Addressing(unittest.TestCase):
    def test_index_banks(self):
        self.assertEqual(index_address(0), 0x04000000)
        self.assertEqual(index_address(INDEX_BANK_SIZE // 8 - 1),
                         0x04000000 + INDEX_BANK_SIZE - 8)
        self.assertEqual(index_address(INDEX_BANK_SIZE // 8), 0x04040000)

    def test_entry_banks(self):
        # Vorgabe ist die am AT-D878UV II Plus gemessene Ablage
        self.assertEqual(entry_address(0), 0x05500000)
        self.assertEqual(entry_address(ENTRY_BANK_SIZE - 1), 0x05500000 + ENTRY_BANK_SIZE - 1)
        self.assertEqual(entry_address(ENTRY_BANK_SIZE), 0x05540000)

    def test_measured_end_of_database(self):
        """Gegenprobe mit den Kopfdaten des Pruefgeraets.

        Der Kopf bei 0x04840000 nennt als Ende 0x07F547C1. Mit Basis
        0x05500000 und Baenken zu 100000 Byte im Abstand 0x40000 entspricht das
        einem Datenstrom von 16983905 Byte (169 volle Baenke + 83905 Byte) -
        bei 298897 Eintraegen also rund 57 Byte je Eintrag, was zu den
        gemessenen Satzlaengen passt.
        """
        self.assertEqual(entry_address(16983905), 0x07F547C1)

    def test_old_geometry_still_available(self):
        geo = GEOMETRIES["d868uv"]
        self.assertEqual(geo.entry_address(0), 0x04500000)
        self.assertEqual(geo.limits, 0x044C0000)


class Records(unittest.TestCase):
    def test_header_matches_real_device(self):
        """Kopfbytes wie am AT-D878UV II Plus gelesen.

        Erster Datensatz bei 0x05500000: Ruftyp 0, ID 23401 als BCD in
        Big-Endian-Folge, Flags 0, danach der Name.
        """
        real = bytes.fromhex("000002340100") + b"Bradley Brown\x00"
        entry, _ = decode_entry(real + b"\x00" * 5, 0, bcd_id=True)
        self.assertEqual(entry.id, 23401)
        self.assertEqual(entry.call_type, 0)
        self.assertEqual(entry.name, "Bradley Brown")
        mine = encode_entry(UserEntry(id=23401, name="Bradley Brown"), bcd_id=True)
        self.assertEqual(mine[:6], real[:6])

    def test_entry_roundtrip(self):
        entry = UserEntry(id=2635224, callsign="DA6JEY", name="Jens", city="Leverkusen",
                          state="NRW", country="Germany")
        blob = encode_entry(entry, bcd_id=True)
        self.assertEqual(blob[0], 0)                       # Einzelruf
        back, end = decode_entry(blob, 0, bcd_id=True)
        self.assertEqual(end, len(blob))
        for attr in ("id", "callsign", "name", "city", "state", "country"):
            self.assertEqual(getattr(back, attr), getattr(entry, attr))

    def test_long_fields_are_cut(self):
        entry = UserEntry(id=1, name="x" * 40, callsign="y" * 20, city="z" * 30)
        back, _ = decode_entry(encode_entry(entry, True), 0, True)
        self.assertEqual(len(back.name), 16)
        self.assertEqual(len(back.callsign), 8)
        self.assertEqual(len(back.city), 15)

    def test_encoding_detection(self):
        entry = UserEntry(id=2621234, callsign="DL1ABC")
        self.assertTrue(detect_id_encoding(encode_entry(entry, True), 2621234))
        self.assertFalse(detect_id_encoding(encode_entry(entry, False), 2621234))


class JsonList(unittest.TestCase):
    """Die Liste von radioid.net liegt als JSON vor."""

    def _write(self, payload):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "users.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        return path

    def _users(self):
        return [
            {"id": 2635224, "callsign": "DA6JEY", "fname": "Jens", "surname": "M",
             "city": "Leverkusen", "state": "NRW", "country": "Germany", "remarks": ""},
            {"id": 3124726, "callsign": "N7EWB", "fname": "Eric", "surname": "Barendt",
             "city": "Kirkland", "state": "WA", "country": "United States",
             "remarks": "note"},
            {"id": 2312013, "callsign": "OM3TKT", "fname": "Jozef", "surname": "Turi",
             "city": "Sturovo", "state": None, "country": "Slovakia", "remarks": None},
        ]

    def test_root_object(self):
        path = self._write({"users": self._users()})
        entries = list(iter_json_users(path))
        self.assertEqual([e.id for e in entries], [2635224, 3124726, 2312013])
        self.assertEqual(entries[0].name, "Jens M")
        self.assertEqual(entries[1].comment, "note")
        self.assertEqual(entries[2].state, "")          # null wird zu leer

    def test_root_array(self):
        path = self._write(self._users())
        self.assertEqual(len(list(iter_json_users(path))), 3)

    def test_braces_inside_strings(self):
        """Geschweifte Klammern in Namen duerfen den Leser nicht aus dem Takt bringen."""
        users = self._users()
        users[0]["city"] = "Bad {Ort} \\ Nord"
        path = self._write({"users": users})
        entries = list(iter_json_users(path))
        self.assertEqual(len(entries), 3)
        self.assertEqual(entries[0].city, "Bad {Ort} \\ Nord")

    def test_read_user_file_dispatch_and_filter(self):
        path = self._write({"users": self._users()})
        self.assertEqual(len(read_user_file(path)), 3)
        self.assertEqual([e.callsign for e in read_user_file(path, country="Germany")],
                         ["DA6JEY"])
        self.assertEqual([e.callsign for e in read_user_file(path, prefixes=["31"])],
                         ["N7EWB"])
        self.assertEqual(len(read_user_file(path, limit=2)), 2)

    def test_fetch_writes_file(self):
        """Der Abruf laedt in eine Datei - hier ueber eine lokale Quelle."""
        src = self._write({"users": self._users()})
        dest = os.path.join(tempfile.mkdtemp(), "geholt.json")
        size = fetch_user_database(dest, url="file://" + src)
        self.assertGreater(size, 0)
        self.assertEqual(os.path.getsize(dest), size)
        self.assertEqual(len(list(iter_json_users(dest))), 3)


class Database(unittest.TestCase):
    def _db(self, n=2500):
        entries = [UserEntry(id=2620000 + i * 3, callsign="DL%04d" % i, name="Name%d" % i,
                             city="Ort%d" % (i % 100), country="Germany") for i in range(n)]
        return UserDatabase(entries=entries)

    def test_build_and_read_back(self):
        db = self._db()
        img = db.build()
        count, end = read_limits(img)
        self.assertEqual(count, len(db.entries))
        self.assertGreater(end, 0x04500000)
        back = UserDatabase.from_image(img)
        self.assertEqual(len(back.entries), len(db.entries))
        self.assertEqual([e.id for e in back.entries], [e.id for e in db.entries])
        self.assertEqual(back.entries[7].callsign, db.entries[7].callsign)

    def test_entries_are_sorted_by_id(self):
        db = UserDatabase(entries=[UserEntry(id=9999999, callsign="Z"),
                                   UserEntry(id=1000, callsign="A"),
                                   UserEntry(id=262000, callsign="M")])
        back = UserDatabase.from_image(db.build())
        self.assertEqual([e.id for e in back.entries], [1000, 262000, 9999999])

    def test_spans_multiple_banks(self):
        # genug Eintraege, damit Index und Daten mehrere Baenke belegen
        db = self._db(n=18000)
        img = db.build()
        starts = {addr for addr, _ in img.ranges()}
        self.assertIn(0x04000000, starts)
        self.assertIn(0x04040000, starts)      # zweite Indexbank
        self.assertIn(0x05500000, starts)      # erste Datenbank
        back = UserDatabase.from_image(img, limit=18000)
        self.assertEqual(len(back.entries), 18000)
        self.assertEqual(back.entries[-1].id, db.entries[-1].id)

    def test_count_without_header(self):
        """Ist der Kopf geloescht, muss die Anzahl aus dem Index kommen.

        Genau dieser Fall trat an echter Hardware auf (Firmware V101): der
        Block bei 0x044C0000 stand auf 0xFFFFFFFF.
        """
        for n in (0, 1, 500, 16001):
            db = UserDatabase(entries=[UserEntry(id=1000 + i, callsign="X%d" % i)
                                       for i in range(n)])
            img = db.build()
            img.write(LIMITS_ADDR, b"\xff" * 8)
            read8 = lambda a: img.read(a, 8) if img.has(a, 8) else b""
            self.assertEqual(count_by_index(read8), n, "bei %d Eintraegen" % n)
            self.assertEqual(read_limits(img)[0], n)

    def test_csv_filters(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "user.csv")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("RADIO_ID,CALLSIGN,FIRST_NAME,LAST_NAME,CITY,STATE,COUNTRY\n")
            fh.write("2635224,DA6JEY,Jens,M,Leverkusen,NRW,Germany\n")
            fh.write("2631111,DL9XYZ,Anna,B,Bonn,NRW,Germany\n")
            fh.write("3101234,K1XYZ,John,D,Boston,MA,United States\n")
        self.assertEqual(len(read_user_csv(path)), 3)
        self.assertEqual(len(read_user_csv(path, country="Germany")), 2)
        self.assertEqual(len(read_user_csv(path, prefixes=["2635"])), 1)
        self.assertEqual(len(read_user_csv(path, limit=2)), 2)
        entries = read_user_csv(path, country="Germany")
        self.assertEqual(entries[0].name, "Jens M")

        out = os.path.join(tmp, "zurueck.csv")
        self.assertEqual(write_user_csv(out, entries), 2)
        again = read_user_csv(out)
        self.assertEqual([e.id for e in again], [e.id for e in entries])
        self.assertEqual(again[0].callsign, "DA6JEY")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class AprsSettings(unittest.TestCase):
    """APRS-Einstellungen, geprueft an Bytes aus echter Hardware."""

    # Erste 64 Byte von 0x02501000 eines AT-D878UV II Plus (Firmware V101)
    REAL = bytes.fromhex(
        "00144800001E0000130028020000220C"
        "49006C31640041504154383101444136"
        "4A45590757494445312D315749444532"
        "2D32000000000000002F2603960000FF"
    )

    def _layout(self):
        from atprog import layout as layout_mod
        return layout_mod.load()

    def test_decodes_real_beacon(self):
        from atprog.codec import decode_record
        obj = self._layout().obj("aprs")
        rec = decode_record(obj, self.REAL + bytes(obj.record_size - len(self.REAL)))
        self.assertEqual(rec["source"], "DA6JEY")
        self.assertEqual(rec["source_ssid"], 7)
        self.assertEqual(rec["destination"], "APAT81")
        self.assertEqual(rec["destination_ssid"], 1)
        self.assertEqual(rec["path"], "WIDE1-1WIDE2-2")
        self.assertEqual(rec["fm_frequency"], 144_800_000)
        self.assertEqual(rec["symbol_table"], "/")
        self.assertEqual(rec["symbol"], "&")
        self.assertEqual(rec["auto_interval"], 2)          # alle 60 Sekunden

    def test_roundtrip_keeps_bytes(self):
        from atprog.codec import roundtrip_ok
        obj = self._layout().obj("aprs")
        data = self.REAL + bytes(obj.record_size - len(self.REAL))
        self.assertTrue(roundtrip_ok(obj, data))

    def test_patch_touches_only_changed_field(self):
        from atprog.device import patch_settings
        from atprog.image import MemoryImage
        from atprog.models import Codeplug
        obj = self._layout().obj("aprs")
        img = MemoryImage()
        img.write(obj.address(0), self.REAL + bytes(obj.record_size - len(self.REAL)))
        cp = Codeplug()
        cp.settings["aprs"] = {"source": "DA6JEY", "source_ssid": 9}
        out = patch_settings(img, cp)
        changes = img.diff(out)
        self.assertEqual(len(changes), 1)
        addr, old, new = changes[0]
        self.assertEqual(addr, obj.address(0) + 35)        # nur das SSID-Byte
        self.assertEqual((old, new), (b"\x07", b"\x09"))
