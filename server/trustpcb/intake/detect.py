"""Automatic board detection: which files are the project, and is this a wireless board?

``find_project(root)`` locates the board, project, schematic and library files.
``detect(board, ...)`` reads a parsed :class:`~trustpcb.pcb.model.Board` and returns a
:class:`Detection` with the wireless verdict, a band guess, the antenna kind and the evidence
behind each conclusion. Evidence items carry target ids (``ref:U3``, ``net:ANT_FEED``,
``keepout:...``) so the UI can show them on the board. Nothing here simulates anything; the
band is a guess from part families, net names and antenna size, shown to the user for
confirmation on the goal form.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..pcb import geom
from ..pcb.model import Board

C0 = 299_792_458.0

# (pattern, family, technology, band GHz or None, note)
PART_FAMILIES: list[tuple[str, str, str, tuple[float, float] | None]] = [
    (r"nrf5[0-9]\d{2,3}|nrf54|nrf53|nrf51", "Nordic nRF5 series", "Bluetooth LE / 2.4 GHz", (2.4, 2.4835)),
    (r"nrf24", "Nordic nRF24", "2.4 GHz proprietary", (2.4, 2.4835)),
    (r"nrf91|nrf9160", "Nordic nRF91", "LTE-M / NB-IoT", None),
    (r"esp32|esp8266|esp-?wroom|esp-?wrover|esp-?12|esp-?01", "Espressif ESP", "Wi-Fi / Bluetooth LE", (2.4, 2.4835)),
    (r"cc25\d\d|cc26\d\d|cc2[0-9]{3}[a-z]?\b|cc2640|cc2652|cc2538", "TI CC2xxx", "2.4 GHz", (2.4, 2.4835)),
    (r"cc1[0-9]{3}|cc1101|cc1352|cc1312", "TI CC1xxx", "sub-GHz", (0.863, 0.928)),
    (r"sx126\d|sx127\d|rfm9\d|rfm69|lora|sx130\d|lr11\d\d|rak\d{4}", "Semtech LoRa", "LoRa sub-GHz", (0.863, 0.928)),
    (r"sx128\d", "Semtech SX128x", "LoRa 2.4 GHz", (2.4, 2.4835)),
    (r"efr32|bgm\d|mgm\d|bg[0-9]{2}\b|bl6[0-9]{2}", "Silicon Labs", "Bluetooth / 2.4 GHz", (2.4, 2.4835)),
    (r"stm32wb|stm32wl", "STM32 wireless", "Bluetooth LE / sub-GHz", None),
    (r"mdbt\d|raytac|fanstel|ublox.*nina|nina-?b|nina-?w|annabelle", "BLE module", "Bluetooth LE", (2.4, 2.4835)),
    (r"atwinc|wf121|cyw\d{4,5}|wl18\d\d|rtl8\d{3}|wilc", "Wi-Fi chip", "Wi-Fi", (2.4, 2.4835)),
    (r"max-?m\d|neo-?[mn]\d|sam-?m\d|l76|ublox|u-blox|gnss|quectel.*l\d{2}", "GNSS receiver", "GNSS (L1)", (1.559, 1.610)),
    (r"sim7\d\d\d|sim8\d\d|bg9[0-9]|ec2[0-9]|sara-?[a-z]\d", "Cellular modem", "cellular", None),
]
_FAMILY_RE = [(re.compile(p, re.I), name, tech, band) for p, name, tech, band in PART_FAMILIES]

_NET_RE = re.compile(r"(^|[^A-Za-z])(ANT|ANTENNA|RFIN|RFOUT|RF_?IN|RF_?OUT|RF|BLE|WIFI|WLAN|LORA|2G4|SUBG|LNA|PA_?OUT)([^A-Za-z]|$)", re.I)
_ANT_NET_RE = re.compile(r"(^|[^A-Za-z])(ANT|ANTENNA)([^A-Za-z]|$)", re.I)
_CHIP_ANT_RE = re.compile(r"antenna|chip_?ant|2450ab|2450at|ant[0-9]*$|ant_|_ant", re.I)
_MODULE_RE = re.compile(r"esp32-?wroom|esp32-?wrover|esp-?12|mdbt|bmd-?\d|raytac|nina|rak\d{4}|wroom|wrover|e73|e104|e18|minew", re.I)


@dataclass
class Evidence:
    kind: str  # part | net | keepout | antenna_shape | file | size
    text: str
    targets: list[str] = field(default_factory=list)
    weight: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "text": self.text, "targets": self.targets, "weight": self.weight}


@dataclass
class Detection:
    wireless: bool
    band: dict[str, Any] | None
    antenna: dict[str, Any] | None
    evidence: list[Evidence]
    parts: list[dict[str, Any]]
    keepouts: list[dict[str, Any]]
    ambiguous: bool = False
    confidence: str = "none"  # none | low | medium | high

    def to_dict(self) -> dict[str, Any]:
        return {
            "wireless": self.wireless,
            "confidence": self.confidence,
            "ambiguous": self.ambiguous,
            "band": self.band,
            "antenna": self.antenna,
            "parts": self.parts,
            "keepouts": self.keepouts,
            "evidence": [e.to_dict() for e in self.evidence],
        }


# ------------------------------------------------------------------------------ files


def find_project(root: str | Path) -> dict[str, Any]:
    """Locate the KiCad files under ``root``. Values are paths relative to ``root`` or ``None``."""
    root = Path(root)
    boards = sorted(root.rglob("*.kicad_pcb"), key=lambda p: (not p.with_suffix(".kicad_pro").is_file(), len(p.parts), str(p)))
    pcb = boards[0] if boards else None
    rel = lambda p: str(p.relative_to(root)) if p else None  # noqa: E731
    pro = sch = None
    if pcb:
        pro = pcb.with_suffix(".kicad_pro")
        pro = pro if pro.is_file() else next(iter(sorted(pcb.parent.glob("*.kicad_pro"))), None)
        sch = pcb.with_suffix(".kicad_sch")
        sch = sch if sch.is_file() else next(iter(sorted(pcb.parent.glob("*.kicad_sch"))), None)
    libs = sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.name in ("fp-lib-table", "sym-lib-table") or p.suffix in (".kicad_sym",) or p.name.endswith(".pretty"))
    pdfs = sorted(str(p.relative_to(root)) for p in root.rglob("*.pdf"))
    return {
        "pcb": rel(pcb),
        "pro": rel(pro),
        "sch": rel(sch),
        "libraries": libs,
        "datasheets": pdfs,
        "other_boards": [rel(b) for b in boards[1:]],
        "has_project_file": pro is not None,
        "has_schematic": sch is not None,
    }


# -------------------------------------------------------------------------- detection


def _schematic_parts(sch: Path | None) -> list[tuple[str, str]]:
    """``(lib_id, value)`` pairs from schematic symbol instances, found by a cheap text scan."""
    if sch is None or not sch.is_file():
        return []
    try:
        text = sch.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out = []
    for m in re.finditer(r'\(lib_id "([^"]+)"', text):
        out.append((m.group(1), ""))
    for m in re.finditer(r'\(property "Value" "([^"]*)"', text):
        out.append(("", m.group(1)))
    return out


def _classify_part(text: str):
    for rx, name, tech, band in _FAMILY_RE:
        if rx.search(text):
            return name, tech, band
    return None


def detect(board: Board, schematic: str | Path | None = None) -> Detection:
    evidence: list[Evidence] = []
    parts: list[dict[str, Any]] = []
    bands: list[tuple[float, float]] = []
    techs: list[str] = []
    seen_refs: set[str] = set()

    for f in board.footprints:
        hay = f"{f.value} {f.lib_id.split(':')[-1]}"
        hit = _classify_part(hay)
        if hit is None:
            continue
        family, tech, band = hit
        seen_refs.add(f.ref)
        parts.append({"ref": f.ref, "value": f.value, "family": family, "technology": tech, "band_ghz": list(band) if band else None})
        evidence.append(Evidence("part", f"{f.ref} ({f.value}) is a {family} part: {tech}", [f.id], 3))
        techs.append(tech)
        if band:
            bands.append(band)
    for lib_id, value in _schematic_parts(Path(schematic) if schematic else None):
        hit = _classify_part(f"{lib_id} {value}")
        if hit and not any(p["family"] == hit[0] for p in parts):
            family, tech, band = hit
            parts.append({"ref": None, "value": value or lib_id, "family": family, "technology": tech, "band_ghz": list(band) if band else None})
            evidence.append(Evidence("part", f"schematic contains a {family} part ({value or lib_id}): {tech}", [], 2))
            techs.append(tech)
            if band:
                bands.append(band)

    rf_nets = [n.name for n in board.nets if _NET_RE.search(n.name)]
    ant_nets = [n for n in rf_nets if _ANT_NET_RE.search(n)]
    for name in rf_nets:
        evidence.append(Evidence("net", f"net name {name} looks like an RF net", [f"net:{name}"], 2 if name in ant_nets else 1))

    keepouts: list[dict[str, Any]] = []
    for k in board.keepouts:
        box = geom.bbox_of(k.ring)
        blocks_copper = any(k.rules.get(r) == "not_allowed" for r in ("tracks", "copperpour", "pads", "vias"))
        antenna_named = bool(k.name and re.search(r"ant|rf|keep", k.name, re.I))
        edge = _near_edge(board, box)
        keepouts.append({"id": k.id, "name": k.name, "layers": k.layers, "bbox": [round(v, 3) for v in box], "blocks_copper": blocks_copper, "near_edge": edge})
        if blocks_copper and (antenna_named and re.search(r"ant|rf", k.name or "", re.I)):
            evidence.append(Evidence("keepout", f"keep-out zone {k.name} blocks copper at the board edge" if edge else f"keep-out zone {k.name} blocks copper", [k.id], 3))
        elif blocks_copper and edge:
            evidence.append(Evidence("keepout", "a copper keep-out zone sits at the board edge, where antennas are usually placed", [k.id], 1))

    antenna = _find_antenna(board, ant_nets, keepouts, evidence)

    # footprint-based antennas and modules
    for f in board.footprints:
        hay = f"{f.value} {f.lib_id.split(':')[-1]}"
        if f.ref in seen_refs and _MODULE_RE.search(hay) and antenna is None:
            antenna = {"type": "module", "targets": [f.id], "note": "module with a built-in antenna; placement review only"}
            evidence.append(Evidence("part", f"{f.ref} looks like a module with a built-in antenna", [f.id], 2))
        elif _CHIP_ANT_RE.search(f.value) and f.ref[:1] in ("A", "E", "X") and antenna is None:
            antenna = {"type": "chip", "targets": [f.id], "note": "chip antenna; keep-clear review only"}
            evidence.append(Evidence("part", f"{f.ref} ({f.value}) looks like a chip antenna", [f.id], 3))

    # band
    band: dict[str, Any] | None = None
    if antenna and antenna.get("type") == "patch" and antenna.get("estimated_f0_ghz"):
        f0 = antenna["estimated_f0_ghz"]
        band = _band_around(f0, "estimated from the patch dimensions and stackup")
    if band is None and bands:
        lo = min(b[0] for b in bands)
        hi = max(b[1] for b in bands)
        distinct = {(round(b[0], 3), round(b[1], 3)) for b in bands}
        label = _band_label(lo, hi)
        band = {"low_ghz": lo, "high_ghz": hi, "label": label, "source": "parts", "confidence": "high" if len(distinct) == 1 else "medium"}
    elif band is None and (ant_nets or antenna):
        band = None

    score = sum(e.weight for e in evidence)
    strong_parts = any(e.kind == "part" and e.weight >= 2 for e in evidence)
    wireless = score >= 3 or strong_parts or bool(ant_nets and antenna)
    ambiguous = wireless and band is None
    if band and len({(round(b[0], 2), round(b[1], 2)) for b in bands}) > 1:
        ambiguous = True
    confidence = "none" if not wireless else "high" if (strong_parts and (ant_nets or antenna)) else "medium" if strong_parts or score >= 4 else "low"
    return Detection(wireless, band, antenna, evidence, parts, keepouts, ambiguous, confidence)


def _band_label(lo: float, hi: float) -> str:
    if lo >= 2.39 and hi <= 2.5:
        return "2.4 GHz ISM (2.4 to 2.4835 GHz)"
    if lo >= 0.86 and hi <= 0.93:
        return "sub-GHz ISM (863 to 928 MHz)"
    if 1.5 <= lo and hi <= 1.65:
        return "GNSS L1 (1.559 to 1.610 GHz)"
    return f"{lo:.3f} to {hi:.3f} GHz"


def _band_around(f0_ghz: float, source: str) -> dict[str, Any]:
    if 2.3 <= f0_ghz <= 2.6:
        lo, hi = 2.4, 2.4835
        label = _band_label(lo, hi)
    elif 0.8 <= f0_ghz <= 1.0:
        lo, hi = 0.863, 0.928
        label = _band_label(lo, hi)
    else:
        lo, hi = f0_ghz * 0.97, f0_ghz * 1.03
        label = f"around {f0_ghz:.2f} GHz"
    return {"low_ghz": lo, "high_ghz": hi, "label": label, "source": source, "confidence": "low", "estimated_f0_ghz": round(f0_ghz, 3)}


def _near_edge(board: Board, box: list[float], tolerance: float = 3.0) -> bool:
    x0, y0, x1, y1 = board.bbox
    return box[0] <= x0 + tolerance or box[1] <= y0 + tolerance or box[2] >= x1 - tolerance or box[3] >= y1 - tolerance


# ------------------------------------------------------------------- antenna geometry


def _find_antenna(board: Board, ant_nets: list[str], keepouts: list[dict[str, Any]], evidence: list[Evidence]) -> dict[str, Any] | None:
    # Patch: a large copper pour on an antenna-named net.
    for z in board.zones:
        if z.net and z.net in ant_nets:
            rings = [r for rs in z.filled.values() for r in rs] or ([z.outline] if z.outline else [])
            if not rings:
                continue
            area = sum(geom.ring_area(r) for r in rings)
            box = geom.union_bbox(geom.bbox_of(r) for r in rings)
            if area >= 100.0:
                w, h = box[2] - box[0], box[3] - box[1]
                f0 = _patch_f0(board, w, h)
                evidence.append(Evidence("antenna_shape", f"{area:.0f} mm2 copper pour on net {z.net} ({w:.1f} x {h:.1f} mm): a patch antenna", [z.id, f"net:{z.net}"], 3))
                return {
                    "type": "patch",
                    "net": z.net,
                    "targets": [z.id],
                    "bbox": [round(v, 3) for v in box],
                    "size_mm": [round(w, 3), round(h, 3)],
                    "estimated_f0_ghz": round(f0, 3) if f0 else None,
                }
    # Trace antenna: tracks on an antenna-named net, or tracks inside an antenna keep-out.
    k_boxes = [k for k in keepouts if k["blocks_copper"]]
    candidates: dict[str, list] = {}
    for t in board.tracks:
        inside = any(_in_box(t.start, k["bbox"]) and _in_box(t.end, k["bbox"]) and (t.layer in k["layers"]) for k in k_boxes)
        if (t.net and t.net in ant_nets) or inside:
            candidates.setdefault(t.net or "", []).append(t)
    best = None
    for net, tracks in candidates.items():
        info = _trace_shape(board, tracks, k_boxes)
        score = (info["inside_keepout"], info["bends"], info["length_mm"])
        if best is None or score > best[0]:
            best = (score, net, tracks, info)
    if best is None:
        return None
    _, net, tracks, info = best
    if info["length_mm"] < 5.0 or info["segments"] < 2:
        return None
    kind = "meander trace" if info["bends"] >= 4 else "trace"
    ids = [t.id for t in tracks]
    evidence.append(
        Evidence(
            "antenna_shape",
            f"{len(tracks)} tracks on {net or 'an unnamed net'} form a {kind} antenna ({info['length_mm']:.1f} mm, {info['bends']} bends"
            + (", open end" if info["open_ends"] else "")
            + (", inside the keep-out" if info["inside_keepout"] else "")
            + ")",
            ids[:50] + ([f"net:{net}"] if net else []),
            3,
        )
    )
    return {
        "type": "trace",
        "shape": "meander" if info["bends"] >= 4 else "straight",
        "net": net or None,
        "targets": ([f"net:{net}"] if net else ids),
        "track_ids": ids,
        "length_mm": round(info["length_mm"], 3),
        "bends": info["bends"],
        "open_ends": info["open_ends"],
        "feed_point": info["feed_point"],
        "layer": tracks[0].layer,
        "in_keepout": bool(info["inside_keepout"]),
    }


def _in_box(p: list[float], b: list[float]) -> bool:
    return b[0] - 1e-6 <= p[0] <= b[2] + 1e-6 and b[1] - 1e-6 <= p[1] <= b[3] + 1e-6


def _trace_shape(board: Board, tracks, keepouts: list[dict[str, Any]]) -> dict[str, Any]:
    key = lambda p: (round(p[0], 3), round(p[1], 3))  # noqa: E731
    degree: dict[tuple, int] = {}
    for t in tracks:
        for p in (t.start, t.end):
            degree[key(p)] = degree.get(key(p), 0) + 1
    pad_pts = {key((p.x, p.y)) for p in board.pads}
    via_pts = {key((v.x, v.y)) for v in board.vias}
    ends = [k for k, d in degree.items() if d == 1]
    # An end that lands on a pad or via is a connection (a feed); one that does not is open.
    open_ends, feeds = [], []
    for e in ends:
        landed = any(math.dist(e, q) < 0.35 for q in list(pad_pts) + list(via_pts))
        (feeds if landed else open_ends).append([e[0], e[1]])
    bends = 0
    by_end: dict[tuple, list] = {}
    for t in tracks:
        by_end.setdefault(key(t.start), []).append(t)
        by_end.setdefault(key(t.end), []).append(t)
    for p, ts in by_end.items():
        if len(ts) == 2:
            a, b = ts
            va = _dir(a, p)
            vb = _dir(b, p)
            if va and vb:
                cos = (va[0] * vb[0] + va[1] * vb[1])
                # directions point away from the joint; a straight run has cos = -1
                if cos > -0.94:
                    bends += 1
    length = sum(t.length for t in tracks)
    k_in = sum(1 for t in tracks if any(_in_box(t.start, k["bbox"]) and _in_box(t.end, k["bbox"]) for k in keepouts))
    return {
        "segments": len(tracks),
        "length_mm": length,
        "bends": bends,
        "open_ends": open_ends,
        "feed_point": feeds[0] if feeds else None,
        "inside_keepout": k_in,
    }


def _dir(t, joint) -> tuple[float, float] | None:
    ends = [t.start, t.end]
    far = ends[1] if math.dist(ends[0], joint) < math.dist(ends[1], joint) else ends[0]
    dx, dy = far[0] - joint[0], far[1] - joint[1]
    n = math.hypot(dx, dy)
    return (dx / n, dy / n) if n > 1e-9 else None


def _patch_f0(board: Board, w: float, h: float) -> float | None:
    """Resonant frequency of a rectangular patch of the zone's size over the board stackup.

    The resonant length is the shorter side, extended by the fringing length at each radiating edge.
    This is a closed-form guess used only to pick a band for the goal form, never a result.
    """
    eps = None
    sub_h = None
    for layer in board.stackup.get("layers", []):
        if layer.get("kind") == "dielectric" and layer.get("epsilon_r"):
            eps = layer["epsilon_r"]
            sub_h = layer.get("thickness_mm")
            break
    if not eps or not sub_h:
        return None
    length, width = min(w, h), max(w, h)
    eeff = (eps + 1) / 2 + (eps - 1) / 2 / math.sqrt(1 + 12 * sub_h / width)
    ext = 0.412 * sub_h * ((eeff + 0.3) * (width / sub_h + 0.264)) / ((eeff - 0.258) * (width / sub_h + 0.8))
    return C0 / (2 * (length + 2 * ext) * 1e-3 * math.sqrt(eeff)) / 1e9
