"""Test der Weboberflaeche ueber ihre HTTP-Schnittstelle."""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from http.server import ThreadingHTTPServer
from atprog.webui import Handler, Session

BASE = None


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=20) as res:
        return json.loads(res.read().decode("utf-8"))


def post(path, payload=None):
    data = json.dumps(payload or {}).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as res:
        return json.loads(res.read().decode("utf-8"))


def wait_task(timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        task = get("/api/task")
        if task.get("finished"):
            return task
        time.sleep(0.2)
    raise AssertionError("Aufgabe wurde nicht fertig")


class WebUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global BASE
        cls.tmp = tempfile.mkdtemp(prefix="atprog-web-")
        Handler.session = Session(cls.tmp)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        BASE = "http://127.0.0.1:%d" % cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        if Handler.session.sim:
            Handler.session.sim.stop()
        cls.httpd.shutdown()

    def test_01_static_pages(self):
        for path, needle in (("/", b"AT-D878UV II Plus"), ("/app.js", b"COLUMNS"),
                             ("/app.css", b"--accent")):
            with urllib.request.urlopen(BASE + path, timeout=10) as res:
                self.assertEqual(res.status, 200)
                self.assertIn(needle, res.read())

    def test_02_state(self):
        st = get("/api/state")
        from atprog.version import __version__
        self.assertEqual(st["app"]["version"], __version__)
        self.assertEqual(st["layout"]["id"], "d878uv2plus")
        self.assertIn("channels", st["project"]["stats"])

    def test_03_edit_rows(self):
        self.assertTrue(post("/api/row/add", {"kind": "channels", "row": {
            "name": "Testkanal", "rx": "438.5", "tx": "430.9"}})["ok"])
        rows = get("/api/rows?kind=channels")
        self.assertEqual(rows["total"], 1)
        self.assertEqual(rows["rows"][0]["rx"], "438.50000")

        post("/api/row/save", {"kind": "channels", "index": 0,
                               "row": {"name": "Umbenannt", "power": "Turbo"}})
        row = get("/api/rows?kind=channels")["rows"][0]
        self.assertEqual(row["name"], "Umbenannt")
        self.assertEqual(row["power"], "Turbo")

        post("/api/row/add", {"kind": "talkgroups", "row": {"name": "Welt", "tg_id": 91}})
        self.assertEqual(get("/api/rows?kind=talkgroups")["rows"][0]["tg_id"], 91)

        self.assertEqual(get("/api/rows?kind=channels&q=umbenannt")["total"], 1)
        self.assertEqual(get("/api/rows?kind=channels&q=xyz")["total"], 0)

    def test_04_validate_and_files(self):
        res = post("/api/validate")
        self.assertTrue(any("DMR-ID" in e for e in res["errors"]))

        post("/api/row/add", {"kind": "radio_ids", "row": {"name": "DL0TEST",
                                                           "radio_id": 2621234}})
        saved = post("/api/project/save", {"path": "projekt.json"})
        self.assertTrue(os.path.exists(saved["path"]))

        exported = post("/api/csv/export", {"dir": "csv"})
        self.assertIn("Channel.CSV", exported["files"])

        post("/api/project/new", {"name": "leer"})
        self.assertEqual(get("/api/state")["project"]["stats"]["channels"], 0)
        post("/api/csv/import", {"dir": "csv"})
        self.assertEqual(get("/api/state")["project"]["stats"]["channels"], 1)
        post("/api/project/open", {"path": "projekt.json"})
        self.assertEqual(get("/api/state")["project"]["stats"]["radio_ids"], 1)

    def test_045_writing_needs_explicit_port(self):
        """Schreibende Vorgaenge duerfen keinen Port automatisch waehlen.

        Sonst kann eine Anfrage ohne Portangabe auf einem beliebigen
        angeschlossenen Geraet landen.
        """
        for route, payload in (
                ("/api/radio/write", {"image": "x.atbin", "confirm": True}),
                ("/api/userdb/write", {"csv": "x.csv", "confirm": True})):
            res = post(route, payload)
            self.assertFalse(res.get("ok"), route)
            self.assertIn("Port", res.get("error", ""), route)

    def test_046_userdb_viewer(self):
        """Laden, Blaettern und Suchen in einer Rufzeichenliste."""
        path = os.path.join(self.tmp, "rufzeichen.csv")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("RADIO_ID,CALLSIGN,FIRST_NAME,LAST_NAME,CITY,STATE,COUNTRY\n")
            fh.write("2635224,DA6JEY,Jens,M,Leverkusen,NRW,Germany\n")
            fh.write("2631065,DO1KMK,Michael,K,Koeln,NRW,Germany\n")
            fh.write("3124726,N7EWB,Eric,B,Kirkland,WA,United States\n")
            fh.write("kaputt,,,,,,\n")                      # muss uebersprungen werden
        res = post("/api/userdb/load", {"path": path})
        self.assertTrue(res["ok"])
        self.assertEqual(res["count"], 3)

        page = get("/api/userdb/rows?limit=2")
        self.assertEqual(page["total"], 3)
        self.assertEqual(len(page["rows"]), 2)
        self.assertEqual(page["rows"][0]["callsign"], "DA6JEY")
        self.assertEqual(page["rows"][0]["name"], "Jens M")

        self.assertEqual(get("/api/userdb/rows?offset=2")["rows"][0]["id"], 3124726)
        self.assertEqual(get("/api/userdb/rows?q=koeln")["total"], 1)
        self.assertEqual(get("/api/userdb/rows?q=germany")["total"], 2)
        self.assertEqual(get("/api/userdb/rows?q=nrw+leverkusen")["total"], 1)
        self.assertEqual(get("/api/userdb/rows?q=2635224")["rows"][0]["callsign"], "DA6JEY")
        self.assertEqual(get("/api/userdb/rows?q=gibtesnicht")["total"], 0)

        state = get("/api/state")
        self.assertEqual(state["userdb"]["count"], 3)

    def test_047_missing_file(self):
        res = post("/api/userdb/load", {"path": "gibtesnicht.csv"})
        self.assertFalse(res["ok"])
        self.assertIn("nicht gefunden", res["error"])

    def test_05_simulator_read(self):
        sim = post("/api/simulator", {"action": "start", "channels": 6})
        self.assertTrue(sim["ok"])
        port = sim["port"]

        info = post("/api/radio/info", {"port": port})
        self.assertEqual(info["model"], "878UV2")
        self.assertTrue(info["matches"])

        started = post("/api/radio/read", {"port": port, "out": "gelesen.atbin"})
        self.assertTrue(started["ok"])
        task = wait_task()
        self.assertIsNone(task["error"])
        self.assertGreater(task["result"]["bytes"], 1000)
        self.assertEqual(task["result"]["stats"]["channels"], 6)
        self.assertTrue(all("OK" in line or "keine Daten" in line
                            for line in task["result"]["checks"]), task["result"]["checks"])

        rows = get("/api/rows?kind=channels")
        self.assertEqual(rows["total"], 6)
        self.assertTrue(rows["rows"][0]["name"].startswith("DEMO"))

        written = post("/api/radio/write", {"port": port, "image": "gelesen.atbin",
                                            "confirm": True})
        self.assertTrue(written["ok"], written)
        task = wait_task(timeout=120)
        self.assertIsNone(task["error"])
        self.assertIn("Bloecke geschrieben", task["result"]["summary"])

        self.assertTrue(post("/api/simulator", {"action": "stop"})["ok"])

    @staticmethod
    def _clear_dirty():
        """Ungeschrieben-Marker direkt entfernen - als haette das Geraet den
        Stand schon; nur so laesst sich das Setzen isoliert pruefen."""
        for ch in Handler.session.project.channels:
            ch.extra.pop("_dirty", None)

    def test_06_bulk_save(self):
        """Sammelaenderung setzt Felder fuer mehrere Kanaele auf einmal."""
        post("/api/project/new", {"name": "bulk"})
        for name in ("Alpha", "Beta", "Gamma"):
            post("/api/row/add", {"kind": "channels",
                                  "row": {"name": name, "rx": "438.5", "tx": "430.9"}})
        # Neu angelegte Kanaele gelten als noch nicht geschrieben.
        self.assertTrue(all(r["dirty"] == 1
                            for r in get("/api/rows?kind=channels")["rows"]))
        self._clear_dirty()

        # name und rx duerfen nicht mitgehen: name wuerde Duplikate erzeugen.
        res = post("/api/rows/save", {"kind": "channels", "indexes": [0, 2],
                                      "row": {"power": "Low", "name": "BOESE",
                                              "rx": "999.0"}})
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["changed"], 2)
        self.assertEqual(res["skipped"], 0)
        rows = get("/api/rows?kind=channels")["rows"]
        self.assertEqual([r["name"] for r in rows], ["Alpha", "Beta", "Gamma"])
        self.assertEqual([r["rx"] for r in rows], ["438.50000"] * 3)
        self.assertEqual([r["power"] for r in rows], ["Low", "High", "Low"])
        self.assertEqual([r["dirty"] for r in rows], [1, 0, 1])

        # Gleicher Wert noch einmal: nichts aendert sich, nichts wird markiert.
        again = post("/api/rows/save", {"kind": "channels", "indexes": [0, 2],
                                        "row": {"power": "Low"}})
        self.assertEqual(again["changed"], 0)

        # Indizes ausserhalb werden gezaehlt, nicht angewendet.
        off = post("/api/rows/save", {"kind": "channels", "indexes": [99],
                                      "row": {"power": "Mid"}})
        self.assertEqual((off["changed"], off["skipped"]), (0, 1))

        # Nur Kanaele; leere Angaben sind Fehler.
        self.assertFalse(post("/api/rows/save", {"kind": "talkgroups",
                                                 "indexes": [0],
                                                 "row": {"call_type": "All Call"}})["ok"])
        self.assertFalse(post("/api/rows/save", {"kind": "channels", "indexes": [],
                                                 "row": {"power": "Low"}})["ok"])
        self.assertFalse(post("/api/rows/save", {"kind": "channels", "indexes": [0],
                                                 "row": {"name": "nur verbotene"}})["ok"])

    def test_07_dirty_counter_and_row_save(self):
        """Zaehler im Zustand; Einzelaenderung markiert nur echte Aenderungen."""
        self.assertEqual(get("/api/state")["project"]["dirty_channels"], 2)

        post("/api/row/save", {"kind": "channels", "index": 1,
                               "row": {"power": "Turbo"}})
        self.assertEqual(get("/api/state")["project"]["dirty_channels"], 3)

        # Speichern ohne Wertaenderung setzt keinen Marker.
        self._clear_dirty()
        post("/api/row/save", {"kind": "channels", "index": 1,
                               "row": {"power": "Turbo"}})
        rows = get("/api/rows?kind=channels")["rows"]
        self.assertEqual([r["dirty"] for r in rows], [0, 0, 0])
        post("/api/row/save", {"kind": "channels", "index": 1,
                               "row": {"power": "Mid"}})
        self.assertEqual(get("/api/rows?kind=channels")["rows"][1]["dirty"], 1)

    def test_08_dirty_survives_project_roundtrip(self):
        """Der Marker uebersteht Speichern/Laden, bleibt aber aus der CSV."""
        self.assertEqual(get("/api/state")["project"]["dirty_channels"], 1)
        post("/api/project/save", {"path": "dirty.json"})
        post("/api/project/new", {"name": "leer"})
        post("/api/project/open", {"path": "dirty.json"})
        rows = get("/api/rows?kind=channels")["rows"]
        self.assertEqual([r["dirty"] for r in rows], [0, 1, 0])

        exported = post("/api/csv/export", {"dir": "csv-dirty"})
        path = os.path.join(exported["dir"], "Channel.CSV")
        with open(path, encoding="utf-8") as fh:
            self.assertNotIn("_dirty", fh.read())

    def test_09_dirty_lifecycle_simulator(self):
        """Lesen -> aendern (1) -> Abbild (2) -> schreiben (weg); spaetere
        Aenderungen ueberleben das Schreiben."""
        sim = post("/api/simulator", {"action": "start", "channels": 4})
        self.assertTrue(sim["ok"])
        port = sim["port"]
        try:
            post("/api/radio/read", {"port": port, "out": "lc.atbin"})
            self.assertIsNone(wait_task()["error"])
            rows = get("/api/rows?kind=channels")["rows"]
            self.assertTrue(all(r["dirty"] == 0 for r in rows))

            anders = lambda r: "Low" if r["power"] != "Low" else "High"
            post("/api/row/save", {"kind": "channels", "index": 0,
                                   "row": {"power": anders(rows[0])}})
            self.assertEqual(get("/api/rows?kind=channels")["rows"][0]["dirty"], 1)

            applied = post("/api/image/apply", {"out": "lc-geaendert.atbin"})
            self.assertTrue(applied["ok"], applied)
            self.assertEqual(get("/api/rows?kind=channels")["rows"][0]["dirty"], 2)
            self.assertEqual(get("/api/state")["project"]["dirty_channels"], 1)

            # Aenderung nach dem Uebernehmen: Stufe 1, muss das Schreiben ueberleben.
            rows = get("/api/rows?kind=channels")["rows"]
            post("/api/row/save", {"kind": "channels", "index": 1,
                                   "row": {"power": anders(rows[1])}})

            post("/api/radio/write", {"port": port, "image": "lc-geaendert.atbin",
                                      "confirm": True})
            task = wait_task(timeout=120)
            self.assertIsNone(task["error"])
            self.assertEqual(task["result"]["cleared"], 1)
            rows = get("/api/rows?kind=channels")["rows"]
            self.assertEqual(rows[0]["dirty"], 0)
            self.assertEqual(rows[1]["dirty"], 1)
            self.assertEqual(get("/api/state")["project"]["dirty_channels"], 1)
        finally:
            post("/api/simulator", {"action": "stop"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
