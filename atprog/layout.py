"""Laden und Auswerten der Adressplan-Beschreibung."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

LAYOUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "layouts")
USER_DIR = os.path.join(os.path.expanduser("~"), ".atprog")
DEFAULT_LAYOUT = "d878uv2plus"


def _num(value: Any) -> int:
    if isinstance(value, int):
        return value
    return int(str(value), 0)


@dataclass
class Region:
    name: str
    addr: int
    size: int
    confidence: str = "medium"
    desc: str = ""
    invert: bool = False        # Bitmaps mancher Bereiche sind invertiert
    einstufung: str = "gesichert"   # ausgewertet | gesichert
    zweck: str = ""

    @property
    def end(self) -> int:
        return self.addr + self.size


@dataclass
class Field:
    name: str
    type: str
    offset: int
    length: int = 0
    shift: int = 0
    width: int = 0
    scale: int = 1
    count: int = 0
    item: str = "u16"
    empty: int = 0xFFFF
    enum: Optional[List[str]] = None
    confidence: str = "medium"
    desc: str = ""


@dataclass
class ObjectDef:
    key: str
    title: str
    count: int
    record_size: int
    base: int
    stride: int
    per_bank: int
    bitmap: Optional[str]
    fields: List[Field]
    marker_stride: int = 0      # Flash-Marken am Blockende
    marker_size: int = 0

    def hits_marker(self, index: int) -> bool:
        """Liegt der Datensatz auf einer Flash-Marke (also kein Nutzdatensatz)?"""
        if not self.marker_stride or not self.marker_size:
            return False
        start = self.address(index)
        end = start + self.record_size
        block_end = (start // self.marker_stride + 1) * self.marker_stride
        return end > block_end - self.marker_size

    def address(self, index: int) -> int:
        """Speicheradresse des Datensatzes ``index`` (0-basiert)."""
        if index < 0 or index >= self.count:
            raise IndexError("%s: Index %d ausserhalb 0..%d" % (self.key, index, self.count - 1))
        if self.per_bank:
            bank, slot = divmod(index, self.per_bank)
            return self.base + bank * self.stride + slot * self.record_size
        return self.base + index * self.record_size

    def field(self, name: str) -> Optional[Field]:
        for f in self.fields:
            if f.name == name:
                return f
        return None

    def min_confidence(self) -> str:
        order = {"high": 2, "medium": 1, "low": 0}
        if not self.fields:
            return "low"
        return min((f.confidence for f in self.fields), key=lambda c: order.get(c, 0))


class Layout:
    """Beschreibt Speicherbereiche und Datensatzaufbau eines Geraetetyps."""

    def __init__(self, data: Dict[str, Any]):
        self.id: str = data["id"]
        self.title: str = data.get("title", self.id)
        # 'Revision' statt 'Version': die Zahl bezeichnet diesen Adressplan,
        # nicht die Geraetefirmware und nicht die Programmversion von atprog.
        self.revision: int = int(data.get("plan_revision", 0))
        self.revised: str = data.get("plan_revised", "")
        self.verified_on: str = data.get("geprueft_an", "")
        self.version: str = "r%d" % self.revision if self.revision else "?"
        self.identify: List[str] = list(data.get("identify") or [])
        self.notes: List[str] = list(data.get("notes") or [])
        self.regions: Dict[str, Region] = {}
        for r in data.get("regions", []):
            self.regions[r["name"]] = Region(
                name=r["name"], addr=_num(r["addr"]), size=_num(r["size"]),
                confidence=r.get("confidence", "medium"), desc=r.get("desc", ""),
                invert=bool(r.get("invert", False)),
                einstufung=r.get("einstufung", "gesichert"),
                zweck=r.get("zweck", ""))
        self.unzugeordnet: List[Dict[str, Any]] = list(data.get("unzugeordnet") or [])
        self.revisionen: List[Dict[str, Any]] = list(data.get("revisionen") or [])
        self.bereiche_gross: List[Dict[str, Any]] = list(data.get("bereiche_gross") or [])
        self.objects: Dict[str, ObjectDef] = {}
        for key, obj in (data.get("objects") or {}).items():
            bank = obj.get("bank") or {}
            fields = []
            for f in obj.get("fields", []):
                fields.append(Field(
                    name=f["name"], type=f["type"], offset=_num(f.get("offset", 0)),
                    length=_num(f.get("length", 0)), shift=_num(f.get("shift", 0)),
                    width=_num(f.get("width", 0)), scale=_num(f.get("scale", 1)),
                    count=_num(f.get("count", 0)), item=f.get("item", "u16"),
                    empty=_num(f.get("empty", 0xFFFF)), enum=f.get("enum"),
                    confidence=f.get("confidence", "medium"), desc=f.get("desc", "")))
            marker = obj.get("block_marker") or {}
            self.objects[key] = ObjectDef(
                key=key, title=obj.get("title", key), count=_num(obj.get("count", 0)),
                record_size=_num(obj.get("record_size", 0)), base=_num(bank.get("base", 0)),
                stride=_num(bank.get("stride", 0)), per_bank=_num(bank.get("per_bank", 0)),
                bitmap=obj.get("bitmap"), fields=fields,
                marker_stride=_num(marker.get("stride", 0)),
                marker_size=_num(marker.get("size", 0)))

    # -- Zugriff ----------------------------------------------------------
    def region(self, name: str) -> Region:
        try:
            return self.regions[name]
        except KeyError:
            raise KeyError("Region %r ist im Layout %s nicht definiert" % (name, self.id))

    def obj(self, key: str) -> ObjectDef:
        try:
            return self.objects[key]
        except KeyError:
            raise KeyError("Objekt %r ist im Layout %s nicht definiert" % (key, self.id))

    def read_plan(self) -> List[Region]:
        """Alle zu lesenden Bereiche (Steuerdaten zuerst, dann Datensatzbaenke)."""
        plan = list(self.regions.values())
        for obj in self.objects.values():
            if obj.per_bank:
                banks = (obj.count + obj.per_bank - 1) // obj.per_bank
                span = obj.per_bank * obj.record_size
                for b in range(banks):
                    plan.append(Region("%s_bank%d" % (obj.key, b),
                                       obj.base + b * obj.stride, span, "high",
                                       "%s %d..%d" % (obj.title, b * obj.per_bank,
                                                      (b + 1) * obj.per_bank - 1)))
            else:
                plan.append(Region(obj.key, obj.base, obj.count * obj.record_size,
                                   "high", obj.title))
        plan.sort(key=lambda r: r.addr)
        return plan

    def matches(self, model: str) -> bool:
        from .protocol import normalise_model
        m = normalise_model(model)
        return any(m.startswith(normalise_model(p)) for p in self.identify)

    def describe(self) -> str:
        lines = ["%s" % self.title,
                 "Adressplan %s, Revision %d vom %s" % (self.id, self.revision, self.revised)]
        if self.verified_on:
            lines.append("Geprueft an: %s" % self.verified_on)
        lines.append("")
        lines.append("Bereiche:")
        for r in sorted(self.regions.values(), key=lambda x: x.addr):
            lines.append("  %-16s 0x%08X  %8d B  %-6s %s"
                         % (r.name, r.addr, r.size, r.confidence, r.desc))
        lines.append("")
        lines.append("Datensaetze:")
        for o in self.objects.values():
            lines.append("  %-12s %5d x %3d B @ 0x%08X  (Konfidenz min. %s)"
                         % (o.key, o.count, o.record_size, o.base, o.min_confidence()))
            for f in o.fields:
                lines.append("      %-18s %-10s off=%-4d %s"
                             % (f.name, f.type, f.offset, f.confidence))
        return "\n".join(lines)


def available() -> List[str]:
    names = set()
    for directory in (LAYOUT_DIR, USER_DIR):
        if os.path.isdir(directory):
            for fn in os.listdir(directory):
                if fn.endswith(".json") and not fn.startswith("."):
                    names.add(os.path.splitext(fn)[0])
    return sorted(names)


def load(name: str = DEFAULT_LAYOUT) -> Layout:
    """Laedt ein Layout; Benutzerdateien in ~/.atprog haben Vorrang."""
    if os.path.sep in name or name.endswith(".json"):
        path = name
    else:
        user_path = os.path.join(USER_DIR, name + ".json")
        path = user_path if os.path.exists(user_path) else os.path.join(LAYOUT_DIR, name + ".json")
    if not os.path.exists(path):
        raise FileNotFoundError("Layout %r nicht gefunden (verfuegbar: %s)"
                                % (name, ", ".join(available())))
    with open(path, "r", encoding="utf-8") as fh:
        return Layout(json.load(fh))


def load_for(model: str) -> Layout:
    """Sucht das passende Layout zu einer Geraetekennung."""
    for name in available():
        try:
            lay = load(name)
        except Exception:
            continue
        if lay.matches(model):
            return lay
    return load(DEFAULT_LAYOUT)
