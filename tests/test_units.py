"""Einzeltests fuer Datenmodell, Codec, Abbild und CSV."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from atprog import layout as layout_mod
from atprog.codec import (bitmap_get, bitmap_indices, bitmap_set, decode_bcd_be,
                          decode_record, encode_bcd_be, encode_record, roundtrip_ok)
from atprog.csvio import read_channels, write_channels
from atprog.image import ImageError, MemoryImage
from atprog.models import (Channel, Codeplug, RadioID, TalkGroup, Zone,
                           fmt_freq, norm_tone, parse_freq, tone_is_valid)
from atprog.protocol import checksum


class Frequencies(unittest.TestCase):
    def test_parse_variants(self):
        for text, hz in [("438.5", 438_500_000), ("438,50000", 438_500_000),
                         ("145.7875", 145_787_500), ("438500000", 438_500_000),
                         (438.5, 438_500_000), ("439.5 MHz", 439_500_000), ("", 0)]:
            self.assertEqual(parse_freq(text), hz, text)

    def test_format(self):
        self.assertEqual(fmt_freq(145_787_500), "145.78750")
        self.assertEqual(fmt_freq(0), "")

    def test_bad_input(self):
        with self.assertRaises(ValueError):
            parse_freq("keine Frequenz")


class Tones(unittest.TestCase):
    def test_normalise(self):
        self.assertEqual(norm_tone("67"), "67.0")
        self.assertEqual(norm_tone(" 88,5 "), "88.5")
        self.assertEqual(norm_tone("d023n"), "D023N")
        self.assertEqual(norm_tone("023I"), "D023I")
        self.assertEqual(norm_tone(""), "Off")

    def test_validity(self):
        self.assertTrue(tone_is_valid("Off"))
        self.assertTrue(tone_is_valid("123.0"))
        self.assertTrue(tone_is_valid("D754N"))
        self.assertFalse(tone_is_valid("123.4"))
        self.assertFalse(tone_is_valid("D999N"))


class Bitmaps(unittest.TestCase):
    def test_set_get(self):
        bits = bytearray(4)
        for i in (0, 7, 8, 31):
            bitmap_set(bits, i, True)
        self.assertEqual(bitmap_indices(bits, 32), [0, 7, 8, 31])
        bitmap_set(bits, 7, False)
        self.assertFalse(bitmap_get(bits, 7))

    def test_growth(self):
        bits = bytearray()
        bitmap_set(bits, 100, True)
        self.assertEqual(bitmap_indices(bits, 128), [100])


class Codec(unittest.TestCase):
    def setUp(self):
        self.lay = layout_mod.load()
        self.ch = self.lay.obj("channel")

    def test_bcd(self):
        for value in (0, 1, 2621234, 16777215):
            self.assertEqual(decode_bcd_be(encode_bcd_be(value, 4)), value)

    def test_patch_keeps_unknown_bytes(self):
        original = bytes(range(64))
        field = self.ch.field("name")
        patched = encode_record(self.ch, {"name": "TEST"}, original)
        # Nur die Namensbytes duerfen sich aendern.
        for i in range(64):
            if field.offset <= i < field.offset + field.length:
                continue
            self.assertEqual(patched[i], original[i], "Byte %d wurde veraendert" % i)

    def test_roundtrip(self):
        values = {"rx_freq": 145_787_500, "tx_offset": 600_000, "repeater": "Minus",
                  "ch_type": "A-Analog", "power": "Low", "bandwidth": "25K",
                  "decode_type": "CTCSS", "encode_type": "CTCSS",
                  "ctcss_encode_index": 9, "ctcss_decode_index": 9,
                  "custom_ctcss": 2511, "contact_index": 17, "scan_list_index": 2,
                  "rx_group_index": 255, "color_code": 1, "slot": "2",
                  "name": "Relais Nord"}
        data = encode_record(self.ch, values, b"\x00" * 64)
        self.assertTrue(roundtrip_ok(self.ch, data))
        self.assertEqual(decode_record(self.ch, data), values)

    def test_bcd_frequency_layout(self):
        # Das Geraet legt Frequenzen als BCD in 10-Hz-Schritten ab:
        # 439,1 MHz -> 43 91 00 00
        data = encode_record(self.ch, {"rx_freq": 439_100_000}, b"\x00" * 64)
        self.assertEqual(data[0:4], bytes([0x43, 0x91, 0x00, 0x00]))
        self.assertEqual(decode_record(self.ch, data)["rx_freq"], 439_100_000)

    def test_channel_addresses(self):
        self.assertEqual(self.ch.address(0), 0x00800000)
        self.assertEqual(self.ch.address(1), 0x00800040)
        self.assertEqual(self.ch.address(127), 0x00800000 + 127 * 64)
        self.assertEqual(self.ch.address(128), 0x00840000)      # naechste Bank
        with self.assertRaises(IndexError):
            self.ch.address(4000)


class Image(unittest.TestCase):
    def test_write_read_merge(self):
        img = MemoryImage()
        img.write(0x100, b"ABCD")
        img.write(0x104, b"EFGH")
        self.assertEqual(img.read(0x100, 8), b"ABCDEFGH")
        self.assertEqual(len(img.ranges()), 1)

    def test_gap_handling(self):
        img = MemoryImage()
        img.write(0x000, b"AA")
        img.write(0x100, b"BB")
        self.assertEqual(len(img.ranges()), 2)
        with self.assertRaises(ImageError):
            img.read(0x50, 2)
        self.assertEqual(img.read(0x00, 4, fill=0xFF), b"AA\xff\xff")

    def test_changed_blocks(self):
        a = MemoryImage()
        a.write(0, bytes(256))
        b = a.copy()
        b.write(70, b"\x01")
        changed = a.changed_blocks(b, block=64)
        self.assertEqual([addr for addr, _ in changed], [64])

    def test_container_roundtrip(self):
        img = MemoryImage({"model": "878UV2"})
        img.write(0x00800000, bytes(range(64)))
        img.write(0x02640000, b"\x01\x02\x03")
        with tempfile.NamedTemporaryFile(suffix=".atbin", delete=False) as fh:
            path = fh.name
        try:
            img.save(path)
            again = MemoryImage.load(path)
            self.assertEqual(again.meta["model"], "878UV2")
            self.assertEqual(again.ranges(), img.ranges())
            self.assertEqual(again.read(0x00800000, 64), bytes(range(64)))
        finally:
            os.unlink(path)


class Protocol(unittest.TestCase):
    def test_checksum(self):
        self.assertEqual(checksum(b"\x00\x80\x00\x00\x40"), 0xC0)
        self.assertEqual(checksum(b"\xff" * 4), 0xFC)


class Csv(unittest.TestCase):
    def test_unknown_columns_survive(self):
        cp = Codeplug()
        ch = Channel(name="Test", rx_freq=parse_freq("438.5"), tx_freq=parse_freq("430.9"))
        ch.extra = {"Eigenes Feld": "42"}
        cp.channels.append(ch)
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "Channel.CSV")
        write_channels(path, cp)
        with open(path, encoding="utf-8-sig") as fh:
            header = fh.readline()
        self.assertIn("Eigenes Feld", header)
        self.assertIn("Channel Name", header)
        again = read_channels(path)
        self.assertEqual(again[0].extra.get("Eigenes Feld"), "42")
        self.assertEqual(again[0].rx_freq, 438_500_000)

    def test_semicolon_and_bom(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "Channel.CSV")
        with open(path, "w", encoding="utf-8-sig") as fh:
            fh.write("No.;Channel Name;Receive Frequency;Transmit Frequency;Channel Type\r\n")
            fh.write("1;Semikolon;145,5875;145,1875;D-Digital\r\n")
        rows = read_channels(path)
        self.assertEqual(rows[0].name, "Semikolon")
        self.assertEqual(rows[0].rx_freq, 145_587_500)


class Validation(unittest.TestCase):
    def _base(self):
        cp = Codeplug()
        cp.radio_ids.append(RadioID(name="DL0ABC", radio_id=2621234))
        cp.talkgroups.append(TalkGroup(name="Welt", tg_id=91))
        cp.channels.append(Channel(name="Relais", rx_freq=parse_freq("438.5"),
                                   tx_freq=parse_freq("430.9"), contact="Welt",
                                   radio_id="DL0ABC"))
        cp.zones.append(Zone(name="Heimat", channels=["Relais"], a_channel="Relais",
                             b_channel="Relais"))
        return cp

    def test_clean(self):
        self.assertEqual([i for i in self._base().validate() if i.level == "error"], [])

    def test_missing_contact(self):
        cp = self._base()
        cp.channels[0].contact = "Gibt es nicht"
        errors = [i.message for i in cp.validate() if i.level == "error"]
        self.assertTrue(any("Kontakt" in m for m in errors), errors)

    def test_zone_member_missing(self):
        cp = self._base()
        cp.zones[0].channels = ["Unbekannt"]
        errors = [i.message for i in cp.validate() if i.level == "error"]
        self.assertTrue(any("existiert nicht" in m for m in errors), errors)

    def test_out_of_band(self):
        cp = self._base()
        cp.channels[0].rx_freq = parse_freq("800.0")
        errors = [i.message for i in cp.validate() if i.level == "error"]
        self.assertTrue(any("Empfangsbereiche" in m for m in errors), errors)

    def test_no_radio_id(self):
        cp = self._base()
        cp.radio_ids = []
        errors = [i.message for i in cp.validate() if i.level == "error"]
        self.assertTrue(any("DMR-ID" in m for m in errors), errors)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class Renaming(unittest.TestCase):
    def _cp(self):
        cp = Codeplug()
        cp.radio_ids.append(RadioID(name="DL0ABC", radio_id=2621234))
        cp.talkgroups.append(TalkGroup(name="Welt", tg_id=91))
        cp.channels.append(Channel(name="Alt", rx_freq=parse_freq("438.5"),
                                   tx_freq=parse_freq("430.9"), contact="Welt",
                                   radio_id="DL0ABC"))
        cp.zones.append(Zone(name="Zone", channels=["Alt"], a_channel="Alt", b_channel="Alt"))
        return cp

    def test_channel_references_follow(self):
        cp = self._cp()
        cp.channels[0].name = "Neu"
        cp.rename_channel("Alt", "Neu")
        self.assertEqual(cp.zones[0].channels, ["Neu"])
        self.assertEqual(cp.zones[0].a_channel, "Neu")
        self.assertEqual([i for i in cp.validate() if i.level == "error"], [])

    def test_talkgroup_references_follow(self):
        cp = self._cp()
        cp.talkgroups[0].name = "Weltweit"
        cp.rename_talkgroup("Welt", "Weltweit")
        self.assertEqual(cp.channels[0].contact, "Weltweit")
        self.assertEqual([i for i in cp.validate() if i.level == "error"], [])
