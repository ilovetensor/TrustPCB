"""The board model: a parsed ``.kicad_pcb`` as plain dataclasses with ``to_dict``.

``parse_board(path)`` reads KiCad 9 and 10 files (and older ones where the syntax matches).
Every coordinate is in board millimetres, x right and y down, exactly as KiCad stores it, so
the model, the SVG renderer, DRC results and the 3D export all share one frame.

Differences between KiCad versions that this module absorbs:

* nets: KiCad 9 writes a net table ``(net 3 "GND")`` and items refer to ``(net 3)``; KiCad 10
  writes ``(net "GND")`` on every item and has no table.
* pad angles are absolute (they already include the footprint rotation).
* footprint children are footprint-local; ``abs = footprint_xy + rotate(local, footprint_angle)``.

The ids follow ``docs/specs/contracts.md``. Where a footprint repeats a pad number (several
``SH`` shield pads, or unnumbered mounting holes) the first keeps ``pad:<ref>:<number>``, later
ones become ``pad:<ref>:<number>#2``, ``#3`` and unnumbered ones ``pad:<ref>:~<n>``.
"""

from __future__ import annotations

import fnmatch
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import geom, sexpr
from .sexpr import Node, arg, find, findall, num

COPPER_KIND = "copper"


class UnknownTarget(KeyError):
    """Raised by :meth:`Board.resolve` for an id that does not name anything on the board."""

    def __init__(self, target_id: str):
        super().__init__(target_id)
        self.target_id = target_id

    def __str__(self) -> str:  # KeyError would quote the repr
        return f"unknown target: {self.target_id}"


# --------------------------------------------------------------------------- dataclasses


def _clean(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 4)
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return value


class _Base:
    _exclude: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {k: _clean(v) for k, v in self.__dict__.items() if not k.startswith("_") and k not in self._exclude}


@dataclass
class Layer(_Base):
    name: str
    kind: str
    index: int


@dataclass
class Net(_Base):
    id: str
    name: str
    net_class: str = "Default"


@dataclass
class Pad(_Base):
    id: str
    ref: str
    number: str
    net: str | None
    x: float
    y: float
    w: float
    h: float
    rotation: float
    shape: str
    layers: list[str]
    drill: float | None
    ring: list[list[float]]
    type: str = "smd"
    uuid: str = ""


@dataclass
class Footprint(_Base):
    id: str
    ref: str
    value: str
    lib_id: str
    x: float
    y: float
    rotation: float
    side: str
    bbox: list[float]
    courtyard: list[list[float]] | None
    pad_ids: list[str]
    dnp: bool = False
    uuid: str = ""


@dataclass
class Track(_Base):
    id: str
    net: str | None
    layer: str
    width: float
    start: list[float]
    end: list[float]
    mid: list[float] | None
    length: float
    uuid: str = ""

    def points(self) -> list[tuple[float, float]]:
        if self.mid is None:
            return [(self.start[0], self.start[1]), (self.end[0], self.end[1])]
        return geom.arc_points(tuple(self.start), tuple(self.mid), tuple(self.end))  # type: ignore[arg-type]


@dataclass
class Via(_Base):
    id: str
    net: str | None
    x: float
    y: float
    diameter: float
    drill: float
    layers: list[str]
    uuid: str = ""


@dataclass
class Zone(_Base):
    id: str
    net: str | None
    layers: list[str]
    filled: dict[str, list[list[list[float]]]]
    is_keepout: bool = False
    name: str = ""
    outline: list[list[float]] = field(default_factory=list)
    priority: int = 0
    uuid: str = ""


@dataclass
class Keepout(_Base):
    id: str
    layers: list[str]
    ring: list[list[float]]
    rules: dict[str, str]
    name: str = ""
    uuid: str = ""


@dataclass
class Graphic(_Base):
    """A drawn line, arc, circle, rectangle or polygon, already in board coordinates."""

    layer: str
    kind: str  # line | poly
    points: list[list[float]]
    width: float = 0.0
    fill: bool = False
    closed: bool = False
    owner: str | None = None  # footprint ref, None for board-level drawings


@dataclass
class Text(_Base):
    layer: str
    text: str
    x: float
    y: float
    rotation: float = 0.0
    size: float = 1.0
    owner: str | None = None
    mirror: bool = False
    justify: str = "center"  # left | center | right


@dataclass
class Geometry(_Base):
    """What a target id resolves to. ``kind`` is point, polyline, polygon or group."""

    kind: str
    bbox: list[float]
    layer: str | None = None
    points: list[list[float]] | None = None
    rings: list[list[list[float]]] | None = None
    parts: list[dict[str, Any]] | None = None
    center: list[float] | None = None
    target: str | None = None
    width: float | None = None

    def to_dict(self) -> dict[str, Any]:
        d = {k: _clean(v) for k, v in self.__dict__.items() if v is not None}
        d.setdefault("layer", None)
        return d


@dataclass
class Board:
    version_id: str
    title: str
    outline: list[list[list[float]]]
    bbox: list[float]
    layers: list[Layer]
    stackup: dict[str, Any]
    nets: list[Net]
    footprints: list[Footprint]
    pads: list[Pad]
    tracks: list[Track]
    vias: list[Via]
    zones: list[Zone]
    keepouts: list[Keepout]
    graphics: list[Graphic] = field(default_factory=list)
    texts: list[Text] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    kicad_version: str | None = None
    format_version: str | None = None
    thickness: float = 1.6
    source: str | None = None
    #: full KiCad uuid -> target id, so DRC results can be mapped to targets
    uuid_index: dict[str, str] = field(default_factory=dict)
    #: registered later by the pipeline: ``zone_role:<role>`` and ``candidate:<id>`` targets
    zone_roles: dict[str, list[str]] = field(default_factory=dict)
    candidate_targets: dict[str, list[str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._index()

    # ----------------------------------------------------------------- indexes
    def _index(self) -> None:
        self._fp = {f.id: f for f in self.footprints}
        self._pad = {p.id: p for p in self.pads}
        self._track = {t.id: t for t in self.tracks}
        self._via = {v.id: v for v in self.vias}
        self._zone = {z.id: z for z in self.zones}
        self._keepout = {k.id: k for k in self.keepouts}
        self._net = {n.name: n for n in self.nets}
        self._pads_by_number: dict[str, list[Pad]] = {}
        for p in self.pads:
            self._pads_by_number.setdefault(f"pad:{p.ref}:{p.number}", []).append(p)

    def footprint(self, ref: str) -> Footprint | None:
        return self._fp.get(f"ref:{ref}")

    def pads_of(self, ref: str) -> list[Pad]:
        f = self._fp.get(f"ref:{ref}")
        return [self._pad[i] for i in f.pad_ids] if f else []

    def net_names(self) -> list[str]:
        return [n.name for n in self.nets]

    def copper_layers(self) -> list[str]:
        return [layer.name for layer in self.layers if layer.kind == COPPER_KIND]

    def items_on_net(self, name: str) -> dict[str, list[Any]]:
        return {
            "pads": [p for p in self.pads if p.net == name],
            "tracks": [t for t in self.tracks if t.net == name],
            "vias": [v for v in self.vias if v.net == name],
            "zones": [z for z in self.zones if z.net == name],
        }

    # ------------------------------------------------------------------ output
    def to_dict(self) -> dict[str, Any]:
        return {
            "version_id": self.version_id,
            "title": self.title,
            "outline": _clean(self.outline),
            "bbox": _clean(self.bbox),
            "layers": [x.to_dict() for x in self.layers],
            "stackup": _clean(self.stackup),
            "nets": [x.to_dict() for x in self.nets],
            "footprints": [x.to_dict() for x in self.footprints],
            "pads": [x.to_dict() for x in self.pads],
            "tracks": [x.to_dict() for x in self.tracks],
            "vias": [x.to_dict() for x in self.vias],
            "zones": [x.to_dict() for x in self.zones],
            "keepouts": [x.to_dict() for x in self.keepouts],
            "warnings": list(self.warnings),
            "kicad_version": self.kicad_version,
        }

    def summary(self) -> dict[str, Any]:
        """Counts only: cheap to log and to hand to an agent."""
        return {
            "title": self.title,
            "bbox": _clean(self.bbox),
            "size_mm": [round(self.bbox[2] - self.bbox[0], 3), round(self.bbox[3] - self.bbox[1], 3)],
            "layers": [x.name for x in self.layers if x.kind == COPPER_KIND],
            "footprints": len(self.footprints),
            "pads": len(self.pads),
            "tracks": len(self.tracks),
            "vias": len(self.vias),
            "zones": len(self.zones),
            "keepouts": len(self.keepouts),
            "nets": len(self.nets),
            "stackup_assumed": self.stackup.get("assumed"),
        }

    # ----------------------------------------------------------------- resolve
    def register_zone_role(self, role: str, targets: list[str]) -> None:
        self.zone_roles[role] = list(targets)

    def register_candidate(self, candidate_id: str, targets: list[str]) -> None:
        self.candidate_targets[candidate_id] = list(targets)

    def resolve(self, target_id: str) -> Geometry:
        """Geometry for a server-issued target id; raises :class:`UnknownTarget`.

        Supported ids: ``ref:``, ``pad:``, ``track:``, ``via:``, ``zone:``, ``keepout:``,
        ``net:<name>``, ``zone_role:<role>``, ``candidate:<id>``, plus two coordinate forms
        that the pipeline uses for items with no object behind them: ``point:<x>,<y>`` and
        ``bbox:<x0>,<y0>,<x1>,<y1>`` (millimetres).
        """
        try:
            kind, _, rest = target_id.partition(":")
        except AttributeError:
            raise UnknownTarget(str(target_id)) from None
        geo = self._resolve(target_id, kind, rest)
        geo.target = target_id
        return geo

    def _resolve(self, tid: str, kind: str, rest: str) -> Geometry:
        if kind == "ref":
            f = self._fp.get(tid)
            if f is None:
                raise UnknownTarget(tid)
            ring = f.courtyard or geom.bbox_ring(f.bbox)
            return Geometry("polygon", list(f.bbox), f.side and ("F.Cu" if f.side == "top" else "B.Cu"), rings=[ring], center=[f.x, f.y])
        if kind == "pad":
            pads = self._pads_by_number.get(tid)
            if pads and len(pads) > 1 and tid not in self._pad:
                return self._group([self._pad_geo(p) for p in pads])
            p = self._pad.get(tid)
            if p is None:
                if pads:
                    return self._pad_geo(pads[0])
                raise UnknownTarget(tid)
            return self._pad_geo(p)
        if kind == "track":
            t = self._track.get(tid)
            if t is None:
                raise UnknownTarget(tid)
            return self._track_geo(t)
        if kind == "via":
            v = self._via.get(tid)
            if v is None:
                raise UnknownTarget(tid)
            return self._via_geo(v)
        if kind == "zone":
            z = self._zone.get(tid)
            if z is None:
                raise UnknownTarget(tid)
            return self._zone_geo(z)
        if kind == "keepout":
            k = self._keepout.get(tid)
            if k is None:
                raise UnknownTarget(tid)
            return Geometry("polygon", geom.bbox_of(k.ring), k.layers[0] if k.layers else None, rings=[k.ring], center=list(geom.ring_centroid(k.ring)))
        if kind == "net":
            if rest not in self._net:
                raise UnknownTarget(tid)
            items = self.items_on_net(rest)
            parts = [self._pad_geo(p) for p in items["pads"]]
            parts += [self._track_geo(t) for t in items["tracks"]]
            parts += [self._via_geo(v) for v in items["vias"]]
            if not parts:
                raise UnknownTarget(tid)
            return self._group(parts)
        if kind == "zone_role":
            if rest not in self.zone_roles:
                raise UnknownTarget(tid)
            return self._group([self.resolve(t) for t in self.zone_roles[rest]])
        if kind == "candidate":
            if tid not in self.candidate_targets:
                raise UnknownTarget(tid)
            return self._group([self.resolve(t) for t in self.candidate_targets[tid]])
        if kind == "point":
            try:
                x, y = (float(v) for v in rest.split(","))
            except ValueError:
                raise UnknownTarget(tid) from None
            return Geometry("point", [x, y, x, y], None, points=[[x, y]], center=[x, y])
        if kind == "bbox":
            try:
                x0, y0, x1, y1 = (float(v) for v in rest.split(","))
            except ValueError:
                raise UnknownTarget(tid) from None
            return Geometry("polygon", [x0, y0, x1, y1], None, rings=[geom.bbox_ring([x0, y0, x1, y1])], center=[(x0 + x1) / 2, (y0 + y1) / 2])
        raise UnknownTarget(tid)

    def _pad_geo(self, p: Pad) -> Geometry:
        layer = next((name for name in p.layers if name.endswith(".Cu")), None)
        return Geometry("polygon", geom.bbox_of(p.ring), layer, rings=[p.ring], center=[p.x, p.y], target=p.id)

    def _track_geo(self, t: Track) -> Geometry:
        pts = [[x, y] for x, y in t.points()]
        box = geom.bbox_of(pts)
        h = t.width / 2
        return Geometry("polyline", [box[0] - h, box[1] - h, box[2] + h, box[3] + h], t.layer, points=pts, center=[(t.start[0] + t.end[0]) / 2, (t.start[1] + t.end[1]) / 2], target=t.id, width=t.width)

    def _via_geo(self, v: Via) -> Geometry:
        r = v.diameter / 2
        return Geometry("point", [v.x - r, v.y - r, v.x + r, v.y + r], v.layers[0] if v.layers else None, points=[[v.x, v.y]], center=[v.x, v.y], target=v.id, width=v.diameter)

    def _zone_geo(self, z: Zone) -> Geometry:
        layer = z.layers[0] if z.layers else None
        rings = z.filled.get(layer or "") or next(iter(z.filled.values()), None) or [z.outline]
        box = geom.union_bbox(geom.bbox_of(r) for r in rings) or [0, 0, 0, 0]
        return Geometry("polygon", box, layer, rings=rings, center=[(box[0] + box[2]) / 2, (box[1] + box[3]) / 2], target=z.id)

    @staticmethod
    def _group(parts: list[Geometry]) -> Geometry:
        box = geom.union_bbox(p.bbox for p in parts) or [0, 0, 0, 0]
        return Geometry("group", box, parts[0].layer if parts else None, parts=[p.to_dict() for p in parts], center=[(box[0] + box[2]) / 2, (box[1] + box[3]) / 2])


# --------------------------------------------------------------------------- parsing

_COPPER_RE = re.compile(r"\.Cu$")
_UUID_RE = re.compile(r"^[0-9a-fA-F-]{8,}$")


def _layer_kind(name: str) -> str:
    if name.endswith(".Cu"):
        return COPPER_KIND
    if name.endswith(("SilkS", "Silkscreen")):
        return "silk"
    if name.endswith("Mask"):
        return "mask"
    if name.endswith("Paste"):
        return "paste"
    if name.endswith(("Fab", "CrtYd", "Courtyard", "Adhes")):
        return "fab"
    if name == "Edge.Cuts":
        return "edge"
    return "other"


class _Parser:
    def __init__(self, root: Node, version_id: str, source: str | None, project: dict | None):
        self.root = root
        self.version_id = version_id
        self.source = source
        self.project = project or {}
        self.warnings: list[str] = []
        self.net_table: dict[str, str] = {}
        self.net_order: list[str] = []
        self.layers: list[Layer] = []
        self.copper: list[str] = []
        self.used_ids: set[str] = set()
        self.uuid_index: dict[str, str] = {}

    # ---- helpers
    def note_net(self, name: str | None) -> None:
        if name and name not in self._net_seen:
            self._net_seen.add(name)
            self.net_order.append(name)

    _net_seen: set[str]

    def net_of(self, node: Node) -> str | None:
        n = find(node, "net")
        if n is None or len(n) < 2:
            name_node = find(node, "net_name")
            name = name_node[1] if name_node and len(name_node) > 1 else None
        elif len(n) >= 3:  # (net 3 "GND")
            name = n[2]
        elif n[1].lstrip("-").isdigit() and n[1] in self.net_table:  # (net 3) with a table
            name = self.net_table[n[1]]
        elif n[1].isdigit():
            name_node = find(node, "net_name")
            name = name_node[1] if name_node and len(name_node) > 1 else (self.net_table.get(n[1]))
        else:  # (net "GND")
            name = n[1]
        if not name:
            return None
        self.note_net(name)
        return name

    def uid(self, node: Node) -> str:
        v = arg(node, "uuid") or arg(node, "tstamp") or ""
        return v

    def short_id(self, prefix: str, uuid: str, fallback: str) -> str:
        base = (uuid.replace("-", "")[:8] if uuid else fallback) or fallback
        ident = f"{prefix}:{base}"
        if ident in self.used_ids:
            n = 2
            while f"{ident}~{n}" in self.used_ids:
                n += 1
            ident = f"{ident}~{n}"
        self.used_ids.add(ident)
        if uuid:
            self.uuid_index[uuid] = ident
        return ident

    def expand_layers(self, names: list[str]) -> list[str]:
        out: list[str] = []
        for n in names:
            if n in ("*.Cu", "F&B.Cu"):
                if n == "F&B.Cu":
                    out += [x for x in ("F.Cu", "B.Cu") if x in self.copper or not self.copper]
                else:
                    out += self.copper
            elif n == "*.Mask":
                out += ["F.Mask", "B.Mask"]
            elif n == "*.Paste":
                out += ["F.Paste", "B.Paste"]
            elif n == "*.SilkS":
                out += ["F.SilkS", "B.SilkS"]
            else:
                out.append(n)
        seen: set[str] = set()
        return [x for x in out if not (x in seen or seen.add(x))]

    # ---- main
    def parse(self) -> Board:
        root = self.root
        self._net_seen = set()
        self.format_version = arg(root, "version")
        self.kicad_version = arg(root, "generator_version")
        self.parse_layers()
        for n in findall(root, "net"):
            if len(n) >= 3:
                self.net_table[n[1]] = n[2]
                if n[2]:
                    self.note_net(n[2])
        general = find(root, "general")
        thickness = num(general, "thickness", 1.6) if general else 1.6
        title = ""
        tb = find(root, "title_block")
        if tb is not None:
            title = arg(tb, "title", "") or ""

        footprints: list[Footprint] = []
        pads: list[Pad] = []
        graphics: list[Graphic] = []
        texts: list[Text] = []
        edge_polylines: list[list[tuple[float, float]]] = []
        edge_rings: list[list[tuple[float, float]]] = []
        footprint_zones: list[tuple[Node, Any]] = []
        ref_seen: dict[str, int] = {}

        for node in findall(root, "footprint"):
            self.parse_footprint(node, footprints, pads, graphics, texts, edge_polylines, edge_rings, ref_seen)
        for node in root:
            if node.__class__ is list and node and node[0].startswith("gr_"):
                self.parse_graphic(node, None, None, graphics, texts, edge_polylines, edge_rings)

        tracks: list[Track] = []
        vias: list[Via] = []
        for node in root:
            if node.__class__ is not list or not node:
                continue
            tag = node[0]
            if tag == "segment":
                tracks.append(self.parse_segment(node, arc=False))
            elif tag == "arc":
                tracks.append(self.parse_segment(node, arc=True))
            elif tag == "via":
                vias.append(self.parse_via(node))

        zones: list[Zone] = []
        keepouts: list[Keepout] = []
        for node in findall(root, "zone"):
            self.parse_zone(node, zones, keepouts)

        outline, bbox = self.build_outline(edge_polylines, edge_rings, pads, tracks, vias, footprints)
        stackup = self.parse_stackup(thickness)
        net_class_of = self.net_classes()
        nets = [Net(f"net:{n}", n, net_class_of(n)) for n in self.net_order]
        return Board(
            version_id=self.version_id,
            title=title or (Path(self.source).stem if self.source else ""),
            outline=outline,
            bbox=bbox,
            layers=self.layers,
            stackup=stackup,
            nets=nets,
            footprints=footprints,
            pads=pads,
            tracks=tracks,
            vias=vias,
            zones=zones,
            keepouts=keepouts,
            graphics=graphics,
            texts=texts,
            warnings=self.warnings,
            kicad_version=self.kicad_version,
            format_version=self.format_version,
            thickness=thickness,
            source=self.source,
            uuid_index=self.uuid_index,
        )

    def parse_layers(self) -> None:
        layers = find(self.root, "layers")
        if layers is None:
            self.warnings.append("board file has no layer table; assuming two copper layers")
            self.layers = [Layer("F.Cu", COPPER_KIND, 0), Layer("B.Cu", COPPER_KIND, 31)]
        else:
            for item in layers[1:]:
                if item.__class__ is list and len(item) >= 2:
                    try:
                        idx = int(item[0])
                    except ValueError:
                        continue
                    self.layers.append(Layer(item[1], _layer_kind(item[1]), idx))
        self.copper = [layer.name for layer in self.layers if layer.kind == COPPER_KIND]
        # Top to bottom: F.Cu first, B.Cu last, inner layers by index.
        self.copper.sort(key=lambda n: (0 if n == "F.Cu" else 2 if n == "B.Cu" else 1, next(layer.index for layer in self.layers if layer.name == n)))

    # ---- footprints
    def parse_footprint(self, node, footprints, pads_out, graphics, texts, edge_polylines, edge_rings, ref_seen):
        lib_id = node[1] if len(node) > 1 and node[1].__class__ is str else ""
        layer = arg(node, "layer", "F.Cu")
        side = "bottom" if layer.startswith("B.") else "top"
        at = find(node, "at")
        fx, fy = (float(at[1]), float(at[2])) if at else (0.0, 0.0)
        frot = float(at[3]) if at and len(at) > 3 else 0.0
        uuid = self.uid(node)
        props = {p[1]: p[2] for p in findall(node, "property") if len(p) > 2}
        ref = props.get("Reference")
        value = props.get("Value", "")
        if ref is None:
            for t in findall(node, "fp_text"):
                if len(t) > 2 and t[1] == "reference":
                    ref = t[2]
                elif len(t) > 2 and t[1] == "value":
                    value = t[2]
        ref = ref or f"FP{len(footprints) + 1}"
        count = ref_seen.get(ref, 0) + 1
        ref_seen[ref] = count
        unique_ref = ref if count == 1 else f"{ref}#{count}"
        if count > 1:
            self.warnings.append(f"duplicate reference designator {ref}; later copies are named {unique_ref}")
        fid = f"ref:{unique_ref}"
        if uuid:
            self.uuid_index[uuid] = fid
        attr = find(node, "attr")
        dnp = bool(attr and "dnp" in attr) or arg(node, "dnp") == "yes"

        def to_abs(px: float, py: float) -> tuple[float, float]:
            rx, ry = geom.rotate(px, py, frot)
            return fx + rx, fy + ry

        pad_ids: list[str] = []
        used: dict[str, int] = {}
        pad_rings: list[list[list[float]]] = []
        courtyard_geoms: list[list[tuple[float, float]]] = []
        courtyard_closed: list[list[tuple[float, float]]] = []
        for i, pn in enumerate(findall(node, "pad"), start=1):
            pad = self.parse_pad(pn, unique_ref, i, fx, fy, frot, used)
            pads_out.append(pad)
            pad_ids.append(pad.id)
            pad_rings.append(pad.ring)
        for child in node:
            if child.__class__ is not list or not child:
                continue
            tag = child[0]
            if tag.startswith("fp_") and tag != "fp_text":
                layer_name = arg(child, "layer", "")
                polylines: list[list[tuple[float, float]]] = []
                rings: list[list[tuple[float, float]]] = []
                self.shape_geometry(child, to_abs, polylines, rings)
                if layer_name == "Edge.Cuts":
                    edge_polylines.extend(polylines)
                    edge_rings.extend(rings)
                elif layer_name in ("F.CrtYd", "B.CrtYd"):
                    courtyard_geoms.extend(polylines)
                    courtyard_closed.extend(rings)
                    for ring in rings:
                        graphics.append(Graphic(layer_name, "poly", [list(p) for p in ring], self.stroke_width(child), False, True, unique_ref))
                    for line in polylines:
                        graphics.append(Graphic(layer_name, "line", [list(p) for p in line], self.stroke_width(child), False, False, unique_ref))
                else:
                    width = self.stroke_width(child)
                    filled = self.is_filled(child)
                    for ring in rings:
                        graphics.append(Graphic(layer_name, "poly", [list(p) for p in ring], width, filled, True, unique_ref))
                    for line in polylines:
                        graphics.append(Graphic(layer_name, "line", [list(p) for p in line], width, False, False, unique_ref))
            elif tag == "fp_text" and len(child) > 2 and child[1] == "user":
                pass
            elif tag == "zone":
                self.parse_zone(child, self._pending_zones, self._pending_keepouts, to_abs)
        # courtyard ring
        courtyard = self.courtyard_ring(courtyard_geoms, courtyard_closed)
        boxes = [geom.bbox_of(r) for r in pad_rings]
        if courtyard:
            boxes.append(geom.bbox_of(courtyard))
        bbox = geom.union_bbox(boxes) or [fx - 0.5, fy - 0.5, fx + 0.5, fy + 0.5]
        footprints.append(
            Footprint(
                id=fid, ref=unique_ref, value=value, lib_id=lib_id, x=fx, y=fy, rotation=frot, side=side,
                bbox=bbox, courtyard=courtyard, pad_ids=pad_ids, dnp=dnp, uuid=uuid,
            )
        )
        # reference label: the property's own layer decides whether it prints on silk
        ref_prop = next((p for p in findall(node, "property") if len(p) > 2 and p[1] == "Reference"), None)
        if ref_prop is not None:
            pat = find(ref_prop, "at")
            lx, ly = (float(pat[1]), float(pat[2])) if pat else (0.0, 0.0)
            ax, ay = to_abs(lx, ly)
            hidden = arg(ref_prop, "hide") == "yes" or "hide" in ref_prop
            eff = find(ref_prop, "effects")
            font = find(eff, "font") if eff else None
            size_node = find(font, "size") if font else None
            sz = float(size_node[1]) if size_node else 1.0
            if not hidden:
                texts.append(Text(arg(ref_prop, "layer", "F.SilkS"), unique_ref, ax, ay, 0.0, sz, unique_ref))

    _pending_zones: list[Zone]
    _pending_keepouts: list[Keepout]

    def parse_pad(self, pn: Node, ref: str, index: int, fx: float, fy: float, frot: float, used: dict[str, int]) -> Pad:
        number = pn[1] if len(pn) > 1 and pn[1].__class__ is str else ""
        ptype = pn[2] if len(pn) > 2 and pn[2].__class__ is str else "smd"
        shape = pn[3] if len(pn) > 3 and pn[3].__class__ is str else "rect"
        at = find(pn, "at")
        lx, ly = (float(at[1]), float(at[2])) if at else (0.0, 0.0)
        pangle = float(at[3]) if at and len(at) > 3 else frot  # absolute, includes the footprint angle
        rx, ry = geom.rotate(lx, ly, frot)
        px, py = fx + rx, fy + ry
        size = find(pn, "size")
        w = float(size[1]) if size and len(size) > 1 else 0.0
        h = float(size[2]) if size and len(size) > 2 else w
        layers = self.expand_layers([x for x in (find(pn, "layers") or [])[1:] if x.__class__ is str])
        drill_node = find(pn, "drill")
        drill = None
        if drill_node is not None:
            vals = [x for x in drill_node[1:] if x.__class__ is str and re.match(r"^-?[\d.]+$", x)]
            if vals:
                drill = float(vals[0])
        net = self.net_of(pn)
        uuid = self.uid(pn)
        ring = self.pad_ring(pn, shape, px, py, w, h, pangle)
        if number == "":
            ident = f"pad:{ref}:~{index}"
        else:
            used[number] = used.get(number, 0) + 1
            ident = f"pad:{ref}:{number}" if used[number] == 1 else f"pad:{ref}:{number}#{used[number]}"
        if uuid:
            self.uuid_index[uuid] = ident
        return Pad(ident, ref, number, net, px, py, w, h, pangle, shape, layers, drill, ring, ptype, uuid)

    def pad_ring(self, pn: Node, shape: str, px: float, py: float, w: float, h: float, angle: float) -> list[list[float]]:
        if shape == "circle":
            return geom.circle_ring(px, py, w / 2)
        if shape == "oval":
            return geom.oval_ring(px, py, w, h, angle)
        if shape == "roundrect":
            ratio = num(pn, "roundrect_rratio", 0.25)
            return geom.roundrect_ring(px, py, w, h, ratio, angle)
        if shape == "custom":
            ring = self.custom_pad_ring(pn, px, py, w, h, angle)
            if ring:
                return ring
        return geom.rect_ring(px, py, w, h, angle)

    def custom_pad_ring(self, pn: Node, px: float, py: float, w: float, h: float, angle: float) -> list[list[float]] | None:
        try:
            from shapely.geometry import LineString, Point, Polygon, box
            from shapely.ops import unary_union
        except ImportError:  # pragma: no cover
            return None
        shapes = []
        options = find(pn, "options")
        anchor = arg(options, "anchor", "circle") if options else "circle"
        shapes.append(Point(0, 0).buffer(w / 2, 16) if anchor == "circle" else box(-w / 2, -h / 2, w / 2, h / 2))
        prims = find(pn, "primitives")
        for prim in prims[1:] if prims else []:
            if prim.__class__ is not list or not prim:
                continue
            tag = prim[0]
            width = num(prim, "width", 0.0) or 0.0
            try:
                if tag == "gr_poly":
                    pts = self.read_pts(find(prim, "pts"))
                    if len(pts) >= 3:
                        poly = Polygon(pts)
                        shapes.append(poly.buffer(width / 2) if width else poly)
                elif tag == "gr_line":
                    a, b = find(prim, "start"), find(prim, "end")
                    shapes.append(LineString([(float(a[1]), float(a[2])), (float(b[1]), float(b[2]))]).buffer(max(width, 0.001) / 2))
                elif tag == "gr_circle":
                    c, e = find(prim, "center"), find(prim, "end")
                    r = math.hypot(float(e[1]) - float(c[1]), float(e[2]) - float(c[2]))
                    shapes.append(Point(float(c[1]), float(c[2])).buffer(r + width / 2, 16))
                elif tag == "gr_rect":
                    a, b = find(prim, "start"), find(prim, "end")
                    shapes.append(box(float(a[1]), float(a[2]), float(b[1]), float(b[2])).buffer(width / 2))
                elif tag == "gr_arc":
                    s, m, e = find(prim, "start"), find(prim, "mid"), find(prim, "end")
                    pts = geom.arc_points((float(s[1]), float(s[2])), (float(m[1]), float(m[2])), (float(e[1]), float(e[2])))
                    shapes.append(LineString(pts).buffer(max(width, 0.001) / 2))
            except (TypeError, ValueError, IndexError):
                continue
        merged = unary_union(shapes)
        if merged.is_empty:
            return None
        if merged.geom_type == "MultiPolygon":
            merged = max(merged.geoms, key=lambda g: g.area)
        ring = [[x, y] for x, y in list(merged.exterior.coords)[:-1]]
        return [[px + rx, py + ry] for rx, ry in (geom.rotate(x, y, angle) for x, y in ring)]

    @staticmethod
    def courtyard_ring(lines, rings) -> list[list[float]] | None:
        if not lines and not rings:
            return None
        try:
            from shapely.geometry import LineString, Polygon
            from shapely.ops import polygonize, unary_union
        except ImportError:  # pragma: no cover
            pts = [p for line in lines for p in line] + [p for r in rings for p in r]
            box = geom.bbox_of(pts)
            return geom.bbox_ring(box)
        polys = [Polygon(r) for r in rings if len(r) >= 3]
        if lines:
            merged = unary_union([LineString(line) for line in lines if len(line) >= 2])
            polys.extend(polygonize(merged))
        polys = [p for p in polys if p.is_valid and p.area > 0]
        if not polys:
            pts = [p for line in lines for p in line] + [p for r in rings for p in r]
            return geom.bbox_ring(geom.bbox_of(pts))
        union = unary_union(polys)
        if union.geom_type != "Polygon":
            union = union.convex_hull
        return [[x, y] for x, y in list(union.exterior.coords)[:-1]]

    # ---- shared shape reading
    @staticmethod
    def read_pts(pts: Node | None) -> list[tuple[float, float]]:
        out: list[tuple[float, float]] = []
        if pts is None:
            return out
        for item in pts[1:]:
            if item.__class__ is not list or not item:
                continue
            if item[0] == "xy":
                out.append((float(item[1]), float(item[2])))
            elif item[0] == "arc":
                s, m, e = find(item, "start"), find(item, "mid"), find(item, "end")
                if s and m and e:
                    a = geom.arc_points((float(s[1]), float(s[2])), (float(m[1]), float(m[2])), (float(e[1]), float(e[2])))
                    out.extend(a if not out else a[1:] if out[-1] == a[0] else a)
        return out

    def stroke_width(self, node: Node) -> float:
        stroke = find(node, "stroke")
        if stroke is not None:
            return num(stroke, "width", 0.1) or 0.0
        return num(node, "width", 0.1) or 0.0

    @staticmethod
    def is_filled(node: Node) -> bool:
        f = find(node, "fill")
        if f is not None:
            return len(f) > 1 and f[1] in ("yes", "solid", "true")
        return False

    def shape_geometry(self, node: Node, to_abs, polylines: list, rings: list) -> None:
        """Turn a ``gr_*``/``fp_*`` shape into absolute polylines (open) or rings (closed)."""
        tag = node[0].split("_", 1)[1] if "_" in node[0] else node[0]

        def pt(n: Node | None) -> tuple[float, float] | None:
            if n is None or len(n) < 3:
                return None
            return to_abs(float(n[1]), float(n[2]))

        if tag == "line":
            a, b = pt(find(node, "start")), pt(find(node, "end"))
            if a and b:
                polylines.append([a, b])
        elif tag == "arc":
            s, m, e = pt(find(node, "start")), pt(find(node, "mid")), pt(find(node, "end"))
            if s and m and e:
                polylines.append(geom.arc_points(s, m, e))
            elif s and e:
                polylines.append([s, e])
        elif tag == "rect":
            a, b = find(node, "start"), find(node, "end")
            if a and b:
                x0, y0, x1, y1 = float(a[1]), float(a[2]), float(b[1]), float(b[2])
                rings.append([to_abs(x0, y0), to_abs(x1, y0), to_abs(x1, y1), to_abs(x0, y1)])
        elif tag == "circle":
            c, e = find(node, "center"), find(node, "end")
            if c and e:
                cx, cy = float(c[1]), float(c[2])
                r = math.hypot(float(e[1]) - cx, float(e[2]) - cy)
                rings.append([to_abs(x, y) for x, y in geom.circle_ring(cx, cy, r)])
        elif tag == "poly":
            pts = self.read_pts(find(node, "pts"))
            if len(pts) >= 3:
                rings.append([to_abs(x, y) for x, y in pts])
        elif tag in ("curve", "bezier"):
            pts = self.read_pts(find(node, "pts"))
            if len(pts) == 4:
                polylines.append([to_abs(x, y) for x, y in self.bezier(pts)])

    @staticmethod
    def bezier(p: list[tuple[float, float]], steps: int = 24) -> list[tuple[float, float]]:
        out = []
        for i in range(steps + 1):
            t = i / steps
            u = 1 - t
            out.append(
                (
                    u**3 * p[0][0] + 3 * u * u * t * p[1][0] + 3 * u * t * t * p[2][0] + t**3 * p[3][0],
                    u**3 * p[0][1] + 3 * u * u * t * p[1][1] + 3 * u * t * t * p[2][1] + t**3 * p[3][1],
                )
            )
        return out

    def parse_graphic(self, node, owner, to_abs, graphics, texts, edge_polylines, edge_rings) -> None:
        tag = node[0]
        ident = to_abs or (lambda x, y: (x, y))
        if tag == "gr_text":
            at = find(node, "at")
            if at is None or len(node) < 2:
                return
            eff = find(node, "effects")
            font = find(eff, "font") if eff else None
            size_node = find(font, "size") if font else None
            size = float(size_node[1]) if size_node else 1.0
            layer = arg(node, "layer", "F.SilkS")
            jn = find(eff, "justify") if eff else None
            mirror = bool(jn and "mirror" in jn)
            justify = "left" if jn and "left" in jn else "right" if jn and "right" in jn else "center"
            texts.append(Text(layer, node[1], float(at[1]), float(at[2]), float(at[3]) if len(at) > 3 else 0.0, size, owner, mirror, justify))
            return
        if tag not in ("gr_line", "gr_arc", "gr_rect", "gr_circle", "gr_poly", "gr_curve", "gr_bezier"):
            return
        layer = arg(node, "layer", "")
        polylines: list = []
        rings: list = []
        self.shape_geometry(node, ident, polylines, rings)
        if layer == "Edge.Cuts":
            edge_polylines.extend(polylines)
            edge_rings.extend(rings)
        width = self.stroke_width(node)
        filled = self.is_filled(node)
        for ring in rings:
            graphics.append(Graphic(layer, "poly", [list(p) for p in ring], width, filled, True, owner))
        for line in polylines:
            graphics.append(Graphic(layer, "line", [list(p) for p in line], width, False, False, owner))

    # ---- tracks and vias
    def parse_segment(self, node: Node, arc: bool) -> Track:
        s, e = find(node, "start"), find(node, "end")
        start = [float(s[1]), float(s[2])]
        end = [float(e[1]), float(e[2])]
        mid = None
        if arc:
            m = find(node, "mid")
            mid = [float(m[1]), float(m[2])]
            length = geom.arc_length(tuple(start), tuple(mid), tuple(end))  # type: ignore[arg-type]
        else:
            length = math.dist(start, end)
        uuid = self.uid(node)
        ident = self.short_id("track", uuid, f"i{len(self.used_ids)}")
        return Track(ident, self.net_of(node), arg(node, "layer", ""), num(node, "width", 0.0) or 0.0, start, end, mid, length, uuid)

    def parse_via(self, node: Node) -> Via:
        at = find(node, "at")
        layers_node = find(node, "layers")
        layers = [x for x in (layers_node[1:] if layers_node else []) if x.__class__ is str] or ["F.Cu", "B.Cu"]
        uuid = self.uid(node)
        ident = self.short_id("via", uuid, f"i{len(self.used_ids)}")
        return Via(ident, self.net_of(node), float(at[1]), float(at[2]), num(node, "size", 0.0) or 0.0, num(node, "drill", 0.0) or 0.0, self.expand_layers(layers), uuid)

    # ---- zones
    def parse_zone(self, node: Node, zones: list, keepouts: list, to_abs=None) -> None:
        ident = to_abs or (lambda x, y: (x, y))
        layers = []
        ln = find(node, "layers")
        if ln is not None:
            layers = self.expand_layers([x for x in ln[1:] if x.__class__ is str])
        else:
            lone = arg(node, "layer")
            if lone:
                layers = self.expand_layers([lone])
        uuid = self.uid(node)
        name = arg(node, "name", "") or ""
        outline_pts = []
        poly = find(node, "polygon")
        if poly is not None:
            outline_pts = [ident(x, y) for x, y in self.read_pts(find(poly, "pts"))]
        outline = [[x, y] for x, y in outline_pts]
        ko = find(node, "keepout")
        if ko is not None:
            kid = self.short_id("keepout", uuid, f"k{len(self.used_ids)}")
            rules = {c[0]: c[1] for c in ko[1:] if c.__class__ is list and len(c) > 1}
            keepouts.append(Keepout(kid, layers, outline, rules, name, uuid))
            return
        filled: dict[str, list] = {}
        for fp in findall(node, "filled_polygon"):
            layer = arg(fp, "layer", layers[0] if layers else "")
            pts = [ident(x, y) for x, y in self.read_pts(find(fp, "pts"))]
            if len(pts) >= 3:
                filled.setdefault(layer, []).append([[x, y] for x, y in pts])
        zid = self.short_id("zone", uuid, f"z{len(self.used_ids)}")
        priority = int(num(node, "priority", 0) or 0)
        zones.append(Zone(zid, self.net_of(node), layers, filled, False, name, outline, priority, uuid))

    # ---- outline
    def build_outline(self, edge_polylines, edge_rings, pads, tracks, vias, footprints):
        closed, open_chains = geom.chain_rings(edge_polylines)
        rings = [list(map(list, r)) for r in edge_rings] + [[list(p) for p in r] for r in closed]
        rings = [r for r in rings if len(r) >= 3]
        if open_chains:
            self.warnings.append(f"board outline has {len(open_chains)} unclosed Edge.Cuts chain(s); outline may be incomplete")
        rings.sort(key=lambda r: geom.ring_area(r), reverse=True)
        if rings:
            bbox = geom.bbox_of(rings[0])
        else:
            pts = [(f.x, f.y) for f in footprints] + [(p.x, p.y) for p in pads] + [tuple(t.start) for t in tracks] + [tuple(t.end) for t in tracks] + [(v.x, v.y) for v in vias]
            boxes = [f.bbox for f in footprints]
            base = geom.bbox_of(pts) if pts else [0, 0, 100, 100]
            base = geom.union_bbox([base, *boxes]) or base
            bbox = [base[0] - 1, base[1] - 1, base[2] + 1, base[3] + 1]
            rings = [geom.bbox_ring(bbox)]
            self.warnings.append("no closed board outline on Edge.Cuts; using the bounding box of the parts")
        if open_chains and not rings:
            pass
        return rings, bbox

    # ---- stackup
    def parse_stackup(self, thickness: float) -> dict[str, Any]:
        setup = find(self.root, "setup")
        stack = find(setup, "stackup") if setup else None
        layers: list[dict[str, Any]] = []
        if stack is not None:
            for item in findall(stack, "layer"):
                name = item[1] if len(item) > 1 else ""
                typ = (arg(item, "type", "") or "").lower()
                if name.endswith(".Cu"):
                    kind = "copper"
                elif "dielectric" in name.lower() or typ in ("core", "prepreg", "dielectric"):
                    kind = "dielectric"
                elif "mask" in name.lower() or "mask" in typ:
                    kind = "mask"
                elif "silk" in name.lower() or "silk" in typ:
                    kind = "silk"
                elif "paste" in name.lower():
                    kind = "paste"
                else:
                    kind = "other"
                layers.append(
                    {
                        "name": name,
                        "kind": kind,
                        "thickness_mm": num(item, "thickness"),
                        "epsilon_r": num(item, "epsilon_r"),
                        "loss_tangent": num(item, "loss_tangent"),
                        "material": arg(item, "material"),
                    }
                )
        if any(x["kind"] == "copper" for x in layers):
            # A stackup with copper but no dielectric values is as good as none for physics.
            return {"source": "board", "assumed": False, "layers": layers, "total_thickness_mm": thickness}
        return self.default_stackup(thickness)

    def default_stackup(self, thickness: float) -> dict[str, Any]:
        copper = self.copper or ["F.Cu", "B.Cu"]
        n = len(copper)
        cu = 0.035
        inner = 0.0175 if n > 2 else 0.035
        diel_total = max(thickness - cu * 2 - inner * (n - 2), 0.1)
        per = diel_total / max(n - 1, 1)
        layers = []
        for i, name in enumerate(copper):
            layers.append({"name": name, "kind": "copper", "thickness_mm": cu if i in (0, n - 1) else inner, "epsilon_r": None, "loss_tangent": None, "material": "Copper"})
            if i < n - 1:
                layers.append({"name": f"dielectric {i + 1}", "kind": "dielectric", "thickness_mm": round(per, 4), "epsilon_r": 4.5, "loss_tangent": 0.02, "material": "FR4"})
        return {"source": "default", "assumed": True, "layers": layers, "total_thickness_mm": thickness}

    # ---- net classes from the project file
    def net_classes(self):
        classes = self.project.get("net_settings", {}) if isinstance(self.project, dict) else {}
        patterns = [(p.get("pattern", ""), p.get("netclass", "Default")) for p in classes.get("netclass_patterns", []) if isinstance(p, dict)]
        assignments = classes.get("netclass_assignments") or {}

        def lookup(name: str) -> str:
            value = assignments.get(name) if isinstance(assignments, dict) else None
            if isinstance(value, str):
                return value
            if isinstance(value, list) and value:
                return str(value[0])
            for pat, cls in patterns:
                if fnmatch.fnmatchcase(name, pat):
                    return cls
            return "Default"

        return lookup


def parse_board(path: str | Path, version_id: str = "") -> Board:
    """Parse a ``.kicad_pcb`` file (KiCad 9 or 10). Reads ``<stem>.kicad_pro`` beside it for net classes."""
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    project = None
    pro = path.with_suffix(".kicad_pro")
    if pro.is_file():
        try:
            project = json.loads(pro.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            project = None
    return parse_board_text(text, version_id=version_id, source=str(path), project=project)


def parse_board_text(text: str, version_id: str = "", source: str | None = None, project: dict | None = None) -> Board:
    root = sexpr.parse(text)
    if not root or root[0] != "kicad_pcb":
        raise ValueError("not a KiCad board file (expected a kicad_pcb form)")
    parser = _Parser(root, version_id, source, project)
    parser._pending_zones = []
    parser._pending_keepouts = []
    board = parser.parse()
    # zones declared inside footprints
    if parser._pending_zones or parser._pending_keepouts:
        board.zones.extend(parser._pending_zones)
        board.keepouts.extend(parser._pending_keepouts)
        board._index()
    return board
