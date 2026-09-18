"""Ende-zu-Ende-Test: Simulator -> lesen -> decodieren -> CSV -> zurueckschreiben."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from atprog import layout as layout_mod
from atprog.codec import decode_record
from atprog.csvio import export_all, import_all, read_channels
from atprog.device import (decode, field_write_allowed, patch_channels, read_image,
                           verify_layout, write_image)
from atprog.image import MemoryImage
from atprog.models import Codeplug, parse_freq
from atprog.protocol import Radio
from atprog.simulator import SimulatedRadio, populate_demo


class EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sim = SimulatedRadio()
        cls.port = cls.sim.start()
        populate_demo(cls.sim, channels=8)
        cls.tmp = tempfile.mkdtemp(prefix="atprog-test-")

    @classmethod
    def tearDownClass(cls):
        cls.sim.stop()

    def _radio(self):
        return Radio(self.port, timeout=3.0, block_size=64)

    def test_01_identify(self):
        radio = self._radio()
        info = radio.open()
        try:
            self.assertEqual(info.model, "878UV2")
            self.assertTrue(info.is_878uv2)
            self.assertTrue(layout_mod.load_for(info.model).matches(info.model))
        finally:
            radio.close()

    def test_02_read_and_decode(self):
        radio = self._radio()
        radio.open()
        try:
            img = read_image(radio, layout_mod.load(), smart=True)
        finally:
            radio.close()
        path = os.path.join(self.tmp, "backup.atbin")
        img.save(path)
        self.assertGreater(img.size(), 1000)

        reloaded = MemoryImage.load(path)
        self.assertEqual(reloaded.ranges(), img.ranges())

        for check in verify_layout(img):
            self.assertFalse(check.failures, "Round-Trip fehlgeschlagen: %s" % check.line())

        cp = decode(img)
        self.assertEqual(len(cp.channels), 8)
        self.assertEqual(cp.channels[0].name, "DEMO 01")
        self.assertEqual(cp.channels[0].rx_freq, 438_000_000)
        self.assertEqual(cp.channels[0].tx_freq, 430_400_000)   # 7,6 MHz Ablage, minus
        self.assertEqual(len(cp.talkgroups), 3)
        self.assertEqual(cp.talkgroups[2].tg_id, 262)
        self.assertEqual(len(cp.zones), 1)
        self.assertEqual(len(cp.zones[0].channels), 8)
        self.assertEqual(cp.radio_ids[0].radio_id, 2621234)
        EndToEnd.image_path = path
        EndToEnd.codeplug = cp

    def test_03_csv_roundtrip(self):
        cp = EndToEnd.codeplug
        csv_dir = os.path.join(self.tmp, "csv")
        files = export_all(csv_dir, cp)
        self.assertEqual(len(files), 6)
        again, loaded = import_all(csv_dir)
        self.assertIn("Channel.CSV", loaded)
        self.assertEqual(len(again.channels), len(cp.channels))
        self.assertEqual(again.channels[3].name, cp.channels[3].name)
        self.assertEqual(again.channels[3].rx_freq, cp.channels[3].rx_freq)
        self.assertEqual(again.channels[3].tx_freq, cp.channels[3].tx_freq)
        self.assertEqual([z.channels for z in again.zones], [z.channels for z in cp.zones])
        self.assertEqual(again.talkgroups[1].tg_id, cp.talkgroups[1].tg_id)

    def test_04_project_roundtrip(self):
        path = os.path.join(self.tmp, "projekt.json")
        EndToEnd.codeplug.save(path)
        again = Codeplug.load(path)
        self.assertEqual(again.stats(), EndToEnd.codeplug.stats())
        self.assertEqual(again.channels[0].to_dict(), EndToEnd.codeplug.channels[0].to_dict())

    def test_05_differential_write(self):
        img = MemoryImage.load(EndToEnd.image_path)
        cp = decode(img)
        cp.channels[2].name = "GEAENDERT"
        cp.channels[2].rx_freq = parse_freq("439.1250")
        cp.channels[2].tx_freq = parse_freq("431.5250")
        patched = patch_channels(img, cp)

        radio = self._radio()
        radio.open()
        try:
            before_writes = self.sim.writes
            report = write_image(radio, patched, differential=True, verify=True)
        finally:
            radio.close()
        self.assertEqual(report.mismatches, [])
        blocks = self.sim.writes - before_writes
        self.assertEqual(blocks, report.blocks_written)
        self.assertLessEqual(blocks, 2, "differenzielles Schreiben fasst zu viel an")
        self.assertGreater(report.blocks_skipped, 50)

        obj = layout_mod.load().obj("channel")
        rec = decode_record(obj, self.sim.peek(obj.address(2), obj.record_size))
        self.assertEqual(rec["name"], "GEAENDERT")
        self.assertEqual(rec["rx_freq"], 439_125_000)
        self.assertEqual(rec["repeater"], "Minus")

    def test_06_field_write_guard(self):
        img = MemoryImage.load(EndToEnd.image_path)
        allowed, reason = field_write_allowed(img)
        self.assertTrue(allowed, reason)
        self.assertIn("channel", reason)

        # Ein Layout, das nicht passt, muss das Feldschreiben sperren.
        broken = MemoryImage.load(EndToEnd.image_path)
        obj = layout_mod.load().obj("channel")
        addr = obj.address(0)
        raw = bytearray(broken.read(addr, obj.record_size))
        # Ungueltige BCD-Ziffern in der Frequenz: der Codec kann diesen
        # Datensatz nicht byteidentisch rekonstruieren - genau der Fall, in dem
        # das Layout die Firmware nicht korrekt beschreibt.
        freq = obj.field("rx_freq")
        raw[freq.offset:freq.offset + freq.length] = b"\xab\xcd\xef\x01"
        broken.write(addr, bytes(raw))
        allowed, reason = field_write_allowed(broken)
        self.assertFalse(allowed)
        self.assertIn("Layout passt nicht", reason)

    def test_07_rename_follows_references(self):
        img = MemoryImage.load(EndToEnd.image_path)
        cp = decode(img)
        old = cp.channels[1].name
        cp.rename_channel(old, "DB0TEST TS2")
        cp.channels[1].name = "DB0TEST TS2"
        self.assertIn("DB0TEST TS2", cp.zones[0].channels)
        self.assertNotIn(old, cp.zones[0].channels)
        self.assertEqual([i for i in cp.validate() if i.level == "error"], [])

    def test_08_validation(self):
        cp = Codeplug.load(os.path.join(self.tmp, "projekt.json"))
        issues = cp.validate()
        errors = [i for i in issues if i.level == "error"]
        self.assertEqual(errors, [], "unerwartete Fehler: %s" % [str(e) for e in errors])
        cp.channels[0].rx_freq = 900_000_000
        cp.channels[1].name = cp.channels[0].name
        cp.channels[3].color_code = 99
        levels = [i.level for i in cp.validate()]
        self.assertGreaterEqual(levels.count("error"), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class RealImageInvariants(unittest.TestCase):
    """Prueft die wichtigste Sicherheitszusage am echten Geraeteabbild.

    Ein unveraendert eingelesener und wieder eingebauter Codeplug darf kein
    einziges Byte im Abbild veraendern. Laeuft nur, wenn ein Abbild vorliegt
    (Umgebungsvariable ATPROG_TEST_IMAGE oder ./backup.atbin).
    """

    def setUp(self):
        path = os.environ.get("ATPROG_TEST_IMAGE", "")
        if not path or not os.path.exists(path):
            self.skipTest("kein Geraeteabbild vorhanden")
        self.img = MemoryImage.load(path)

    def test_layout_matches_image(self):
        allowed, reason = field_write_allowed(self.img)
        self.assertTrue(allowed, reason)

    def test_decode_patch_is_byte_identical(self):
        unchanged = patch_channels(self.img, decode(self.img))
        self.assertEqual(self.img.changed_blocks(unchanged, 64), [],
                         "Einlesen und Zurueckbauen veraendert Bytes")

    def test_single_change_touches_one_record(self):
        cp = decode(self.img)
        cp.channels[0].power = "Low" if cp.channels[0].power != "Low" else "High"
        out = patch_channels(self.img, cp)
        self.assertEqual(len(self.img.changed_blocks(out, 64)), 1)


class CallsignDatabase(unittest.TestCase):
    """Rufzeichendatenbank gegen das simulierte Geraet."""

    @classmethod
    def setUpClass(cls):
        from atprog.simulator import SimulatedRadio
        cls.sim = SimulatedRadio()
        cls.port = cls.sim.start()

    @classmethod
    def tearDownClass(cls):
        cls.sim.stop()

    def _db(self, n=600):
        from atprog.userdb import UserDatabase, UserEntry
        return UserDatabase(entries=[
            UserEntry(id=2620000 + i * 3, callsign="DL%04d" % i, name="Name%d" % i,
                      city="Ort%d" % (i % 20), country="Germany") for i in range(n)])

    def test_write_read_and_verify(self):
        from atprog.protocol import Radio
        from atprog.userdb import probe, read_from_radio, verify_on_radio, write_to_radio
        db = self._db()
        radio = Radio(self.port, timeout=3.0)
        radio.open()
        try:
            report = write_to_radio(radio, db.build(), probe(radio))
            self.assertTrue(report["header_ok"])
            status = probe(radio)
            self.assertEqual(status.count, 600)
            self.assertTrue(status.bcd_id)
            check = verify_on_radio(radio, status, samples=10)
            self.assertEqual(check["bad"], [])
            entries = read_from_radio(radio, status)
            self.assertEqual(len(entries), 600)
            self.assertEqual(entries[0].callsign, "DL0000")
            self.assertEqual(entries[-1].city, "Ort19")
        finally:
            radio.close()

    def test_aborted_transfer_is_detected(self):
        """Ein abgebrochener Schreibvorgang muss auffallen.

        Genau das ist an echter Hardware passiert: das Geraet antwortete nach
        rund 87 Prozent nicht mehr, und ohne Pruefung faellt das kaum auf, weil
        die Anfangseintraege ja vorhanden sind.
        """
        import struct
        from atprog.protocol import Radio
        from atprog.userdb import probe, verify_on_radio, write_to_radio
        db = self._db()
        radio = Radio(self.port, timeout=3.0)
        radio.open()
        try:
            write_to_radio(radio, db.build(), probe(radio))
            status = probe(radio)
        finally:
            radio.close()

        geo = status.geometry
        last = struct.unpack("<II", self.sim.peek(geo.index_address(599), 8))[1]
        start = int(last * 0.8)
        self.sim.poke(geo.entry_address(start), b"\xff" * (last - start + 200))

        radio = Radio(self.port, timeout=3.0)
        radio.open()
        try:
            check = verify_on_radio(radio, probe(radio), samples=10)
        finally:
            radio.close()
        self.assertTrue(check["bad"], "unvollstaendige Datenbank blieb unbemerkt")
        self.assertGreater(check["first_bad"], 400)
        self.assertLess(check["first_bad"], 600)


class DifferentialWriteSafety(unittest.TestCase):
    """Ein Schreibvorgang darf nur tatsaechliche Unterschiede uebertragen.

    Hintergrund: Wurde als Vergleichsstand eine verkuerzte Sicherung benutzt,
    galt jeder nicht gelesene Block als geaendert - damit wurde der gesamte
    Codeplug geschrieben statt weniger Bytes. Genau das ist an echter Hardware
    passiert und hat dort die Kontaktliste zerstoert.
    """

    @classmethod
    def setUpClass(cls):
        from atprog.simulator import SimulatedRadio, populate_demo
        cls.sim = SimulatedRadio()
        cls.port = cls.sim.start()
        populate_demo(cls.sim, channels=8)

    @classmethod
    def tearDownClass(cls):
        cls.sim.stop()

    def test_incomplete_baseline_is_not_trusted(self):
        from atprog.protocol import Radio
        from atprog.device import read_image, write_image
        radio = Radio(self.port, timeout=3.0)
        radio.open()
        try:
            voll = read_image(radio, smart=False)
            verkuerzt = read_image(radio, smart=True)
            self.assertLess(verkuerzt.size(), voll.size())
            # Mit dem verkuerzten Stand als Vergleich darf trotzdem nichts
            # geschrieben werden, weil sich nichts geaendert hat.
            vorher = self.sim.writes
            report = write_image(radio, voll, differential=True, verify=False,
                                 current=verkuerzt)
            self.assertEqual(report.blocks_written, 0)
            self.assertEqual(self.sim.writes, vorher)
        finally:
            radio.close()


class WriteAlignment(unittest.TestCase):
    """Das Geraet nimmt nur vollstaendige, ausgerichtete Bloecke an.

    An echter Hardware gemessen: ein 16-Byte-Kommando bei 0x02950000 wird
    quittiert, ein anschliessendes Ein-Byte-Kommando bei 0x02950010 bleibt ohne
    jede Antwort. atprog muss Randbloecke daher lesen, mischen und vollstaendig
    zurueckschreiben.
    """

    @classmethod
    def setUpClass(cls):
        from atprog.simulator import SimulatedRadio
        cls.sim = SimulatedRadio()
        cls.port = cls.sim.start()

    @classmethod
    def tearDownClass(cls):
        cls.sim.stop()

    def setUp(self):
        # Jeder Test bekommt eine frische, bekannte Umgebung.
        self.sim.poke(0x02950000, b"\xaa" * 128)

    def test_unaligned_write_uses_full_blocks(self):
        from atprog.protocol import Radio
        radio = Radio(self.port, timeout=3.0)
        radio.open()
        try:
            # 17 Byte ab einer um 3 verschobenen Adresse
            radio.write_region(0x02950003, b"X" * 17)
        finally:
            radio.close()
        # Die Nutzdaten stehen richtig ...
        self.assertEqual(self.sim.peek(0x02950003, 17), b"X" * 17)
        # ... und die Bytes davor und danach sind unveraendert geblieben.
        self.assertEqual(self.sim.peek(0x02950000, 3), b"\xaa" * 3)
        self.assertEqual(self.sim.peek(0x02950014, 4), b"\xaa" * 4)

    def test_only_full_blocks_are_sent(self):
        from atprog.protocol import Radio
        gesendet = []
        radio = Radio(self.port, timeout=3.0)
        radio.open()
        original = radio.write_block
        try:
            radio.write_block = lambda a, d: (gesendet.append((a, len(d))), original(a, d))[1]
            radio.write_region(0x02950005, b"Y" * 21)
        finally:
            radio.write_block = original
            radio.close()
        self.assertTrue(gesendet)
        for adresse, laenge in gesendet:
            self.assertEqual(laenge, 16, "Block bei 0x%08X hat %d Byte" % (adresse, laenge))
            self.assertEqual(adresse % 16, 0, "Block bei 0x%08X ist nicht ausgerichtet" % adresse)
