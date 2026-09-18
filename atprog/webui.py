"""Grafische Oberflaeche: lokaler Webserver + Einseiten-Anwendung.

Bewusst ohne Fremdbibliotheken (kein Tk, kein Flask). Der Server laeuft nur auf
127.0.0.1 und haelt das aktuell geladene Projekt im Speicher. Lang laufende
Geraeteoperationen werden in einem Thread ausgefuehrt und per Fortschritts-
Abfrage an die Oberflaeche gemeldet.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import webbrowser
from dataclasses import asdict, is_dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from . import layout as layout_mod
from .csvio import export_all, import_all, import_dmr_id_database
from .device import (decode, field_write_allowed, patch_channels, patch_settings,
                     read_image, verify_layout, write_image)
from .image import MemoryImage
from .models import (
    Channel, Codeplug, RadioID, RxGroupList, ScanList, TalkGroup, Zone, fmt_freq, parse_freq,
)
from .protocol import Radio
from .serialport import describe_backend, list_ports
from .version import APP_TITLE, __version__

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


# ---------------------------------------------------------------------------
# Sitzungszustand
# ---------------------------------------------------------------------------

class Task:
    def __init__(self, name: str):
        self.name = name
        self.done = 0
        self.total = 1
        self.label = ""
        self.finished = False
        self.error: Optional[str] = None
        self.result: Dict[str, Any] = {}
        self.started = time.time()

    def as_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "done": self.done, "total": self.total,
                "label": self.label, "finished": self.finished, "error": self.error,
                "result": self.result, "seconds": round(time.time() - self.started, 1)}


class Session:
    def __init__(self, workdir: str):
        self.workdir = os.path.abspath(workdir)
        self.project = Codeplug(name="Neues Projekt")
        self.project_path: Optional[str] = None
        self.image: Optional[MemoryImage] = None
        self.image_path: Optional[str] = None
        self.layout = layout_mod.load()
        self.log: List[str] = []
        self.task: Optional[Task] = None
        self.sim = None
        self.lock = threading.Lock()
        # Rufzeichenliste: als Tupel gehalten, damit auch 300000 Eintraege
        # nicht zu viel Speicher kosten; dazu je Zeile ein kleingeschriebener
        # Suchtext, damit die Suche ohne Neuaufbau schnell bleibt.
        self.userdb_rows: List[tuple] = []
        self.userdb_hay: List[str] = []
        self.userdb_path: Optional[str] = None

    # -- Protokoll --------------------------------------------------------
    def set_userdb(self, entries, path: Optional[str] = None) -> int:
        """Rufzeichenliste uebernehmen.

        Ort, Region und Land wiederholen sich tausendfach - ``intern`` laesst
        alle Zeilen dieselbe Zeichenkette benutzen und spart so ein gutes
        Drittel des Speichers. Der Suchtext wird erst bei der ersten Suche
        gebaut (siehe ``search_index``).
        """
        intern = sys.intern
        rows = [(e.id, e.callsign, e.name,
                 intern(e.city or ""), intern(e.state or ""), intern(e.country or ""))
                for e in entries]
        self.userdb_rows, self.userdb_hay, self.userdb_path = rows, [], path
        return len(rows)

    def load_userdb_csv(self, path: str) -> int:
        """Liest eine Rufzeichenliste zeilenweise ein.

        Bewusst ohne den Umweg ueber Eintragsobjekte: bei 300000 Zeilen spart
        das mehrere hundert Megabyte Spitzenlast.
        """
        import csv as _csv
        intern = sys.intern
        rows: List[tuple] = []
        for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
            try:
                with open(path, "r", encoding=encoding, newline="") as fh:
                    reader = _csv.reader(fh)
                    header = [h.strip().upper().replace(" ", "_") for h in next(reader, [])]
                    pos = {name: header.index(name) for name in
                           ("RADIO_ID", "CALLSIGN", "NAME", "FIRST_NAME", "LAST_NAME",
                            "CITY", "STATE", "COUNTRY") if name in header}
                    id_at = pos.get("RADIO_ID", 0)
                    for row in reader:
                        if len(row) <= id_at or not row[id_at].strip().isdigit():
                            continue
                        def cell(key: str) -> str:
                            i = pos.get(key, -1)
                            return row[i].strip() if 0 <= i < len(row) else ""
                        name = cell("NAME") or " ".join(
                            p for p in (cell("FIRST_NAME"), cell("LAST_NAME")) if p)
                        rows.append((int(row[id_at]), cell("CALLSIGN"), name,
                                     intern(cell("CITY")), intern(cell("STATE")),
                                     intern(cell("COUNTRY"))))
                break
            except UnicodeDecodeError:
                rows = []
                continue
        self.userdb_rows, self.userdb_hay, self.userdb_path = rows, [], path
        return len(rows)

    def search_index(self) -> List[str]:
        """Kleingeschriebener Suchtext je Zeile, beim ersten Aufruf gebaut."""
        if len(self.userdb_hay) != len(self.userdb_rows):
            self.userdb_hay = [(" ".join((str(r[0]), r[1], r[2], r[3], r[4], r[5]))).lower()
                               for r in self.userdb_rows]
        return self.userdb_hay

    def say(self, message: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.log.append("%s  %s" % (stamp, message))
        del self.log[:-400]

    # -- Hintergrundaufgaben ---------------------------------------------
    def start_task(self, name: str, fn: Callable[[Task], Dict[str, Any]]) -> Dict[str, Any]:
        if self.task and not self.task.finished:
            return {"ok": False, "error": "Es laeuft bereits: %s" % self.task.name}
        task = Task(name)
        self.task = task

        def runner():
            try:
                task.result = fn(task) or {}
            except Exception as exc:
                task.error = str(exc)
                self.say("Fehler in %s: %s" % (name, exc))
            finally:
                task.finished = True
        threading.Thread(target=runner, daemon=True).start()
        return {"ok": True, "task": task.as_dict()}

    def path(self, name: str) -> str:
        name = os.path.expanduser(name)
        return name if os.path.isabs(name) else os.path.join(self.workdir, name)


# ---------------------------------------------------------------------------
# Umwandlung Modell <-> JSON-Zeilen
# ---------------------------------------------------------------------------

def channel_row(ch: Channel, index: int) -> Dict[str, Any]:
    return {"i": index, "name": ch.name, "rx": fmt_freq(ch.rx_freq), "tx": fmt_freq(ch.tx_freq),
            "ch_type": ch.ch_type, "power": ch.power, "bandwidth": ch.bandwidth,
            "color_code": ch.color_code, "slot": ch.slot, "contact": ch.contact,
            "radio_id": ch.radio_id, "rx_group": ch.rx_group, "scan_list": ch.scan_list,
            "ctcss_decode": ch.ctcss_decode, "ctcss_encode": ch.ctcss_encode,
            "tx_permit": ch.tx_permit, "squelch_mode": ch.squelch_mode,
            "ptt_prohibit": ch.ptt_prohibit, "talkaround": ch.talkaround,
            "reverse": ch.reverse,
            "dirty": int(ch.extra.get("_dirty") or 0)}


def mark_dirty(ch: Channel) -> None:
    """Merkt einen Kanal als geaendert, solange die Aenderung noch nicht im
    Geraet ist. Stufen: "1" = nur im Projekt, "2" = ins Abbild uebernommen
    (siehe _image_apply/_radio_write). Der Unterstrich haelt den Marker aus
    dem CSV-Export heraus."""
    ch.extra["_dirty"] = "1"


def apply_channel(ch: Channel, row: Dict[str, Any]) -> Channel:
    ch.name = str(row.get("name", ch.name))[:16]
    if "rx" in row:
        ch.rx_freq = parse_freq(row["rx"])
    if "tx" in row:
        ch.tx_freq = parse_freq(row["tx"])
    for key in ("ch_type", "power", "bandwidth", "contact", "radio_id", "rx_group",
                "scan_list", "ctcss_decode", "ctcss_encode", "tx_permit", "squelch_mode",
                "ptt_prohibit", "talkaround", "reverse"):
        if key in row:
            setattr(ch, key, str(row[key]))
    for key in ("color_code", "slot"):
        if key in row:
            try:
                setattr(ch, key, int(row[key]))
            except (TypeError, ValueError):
                pass
    return ch


ROWS = {
    "channels": (lambda cp: cp.channels, channel_row),
    "talkgroups": (lambda cp: cp.talkgroups,
                   lambda t, i: {"i": i, "name": t.name, "tg_id": t.tg_id,
                                 "call_type": t.call_type, "call_alert": t.call_alert}),
    "zones": (lambda cp: cp.zones,
              lambda z, i: {"i": i, "name": z.name, "channels": z.channels,
                            "count": len(z.channels), "a_channel": z.a_channel,
                            "b_channel": z.b_channel}),
    "radio_ids": (lambda cp: cp.radio_ids,
                  lambda r, i: {"i": i, "name": r.name, "radio_id": r.radio_id}),
    "scanlists": (lambda cp: cp.scanlists,
                  lambda s, i: {"i": i, "name": s.name, "channels": s.channels,
                                "count": len(s.channels)}),
    "rxgroups": (lambda cp: cp.rxgroups,
                 lambda g, i: {"i": i, "name": g.name, "contacts": g.contacts,
                               "count": len(g.contacts)}),
}


# ---------------------------------------------------------------------------
# HTTP-Schnittstelle
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "atprog/" + __version__
    session: Session = None            # wird beim Start gesetzt

    def log_message(self, fmt, *args):      # Standardlogging unterdruecken
        pass

    # -- Hilfsfunktionen --------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, data: Any, code: int = 200) -> None:
        self._send(code, json.dumps(data, ensure_ascii=False, default=_default).encode("utf-8"))

    def _body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except ValueError:
            return {}

    # -- Routen -----------------------------------------------------------
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        route, query = parsed.path, parse_qs(parsed.query)
        if route in ("/", "/index.html"):
            return self._file("index.html", "text/html; charset=utf-8")
        if route == "/app.js":
            return self._file("app.js", "application/javascript; charset=utf-8")
        if route == "/app.css":
            return self._file("app.css", "text/css; charset=utf-8")
        if route == "/api/state":
            return self._json(self._state())
        if route == "/api/task":
            task = self.session.task
            return self._json(task.as_dict() if task else {"finished": True, "name": ""})
        if route == "/api/rows":
            return self._json(self._rows(query))
        if route == "/api/userdb/rows":
            return self._json(self._userdb_rows(query))
        if route == "/api/files":
            return self._json(self._files(query.get("dir", [""])[0]))
        return self._json({"error": "Unbekannte Route %s" % route}, 404)

    def do_POST(self) -> None:
        route = urlparse(self.path).path
        body = self._body()
        handlers = {
            "/api/row/save": self._row_save,
            "/api/rows/save": self._rows_save,
            "/api/row/add": self._row_add,
            "/api/row/delete": self._row_delete,
            "/api/project/new": self._project_new,
            "/api/project/open": self._project_open,
            "/api/project/save": self._project_save,
            "/api/csv/import": self._csv_import,
            "/api/csv/export": self._csv_export,
            "/api/csv/userdb": self._csv_userdb,
            "/api/validate": self._validate,
            "/api/settings/aprs": self._settings_aprs,
            "/api/radio/info": self._radio_info,
            "/api/radio/read": self._radio_read,
            "/api/radio/write": self._radio_write,
            "/api/image/open": self._image_open,
            "/api/image/decode": self._image_decode,
            "/api/image/apply": self._image_apply,
            "/api/userdb/info": self._userdb_info,
            "/api/userdb/load": self._userdb_load,
            "/api/userdb/fetch": self._userdb_fetch,
            "/api/userdb/verify": self._userdb_verify,
            "/api/userdb/export": self._userdb_export,
            "/api/userdb/write": self._userdb_write,
            "/api/simulator": self._simulator,
        }
        fn = handlers.get(route)
        if not fn:
            return self._json({"error": "Unbekannte Route %s" % route}, 404)
        try:
            return self._json(fn(body))
        except Exception as exc:
            self.session.say("Fehler: %s" % exc)
            return self._json({"ok": False, "error": str(exc)}, 200)

    def _file(self, name: str, ctype: str) -> None:
        path = os.path.join(WEB_DIR, name)
        try:
            with open(path, "rb") as fh:
                self._send(200, fh.read(), ctype)
        except OSError:
            self._send(404, b"not found", "text/plain")

    # -- Zustand ----------------------------------------------------------
    def _state(self) -> Dict[str, Any]:
        s = self.session
        ports = list_ports()
        sim_port = getattr(s.sim, "slave_name", None) if s.sim else None
        if sim_port and sim_port not in ports:
            ports.append(sim_port)          # virtuelles Geraet mit anbieten
        return {
            "app": {"title": APP_TITLE, "version": __version__, "backend": describe_backend()},
            "project": {"name": s.project.name, "path": s.project_path,
                        "stats": s.project.stats(),
                        "dirty_channels": sum(1 for c in s.project.channels
                                              if c.extra.get("_dirty"))},
            "image": {"path": s.image_path,
                      "size": s.image.size() if s.image else 0,
                      "meta": s.image.meta if s.image else {}},
            "layout": {"id": s.layout.id, "title": s.layout.title,
                       "revision": s.layout.revision, "revised": s.layout.revised},
            "ports": ports,
            "workdir": s.workdir,
            "log": s.log[-120:],
            "task": s.task.as_dict() if s.task else None,
            "simulator": sim_port,
            "userdb": {"path": s.userdb_path, "count": len(s.userdb_rows)},
        }

    def _rows(self, query: Dict[str, List[str]]) -> Dict[str, Any]:
        kind = query.get("kind", ["channels"])[0]
        needle = query.get("q", [""])[0].strip().lower()
        offset = int(query.get("offset", ["0"])[0])
        limit = min(int(query.get("limit", ["200"])[0]), 2000)
        getter, to_row = ROWS.get(kind, ROWS["channels"])
        items = getter(self.session.project)
        rows = [to_row(item, i) for i, item in enumerate(items)]
        if needle:
            rows = [r for r in rows
                    if any(needle in str(v).lower() for v in r.values())]
        return {"kind": kind, "total": len(rows), "rows": rows[offset:offset + limit],
                "offset": offset}

    def _userdb_rows(self, query: Dict[str, List[str]]) -> Dict[str, Any]:
        """Blaettern und Suchen in der geladenen Rufzeichenliste."""
        s = self.session
        needle = query.get("q", [""])[0].strip().lower()
        offset = max(0, int(query.get("offset", ["0"])[0]))
        limit = min(int(query.get("limit", ["200"])[0]), 1000)
        rows = s.userdb_rows
        if needle:
            terms = needle.split()
            index = s.search_index()
            hits = [i for i, hay in enumerate(index) if all(t in hay for t in terms)]
            total = len(hits)
            chosen = [rows[i] for i in hits[offset:offset + limit]]
        else:
            total = len(rows)
            chosen = rows[offset:offset + limit]
        return {"total": total, "offset": offset, "path": s.userdb_path,
                "rows": [{"id": r[0], "callsign": r[1], "name": r[2], "city": r[3],
                          "state": r[4], "country": r[5]} for r in chosen]}

    def _files(self, directory: str) -> Dict[str, Any]:
        base = self.session.path(directory or ".")
        try:
            entries = sorted(os.listdir(base))
        except OSError as exc:
            return {"ok": False, "error": str(exc), "dir": base, "entries": []}
        out = []
        for name in entries:
            if name.startswith("."):
                continue
            full = os.path.join(base, name)
            out.append({"name": name, "dir": os.path.isdir(full),
                        "size": os.path.getsize(full) if os.path.isfile(full) else 0})
        return {"ok": True, "dir": base, "entries": out}

    # -- Zeilen bearbeiten -------------------------------------------------
    def _row_save(self, body: Dict[str, Any]) -> Dict[str, Any]:
        kind, index, row = body.get("kind"), int(body.get("index", -1)), body.get("row") or {}
        cp = self.session.project
        items = ROWS[kind][0](cp)
        if not 0 <= index < len(items):
            return {"ok": False, "error": "Zeile %d existiert nicht" % index}
        item = items[index]
        followed = 0
        if kind == "channels":
            old_name = item.name
            before = channel_row(item, index)
            apply_channel(item, row)
            if channel_row(item, index) != before:
                mark_dirty(item)
            if item.name != old_name:
                followed = cp.rename_channel(old_name, item.name)
        elif kind == "talkgroups":
            old_name = item.name
            item.name = str(row.get("name", item.name))[:16]
            if item.name != old_name:
                followed = cp.rename_talkgroup(old_name, item.name)
            item.tg_id = int(row.get("tg_id") or 0)
            item.call_type = str(row.get("call_type", item.call_type))
            item.call_alert = str(row.get("call_alert", item.call_alert))
        elif kind == "radio_ids":
            item.name = str(row.get("name", item.name))[:16]
            item.radio_id = int(row.get("radio_id") or 0)
        elif kind in ("zones", "scanlists"):
            item.name = str(row.get("name", item.name))[:16]
            if "channels" in row:
                item.channels = _as_list(row["channels"])
            if kind == "zones":
                item.a_channel = str(row.get("a_channel", item.a_channel))
                item.b_channel = str(row.get("b_channel", item.b_channel))
        elif kind == "rxgroups":
            item.name = str(row.get("name", item.name))[:16]
            if "contacts" in row:
                item.contacts = _as_list(row["contacts"])
        self.session.say("%s [%d] geaendert%s"
                         % (kind, index,
                            " (%d Verweise nachgefuehrt)" % followed if followed else ""))
        return {"ok": True, "followed": followed}

    def _rows_save(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """Setzt dieselben Felder fuer mehrere Kanaele auf einmal."""
        if body.get("kind") != "channels":
            return {"ok": False, "error": "Sammelaenderung gibt es nur fuer Kanaele"}
        row = dict(body.get("row") or {})
        # name, rx und tx sind je Kanal individuell. name muss zudem raus,
        # weil apply_channel es sonst immer setzt und so Duplikate entstuenden.
        for key in ("name", "rx", "tx"):
            row.pop(key, None)
        if not row:
            return {"ok": False, "error": "Keine Felder angegeben"}
        indexes = sorted({int(i) for i in (body.get("indexes") or [])})
        if not indexes:
            return {"ok": False, "error": "Keine Zeilen angegeben"}
        items = self.session.project.channels
        changed = skipped = 0
        for idx in indexes:
            if not 0 <= idx < len(items):
                skipped += 1
                continue
            ch = items[idx]
            before = channel_row(ch, idx)
            apply_channel(ch, row)
            if channel_row(ch, idx) != before:
                mark_dirty(ch)
                changed += 1
        self.session.say("channels: %d Kanaele gemeinsam geaendert (%s)"
                         % (changed, ", ".join(sorted(row))))
        return {"ok": True, "changed": changed, "skipped": skipped}

    def _row_add(self, body: Dict[str, Any]) -> Dict[str, Any]:
        kind, row = body.get("kind"), body.get("row") or {}
        cp = self.session.project
        if kind == "channels":
            ch = apply_channel(Channel(), row)
            if not ch.name:
                ch.name = "Kanal %d" % (len(cp.channels) + 1)
            mark_dirty(ch)
            cp.channels.append(ch)
        elif kind == "talkgroups":
            cp.talkgroups.append(TalkGroup(name=str(row.get("name", "TG"))[:16],
                                           tg_id=int(row.get("tg_id") or 0),
                                           call_type=str(row.get("call_type", "Group Call"))))
        elif kind == "radio_ids":
            cp.radio_ids.append(RadioID(name=str(row.get("name", "ID"))[:16],
                                        radio_id=int(row.get("radio_id") or 0)))
        elif kind == "zones":
            cp.zones.append(Zone(name=str(row.get("name", "Zone"))[:16],
                                 channels=_as_list(row.get("channels"))))
        elif kind == "scanlists":
            cp.scanlists.append(ScanList(name=str(row.get("name", "Scan"))[:16],
                                         channels=_as_list(row.get("channels"))))
        elif kind == "rxgroups":
            cp.rxgroups.append(RxGroupList(name=str(row.get("name", "Gruppe"))[:16],
                                           contacts=_as_list(row.get("contacts"))))
        else:
            return {"ok": False, "error": "Unbekannte Liste %r" % kind}
        self.session.say("%s: Eintrag hinzugefuegt" % kind)
        return {"ok": True}

    def _row_delete(self, body: Dict[str, Any]) -> Dict[str, Any]:
        kind = body.get("kind")
        indexes = sorted({int(i) for i in (body.get("indexes") or [body.get("index", -1)])},
                         reverse=True)
        items = ROWS[kind][0](self.session.project)
        removed = 0
        for idx in indexes:
            if 0 <= idx < len(items):
                items.pop(idx)
                removed += 1
        self.session.say("%s: %d Eintraege geloescht" % (kind, removed))
        return {"ok": True, "removed": removed}

    # -- Projekt ----------------------------------------------------------
    def _project_new(self, body: Dict[str, Any]) -> Dict[str, Any]:
        self.session.project = Codeplug(name=str(body.get("name") or "Neues Projekt"))
        self.session.project_path = None
        self.session.say("Neues Projekt angelegt")
        return {"ok": True}

    def _project_open(self, body: Dict[str, Any]) -> Dict[str, Any]:
        path = self.session.path(str(body.get("path") or ""))
        self.session.project = Codeplug.load(path)
        self.session.project_path = path
        self.session.say("Projekt geladen: %s" % path)
        return {"ok": True, "stats": self.session.project.stats()}

    def _project_save(self, body: Dict[str, Any]) -> Dict[str, Any]:
        path = self.session.path(str(body.get("path") or self.session.project_path or "codeplug.json"))
        if not path.endswith(".json"):
            path += ".json"
        self.session.project.save(path)
        self.session.project_path = path
        self.session.say("Projekt gespeichert: %s" % path)
        return {"ok": True, "path": path}

    def _csv_import(self, body: Dict[str, Any]) -> Dict[str, Any]:
        directory = self.session.path(str(body.get("dir") or "."))
        cp, loaded = import_all(directory)
        if not loaded:
            return {"ok": False, "error": "Keine CPS-CSV-Dateien in %s gefunden" % directory}
        cp.name = self.session.project.name
        self.session.project = cp
        self.session.say("CSV importiert (%s): %s" % (directory, ", ".join(loaded)))
        return {"ok": True, "loaded": loaded, "stats": cp.stats()}

    def _csv_export(self, body: Dict[str, Any]) -> Dict[str, Any]:
        directory = self.session.path(str(body.get("dir") or "csv"))
        files = export_all(directory, self.session.project)
        self.session.say("CSV exportiert nach %s" % directory)
        return {"ok": True, "dir": directory, "files": [os.path.basename(f) for f in files]}

    def _csv_userdb(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """Nutzerliste von radioid.net als Einzelruf-Kontakte uebernehmen."""
        from .models import MAX_TALKGROUPS
        cp = self.session.project
        path = self.session.path(str(body.get("path") or ""))
        room = MAX_TALKGROUPS - len(cp.talkgroups)
        if room <= 0:
            return {"ok": False, "error": "Die Kontaktliste ist voll (%d Eintraege)"
                                          % len(cp.talkgroups)}
        wanted = int(body.get("limit") or 0)
        limit = min(wanted, room) if wanted else room
        before = len(cp.talkgroups)
        added = import_dmr_id_database(path, cp, limit=limit,
                                       country=str(body.get("country") or ""))
        skipped = max(0, len(cp.talkgroups) - before - added)
        self.session.say("DMR-ID-Liste: %d Kontakte ergaenzt (Platz fuer %d)" % (added, room))
        return {"ok": True, "added": added, "skipped": skipped,
                "total": len(cp.talkgroups), "room": room}

    def _validate(self, body: Dict[str, Any]) -> Dict[str, Any]:
        issues = self.session.project.validate()
        return {"ok": True,
                "errors": [str(i) for i in issues if i.level == "error"],
                "warnings": [str(i) for i in issues if i.level == "warning"]}

    def _settings_aprs(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """APRS-Einstellungen lesen (ohne Nutzlast) oder aendern."""
        from .models import parse_freq
        s = self.session
        aprs = dict((s.project.settings or {}).get("aprs") or {})
        changes = body.get("values")
        if changes:
            for key in ("source", "destination", "path", "symbol_table", "symbol",
                        "message"):
                if key in changes:
                    aprs[key] = str(changes[key])[:20 if key == "path" else 60]
            for key in ("source_ssid", "destination_ssid", "tx_delay",
                        "manual_interval", "auto_interval"):
                if key in changes:
                    try:
                        aprs[key] = int(changes[key])
                    except (TypeError, ValueError):
                        pass
            if "fm_frequency" in changes:
                aprs["fm_frequency"] = parse_freq(changes["fm_frequency"])
            if "fm_power" in changes:
                aprs["fm_power"] = str(changes["fm_power"])
            s.project.settings["aprs"] = aprs
            s.say("APRS-Einstellungen geaendert")
        return {"ok": True, "aprs": aprs}

    # -- Geraet -----------------------------------------------------------
    def _radio_info(self, body: Dict[str, Any]) -> Dict[str, Any]:
        port = body.get("port") or (list_ports() or [None])[0]
        if not port:
            return {"ok": False, "error": "Kein serieller Port gefunden"}
        radio = Radio(port, log=self.session.say)
        try:
            info = radio.open()
            lay = layout_mod.load_for(info.model)
            self.session.layout = lay
            self.session.say("Verbunden mit %s auf %s" % (info, port))
            return {"ok": True, "model": info.model, "version": info.version,
                    "layout": lay.id, "matches": lay.matches(info.model)}
        finally:
            radio.close()

    def _radio_read(self, body: Dict[str, Any]) -> Dict[str, Any]:
        s = self.session
        port = body.get("port") or (list_ports() or [None])[0]
        out = s.path(str(body.get("out") or "backup-%s.atbin" % time.strftime("%Y%m%d-%H%M%S")))
        full = bool(body.get("full"))
        if not port:
            return {"ok": False, "error": "Kein serieller Port gefunden"}

        def job(task: Task) -> Dict[str, Any]:
            radio = Radio(port, log=s.say)
            try:
                info = radio.open()
                lay = layout_mod.load_for(info.model)
                s.layout = lay

                def prog(done, total, label):
                    task.done, task.total, task.label = done, total, label
                img = read_image(radio, lay, smart=not full, progress=prog, log=s.say)
            finally:
                radio.close()
            img.save(out)
            s.image, s.image_path = img, out
            checks = verify_layout(img, s.layout)
            s.project = decode(img, s.layout, log=s.say)
            s.project.name = os.path.basename(out)
            s.say("Codeplug gesichert: %s (%d Byte)" % (out, img.size()))
            return {"path": out, "bytes": img.size(), "stats": s.project.stats(),
                    "checks": [c.line() for c in checks]}
        return s.start_task("Codeplug sichern", job)

    def _radio_write(self, body: Dict[str, Any]) -> Dict[str, Any]:
        s = self.session
        port = body.get("port")          # beim Schreiben nie automatisch waehlen
        image_path = s.path(str(body.get("image") or s.image_path or ""))
        if not port:
            return {"ok": False,
                    "error": "Bitte den Port ausdruecklich angeben - beim Schreiben "
                             "wird kein Port automatisch gewaehlt."}
        if not image_path or not os.path.exists(image_path):
            return {"ok": False, "error": "Abbilddatei fehlt: %s" % image_path}
        if not body.get("confirm"):
            return {"ok": False, "error": "Schreiben nicht bestaetigt"}
        target = MemoryImage.load(image_path)

        def job(task: Task) -> Dict[str, Any]:
            radio = Radio(port, log=s.say)
            try:
                radio.open()

                def prog(done, total, label):
                    task.done, task.total, task.label = done, total, label
                backup = s.path("auto-backup-%s.atbin" % time.strftime("%Y%m%d-%H%M%S"))
                s.say("Sicherheitskopie vor dem Schreiben ...")
                # WICHTIG: Der Vergleichsstand muss genau die Bereiche des Ziels
                # abdecken. Eine verkuerzte Sicherung (smart) laesst alles
                # Fehlende als "geaendert" erscheinen - dann wird der komplette
                # Codeplug geschrieben statt nur der Unterschiede.
                before = read_image(radio, s.layout, smart=False, progress=prog, log=s.say)
                before.save(backup)
                s.say("Sicherheitskopie: %s" % backup)
                report = write_image(radio, target, differential=True, verify=True,
                                     progress=prog, log=s.say, current=before)
            finally:
                radio.close()
            # Nur wenn das zuletzt uebernommene Abbild geschrieben wurde, sind
            # die Aenderungen der Stufe "2" jetzt im Geraet. Spaeter erneut
            # geaenderte Kanaele stehen wieder auf "1" und bleiben markiert.
            cleared = 0
            if os.path.abspath(image_path) == os.path.abspath(s.image_path or ""):
                with s.lock:
                    for ch in s.project.channels:
                        if ch.extra.get("_dirty") == "2":
                            ch.extra.pop("_dirty", None)
                            cleared += 1
            if cleared:
                s.say("%d Kanaele sind jetzt im Geraet" % cleared)
            s.say("Schreiben beendet: %s" % report.summary())
            return {"backup": backup, "summary": report.summary(), "cleared": cleared,
                    "mismatches": ["0x%08X" % a for a in report.mismatches[:20]]}
        return s.start_task("Codeplug schreiben", job)

    def _image_open(self, body: Dict[str, Any]) -> Dict[str, Any]:
        path = self.session.path(str(body.get("path") or ""))
        img = MemoryImage.load(path)
        self.session.image, self.session.image_path = img, path
        checks = verify_layout(img, self.session.layout)
        self.session.say("Abbild geladen: %s (%d Byte)" % (path, img.size()))
        return {"ok": True, "bytes": img.size(), "meta": img.meta,
                "checks": [c.line() for c in checks]}

    def _image_decode(self, body: Dict[str, Any]) -> Dict[str, Any]:
        if not self.session.image:
            return {"ok": False, "error": "Kein Abbild geladen"}
        cp = decode(self.session.image, self.session.layout, log=self.session.say)
        cp.name = self.session.project.name
        self.session.project = cp
        return {"ok": True, "stats": cp.stats()}

    def _image_apply(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """Kanalaenderungen aus den Tabellen in das geladene Abbild uebernehmen."""
        s = self.session
        if not s.image:
            return {"ok": False, "error": "Kein Abbild geladen"}
        allowed, reason = field_write_allowed(s.image, s.layout)
        if not allowed:
            return {"ok": False, "error": reason}
        known = [c for c in s.project.channels if (c.extra or {}).get("_index") is not None]
        if not known:
            return {"ok": False, "error": "Die Kanaele stammen nicht aus einem Abbild - "
                                          "bitte zuerst aus dem Geraet lesen."}
        errors = [str(i) for i in s.project.validate() if i.level == "error"]
        if errors:
            return {"ok": False, "error": "Bitte zuerst die %d Fehler beheben (Prüfen)."
                                          % len(errors)}
        patched = patch_settings(patch_channels(s.image, s.project, s.layout, log=s.say),
                                 s.project, s.layout, log=s.say)
        spots = len(s.image.diff(patched))
        out = s.path(str(body.get("out") or "geaendert.atbin"))
        patched.save(out)
        s.image, s.image_path = patched, out
        # Diese Kanaele sind jetzt im Abbild, aber noch nicht im Geraet.
        # Erst ein erfolgreiches Schreiben dieses Abbilds loescht Stufe "2";
        # Kanaele ohne _index kann patch_channels nicht uebernehmen.
        with s.lock:
            for ch in s.project.channels:
                if ch.extra.get("_dirty") == "1" and ch.extra.get("_index") is not None:
                    ch.extra["_dirty"] = "2"
        s.say("Aenderungen uebernommen: %d Stellen -> %s" % (spots, out))
        return {"ok": True, "path": out, "spots": spots, "reason": reason}

    # -- Rufzeichendatenbank ---------------------------------------------
    def _userdb_port(self, body: Dict[str, Any], writing: bool = False) -> Optional[str]:
        """Port bestimmen.

        Fuer schreibende Vorgaenge wird der Port ausdruecklich verlangt: sonst
        koennte eine Anfrage ohne Portangabe stillschweigend auf einem anderen
        angeschlossenen Geraet landen.
        """
        port = body.get("port")
        if port:
            return str(port)
        if writing:
            return None
        return (list_ports() or [None])[0]

    def _userdb_info(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """Kurzer Blick ins Geraet: Anzahl, Kodierung, Beispieleintraege."""
        from .userdb import probe, sample_entries
        s = self.session
        port = self._userdb_port(body)
        if not port:
            return {"ok": False, "error": "Kein serieller Port gefunden"}
        radio = Radio(port, log=s.say)
        try:
            radio.open()
            status = probe(radio, str(body.get("geometry") or ""), log=s.say)
            entries = sample_entries(radio, status, int(body.get("samples") or 6))
        finally:
            radio.close()
        s.say("Rufzeichendatenbank: %s" % status.summary())
        return {"ok": True, "count": status.count, "geometry": status.geometry.name,
                "encoding": ("BCD" if status.bcd_id else
                             "binaer" if status.bcd_id is False else "unbekannt"),
                "readable": status.readable,
                "entries": [{"id": e.id, "callsign": e.callsign, "name": e.name,
                             "city": e.city, "country": e.country} for e in entries]}

    def _userdb_load(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """Eine gesicherte Rufzeichenliste (CSV) zum Ansehen laden."""
        s = self.session
        path = s.path(str(body.get("path") or ""))
        if not os.path.exists(path):
            return {"ok": False, "error": "Datei nicht gefunden: %s" % path}
        n = s.load_userdb_csv(path)
        s.say("Rufzeichenliste geladen: %s (%d Eintraege)" % (path, n))
        return {"ok": True, "count": n, "path": path}

    def _userdb_verify(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """Datenbank im Geraet stichprobenweise auf Vollstaendigkeit pruefen."""
        from .userdb import probe, verify_on_radio
        s = self.session
        port = self._userdb_port(body)
        if not port:
            return {"ok": False, "error": "Kein serieller Port gefunden"}
        samples = int(body.get("samples") or 40)

        def job(task: Task) -> Dict[str, Any]:
            radio = Radio(port, log=s.say)
            try:
                radio.open()
                status = probe(radio, "", log=s.say)

                def prog(done, total, label):
                    task.done, task.total, task.label = done, total, label
                result = verify_on_radio(radio, status, samples=samples, progress=prog)
            finally:
                radio.close()
            result["count"] = status.count
            s.say("Pruefung: %d von %d Stichproben in Ordnung"
                  % (result["ok"], result["checked"]))
            return result
        return s.start_task("Datenbank pruefen", job)

    def _userdb_fetch(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """Aktuelle Nutzerliste von radioid.net holen (Hintergrundaufgabe)."""
        from .userdb import RADIOID_URL, fetch_user_database
        s = self.session
        out = s.path(str(body.get("out") or "radioid-users.json"))
        url = str(body.get("url") or RADIOID_URL)

        def job(task: Task) -> Dict[str, Any]:
            def prog(done, total, label):
                task.done, task.total, task.label = done, total, label
            size = fetch_user_database(out, url=url, progress=prog)
            n = s.load_userdb_csv(out) if out.lower().endswith(".csv") else 0
            if not n:
                from .userdb import iter_json_users
                s.set_userdb(list(iter_json_users(out)), out)
                n = len(s.userdb_rows)
            s.say("radioid.net: %.1f MB geladen, %d Eintraege" % (size / 1048576.0, n))
            return {"path": out, "bytes": size, "entries": n}
        return s.start_task("Liste von radioid.net laden", job)

    def _userdb_export(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """Datenbank aus dem Geraet in eine CSV sichern (Hintergrundaufgabe)."""
        from .userdb import probe, read_from_radio, write_user_csv
        s = self.session
        port = self._userdb_port(body)
        out = s.path(str(body.get("out") or "rufzeichen.csv"))
        limit = int(body.get("limit") or 0)
        if not port:
            return {"ok": False, "error": "Kein serieller Port gefunden"}

        def job(task: Task) -> Dict[str, Any]:
            radio = Radio(port, log=s.say)
            try:
                radio.open()
                status = probe(radio, "", log=s.say)
                if not status.readable:
                    raise RuntimeError("Es ist keine lesbare Datenbank geladen")

                def prog(done, total, label):
                    task.done, task.total, task.label = done, total, label
                entries = read_from_radio(radio, status, limit=limit, progress=prog)
            finally:
                radio.close()
            written = write_user_csv(out, entries)
            s.set_userdb(entries, out)      # gleich zum Ansehen bereitstellen
            s.say("Rufzeichendatenbank gesichert: %s (%d Eintraege)" % (out, written))
            return {"path": out, "entries": written, "count": status.count}
        return s.start_task("Rufzeichendatenbank auslesen", job)

    def _userdb_write(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """CSV einlesen, Datenbank bauen und ins Geraet schreiben."""
        from .userdb import UserDatabase, read_user_file, write_to_radio, probe
        s = self.session
        port = self._userdb_port(body, writing=True)
        csv_path = s.path(str(body.get("csv") or ""))
        country = str(body.get("country") or "")
        prefixes = [p.strip() for p in str(body.get("prefix") or "").split(",") if p.strip()]
        limit = int(body.get("limit") or 0)
        if not port:
            return {"ok": False,
                    "error": "Bitte den Port ausdruecklich angeben - beim Schreiben "
                             "wird kein Port automatisch gewaehlt."}
        if not os.path.exists(csv_path):
            return {"ok": False, "error": "Datei nicht gefunden: %s" % csv_path}
        if not body.get("confirm"):
            return {"ok": False, "error": "Schreiben nicht bestaetigt"}
        entries = read_user_file(csv_path, country=country, prefixes=prefixes, limit=limit)
        if not entries:
            return {"ok": False, "error": "Keine passenden Eintraege in %s" % csv_path}

        def job(task: Task) -> Dict[str, Any]:
            radio = Radio(port, log=s.say)
            try:
                radio.open()
                status = probe(radio, "", log=s.say)

                def prog(done, total, label):
                    task.done, task.total, task.label = done, total, label
                db = UserDatabase(entries=entries, geometry=status.geometry)
                img = db.build(progress=prog)
                s.say("%d Eintraege, %.1f MB werden geschrieben - die Restzeit "
                      "steht im Fortschrittsbalken, sobald sie messbar ist"
                      % (len(entries), img.size() / 1048576.0))
                report = write_to_radio(radio, img, status, progress=prog, log=s.say)
            finally:
                radio.close()
            s.say("Rufzeichendatenbank geschrieben: %d Eintraege" % report["count"])
            return {"count": report["count"], "stale_cleared": report["stale_cleared"],
                    "header_ok": report["header_ok"], "verified": report["verified"],
                    "before": status.count,
                    "samples": [{"id": e.id, "callsign": e.callsign, "name": e.name}
                                for e in report["samples"]]}
        return s.start_task("Rufzeichendatenbank schreiben", job)

    def _simulator(self, body: Dict[str, Any]) -> Dict[str, Any]:
        from .simulator import SimulatedRadio, populate_demo
        s = self.session
        if body.get("action") == "stop":
            if s.sim:
                s.sim.stop()
                s.sim = None
                s.say("Simulator beendet")
            return {"ok": True, "port": None}
        if s.sim:
            return {"ok": True, "port": s.sim.slave_name}
        sim = SimulatedRadio()
        port = sim.start()
        populate_demo(sim, channels=int(body.get("channels") or 8))
        s.sim = sim
        s.say("Simuliertes Funkgeraet auf %s" % port)
        return {"ok": True, "port": port}


def _as_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [p.strip() for p in str(value or "").split("|") if p.strip()]


def _default(obj: Any) -> Any:
    if is_dataclass(obj):
        return asdict(obj)
    return str(obj)


def serve(host: str = "127.0.0.1", port: int = 8787, open_browser: bool = True,
          workdir: Optional[str] = None) -> None:
    Handler.session = Session(workdir or os.getcwd())
    Handler.session.say("%s %s gestartet" % (APP_TITLE, __version__))
    httpd = ThreadingHTTPServer((host, port), Handler)
    url = "http://%s:%d/" % (host, port)
    print("%s %s" % (APP_TITLE, __version__))
    print("Oberflaeche: %s   (Arbeitsverzeichnis: %s)" % (url, Handler.session.workdir))
    print("Beenden mit Strg-C.")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nBeendet.")
    finally:
        if Handler.session.sim:
            Handler.session.sim.stop()
        httpd.server_close()
