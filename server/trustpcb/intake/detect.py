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
#: library or value text that names a printed (copper-drawn) antenna
_PRINTED_ANT_RE = re.compile(r"antenna_pcb|pcb_?antenna|pcb[-_ ]ant|(^|[^a-z])ifa([^a-z]|$)|mifa|meander|swra117|swra[0-9]{3}|inverted[-_ ]?f|f[-_ ]antenna|trace[-_ ]?ant|printed[-_ ]?ant|planar[-_ ]?ant", re.I)
#: text that names a chip (ceramic, discrete) antenna
_CHIP_NAME_RE = re.compile(r"chip[-_ ]?ant|ceramic|2450a[bct]|johanson|taoglas|antenova|yageo|rfant|w3008|w3[0-9]{3}\b|ant[-_]?0?[0-9]{3,}", re.I)
_ANT_WORD_RE = re.compile(r"ant(enna|ena)?", re.I)
#: NFC / RFID coils are antennas, but not the radiating kind this product simulates
_COIL_RE = re.compile(r"nfc|rfid|13[._]?56|125\s?k|\bcoil\b|loop[-_ ]?ant|antenna[-_ ]?loop", re.I)
#: net names that read as the RF pin of a radio or the line to its antenna
_RF_PIN_RE = re.compile(r"(^|[^A-Za-z0-9])(RFIO|RF_?IN|RF_?OUT|RF|ANT\d*|ANTENNA|LNA_?IN|PA_?OUT)([^A-Za-z0-9]|$)", re.I)
_POWERISH_RE = re.compile(r"^[+-]?\d*V\d*[A-Z0-9_]*$|^[+-]?(VCC|VDD|VBAT|VBUS|VIN|VSYS|AVDD|DVDD|VDDH?)\b", re.I)
_BAND_NAME_RE = [
    (re.compile(r"2[._]?4\s*-?\s*g(hz)?|2450|2\.4g|bluetooth|ble\b|wifi|wi-fi|wlan|zigbee", re.I), (2.4, 2.4835), "2.4 GHz"),
    (re.compile(r"868|915|sub-?g|lora", re.I), (0.863, 0.928), "sub-GHz"),
    (re.compile(r"\bgps\b|gnss|1575", re.I), (1.559, 1.610), "GNSS"),
]
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

    # nets that run from a radio's RF pin to the antenna through matching parts, whatever they are called
    chains = _rf_chains(board, [p["ref"] for p in parts if p.get("ref")])
    chain_nets: list[str] = []
    for ch in chains:
        for n in ch["nets"]:
            if n not in chain_nets:
                chain_nets.append(n)
        evidence.append(Evidence("net", f"net {ch['nets'][0]} at the RF pin of {ch['radio']} reaches {ch['nets'][-1]} through "
                                 + (", ".join(ch["through"]) if ch["through"] else "no parts"), [f"net:{n}" for n in ch["nets"]] + [f"ref:{r}" for r in ch["through"]], 2))

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

    printed, chips = _footprint_antennas(board, seen_refs, keepouts, evidence)
    antenna = None
    alternatives: list[dict[str, Any]] = []
    if printed:
        printed.sort(key=lambda a: (_CONF_RANK[a["confidence"]], a.get("area_mm2", 0)), reverse=True)
        antenna, alternatives = printed[0], printed[1:]
    if antenna is None:
        antenna = _find_antenna(board, ant_nets, chain_nets, keepouts, evidence)

    # footprint-based chip antennas and modules
    for f in board.footprints:
        hay = f"{f.value} {f.lib_id.split(':')[-1]}"
        if f.ref in seen_refs and _MODULE_RE.search(hay) and antenna is None:
            antenna = {"type": "module", "targets": [f.id], "note": "module with a built-in antenna; placement review only", "confidence": "medium"}
            evidence.append(Evidence("part", f"{f.ref} looks like a module with a built-in antenna", [f.id], 2))
    if antenna is None:
        coil = next((f for f in board.footprints if f.ref not in seen_refs and _COIL_RE.search(f"{f.lib_id.split(':')[-1]} {f.value}") and _ANT_WORD_RE.search(f"{f.lib_id} {f.value} {f.ref}")), None)
        if coil is not None:
            antenna = {"type": "coil", "targets": [coil.id], "footprint": coil.ref, "note": "NFC or RFID coil antenna: not simulated", "confidence": "high"}
            evidence.append(Evidence("part", f"{coil.ref} ({coil.value}) is an NFC or RFID coil antenna", [coil.id], 2))
    if antenna is None and chips:
        f = chips[0]
        antenna = {"type": "chip", "targets": [f.id], "note": "chip antenna; keep-clear review only", "confidence": "high"}
        evidence.append(Evidence("part", f"{f.ref} ({f.value}) looks like a chip antenna", [f.id], 3))
    if antenna is None and chains:
        for ch in chains:
            end = ch["nets"][-1]
            for p in board.pads:
                if p.net == end and re.match(r"^(J|X|CN|P|CON|SMA|U\.?FL)\d*", p.ref, re.I) and len(board.pads_of(p.ref)) <= 6:
                    antenna = {"type": "external", "targets": [f"ref:{p.ref}"], "net": end, "note": f"the RF line ends at connector {p.ref}: the antenna is not on this board", "confidence": "medium"}
                    evidence.append(Evidence("part", f"the RF line from {ch['radio']} ends at connector {p.ref}: an external antenna or cable", [f"ref:{p.ref}", f"net:{end}"], 1))
                    break
            if antenna is not None:
                break
    if antenna is not None and alternatives:
        antenna["alternatives"] = [{"footprint": a.get("footprint"), "confidence": a["confidence"]} for a in alternatives]

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
    if band is None and antenna and antenna.get("source") == "footprint":
        band = _band_from_names(board, antenna)

    score = sum(e.weight for e in evidence)
    strong_parts = any(e.kind == "part" and e.weight >= 2 for e in evidence)
    ant_found = antenna is not None and antenna.get("type") in ("trace", "patch", "chip", "module")
    wireless = score >= 3 or strong_parts or bool(ant_nets and antenna) or bool(antenna and antenna.get("type") in ("trace", "patch") and antenna.get("confidence") in ("high", "medium"))
    ambiguous = wireless and band is None
    if band and len({(round(b[0], 2), round(b[1], 2)) for b in bands}) > 1:
        ambiguous = True
    confidence = "none" if not wireless else "high" if (strong_parts and (ant_nets or antenna)) else "medium" if strong_parts or score >= 4 else "low"
    if wireless and not strong_parts and ant_found:
        confidence = "high" if antenna.get("confidence") == "high" else "medium" if confidence == "low" else confidence
    return Detection(wireless, band, antenna, evidence, parts, keepouts, ambiguous, confidence)


_CONF_RANK = {"low": 0, "medium": 1, "high": 2}


def _band_from_names(board: Board, antenna: dict[str, Any]) -> dict[str, Any] | None:
    """A band guessed from the antenna footprint's own names (``2.4GHz`` in the library name). Medium confidence at best."""
    f = board.footprint(antenna.get("footprint") or "")
    if f is None:
        return None
    hay = f"{f.lib_id} {f.value}"
    for rx, (lo, hi), label in _BAND_NAME_RE:
        if rx.search(hay):
            return {"low_ghz": lo, "high_ghz": hi, "label": _band_label(lo, hi), "source": f"the antenna footprint name ({label})", "confidence": "medium"}
    return None


def _is_ground(name: str | None) -> bool:
    return bool(name and re.match(r"^(/)?(A|D|P|S|E)?(GND|GROUND|VSS|0V)[A-Z0-9_]*$|^GND", name.strip(), re.I))


def _plausible_antenna_net(board: Board, net: str) -> bool:
    """False for ground and supply nets and for any net with a copper pour: a pour is a plane, not an antenna."""
    if _is_ground(net) or _POWERISH_RE.match(net.strip().lstrip("/")):
        return False
    return not any(z.net == net and not z.is_keepout for z in board.zones)


def _passive_prefix(ref: str) -> bool:
    m = re.match(r"^([A-Za-z]+)", ref or "")
    return bool(m and m.group(1).upper() in ("R", "L", "C", "FB", "F", "FL", "Z"))


def _rf_chains(board: Board, radio_refs: list[str]) -> list[dict[str, Any]]:
    """From every radio pad on an RF-named net, follow two-pad passives (matching parts, filters) to the nets beyond.

    Returns ``[{radio, pad, nets: [first, ..., last], through: [refs]}]``. Shunt parts to ground are not followed.
    """
    if not radio_refs:
        return []
    by_net: dict[str, list] = {}
    for p in board.pads:
        if p.net:
            by_net.setdefault(p.net, []).append(p)
    out = []
    for ref in radio_refs:
        for pad in board.pads_of(ref):
            if not pad.net or _is_ground(pad.net) or not _RF_PIN_RE.search(pad.net):
                continue
            nets, through = [pad.net], []
            cur, seen = pad.net, {pad.net}
            for _ in range(5):
                nxt = None
                for q in by_net.get(cur, []):
                    if q.ref == ref or not _passive_prefix(q.ref):
                        continue
                    pp = board.pads_of(q.ref)
                    if len(pp) != 2:
                        continue
                    other = pp[1] if pp[0] is q else pp[0]
                    if other.net and other.net not in seen and not _is_ground(other.net) and not _POWERISH_RE.match(other.net.lstrip("/")):
                        nxt = (q, other)
                        break
                if nxt is None:
                    break
                through.append(nxt[0].ref)
                cur = nxt[1].net
                seen.add(cur)
                nets.append(cur)
            out.append({"radio": ref, "pad": pad.id, "nets": nets, "through": through})
    return out


def _footprint_antennas(board: Board, radio_refs: set[str], keepouts: list[dict[str, Any]], evidence: list[Evidence]):
    """Printed antennas whose copper is drawn in a footprint, and chip antennas.

    A footprint is a printed antenna when it carries copper of its own (drawn polygons, custom pads, or many pads of one net) and its
    names or its surroundings say antenna. A chip antenna is a footprint that names itself an antenna and has no such copper.
    Returns ``(printed antenna dicts, chip footprints)``.
    """
    from shapely.geometry import Point
    from shapely.ops import unary_union

    from ..rf import geometry as geo

    drawn: dict[str, list] = {}
    for _id, layer, owner, g in geo.copper_graphics(board):
        if owner:
            drawn.setdefault(owner, []).append((layer, g))
    printed, chips = [], []
    for f in board.footprints:
        if f.ref in radio_refs:
            continue
        name = f.lib_id.split(":")[-1]
        hay = f"{name} {f.value}"
        pads = board.pads_of(f.ref)
        named_printed = bool(_PRINTED_ANT_RE.search(hay))
        chip_named = bool(_CHIP_NAME_RE.search(hay))
        antish = named_printed or bool(_ANT_WORD_RE.search(hay)) or bool(re.match(r"^(ANT|AE|AN)\d*$", f.ref))
        if _COIL_RE.search(hay):
            continue  # an NFC or RFID coil: not an RF antenna of this product's kind (handled by the caller)
        gfx = drawn.get(f.ref, [])
        custom = [(p, geo.pad_geometry(p)) for p in pads if p.shape == "custom" and getattr(p, "polys", None)]
        nets: dict[str, list] = {}
        for p in pads:
            if p.net and not _is_ground(p.net):
                nets.setdefault(p.net, []).append(p)
        cluster_net, cluster = next(((n, ps) for n, ps in nets.items() if len(ps) >= 4), (None, []))
        parts = [g for _, g in gfx] + [g for _, g in custom if g is not None] + [geo.pad_geometry(p) for p in cluster]
        parts = [g for g in parts if g is not None and not g.is_empty]
        own = unary_union(parts) if parts else None
        area = own.area if own is not None else 0.0
        extent = max(own.bounds[2] - own.bounds[0], own.bounds[3] - own.bounds[1]) if own is not None else 0.0
        if len(nets) > 3 or len(pads) > 16 and not cluster:
            continue
        if own is not None and area >= 5.0 and extent >= 6.0 and not (chip_named and not named_printed):
            layer_of = {}
            for layer, g in gfx:
                layer_of[layer] = layer_of.get(layer, 0.0) + g.area
            for p in cluster:
                for l in p.layers:
                    if l.endswith(".Cu"):
                        layer_of[l] = layer_of.get(l, 0.0) + 1.0
            layer = max(layer_of, key=layer_of.get) if layer_of else (pads[0].layers[0] if pads and pads[0].layers else "F.Cu")
            reasons, conf = [], "low"
            if named_printed:
                reasons.append(f"footprint name {name!r} / value {f.value!r} names a printed antenna")
            elif antish:
                reasons.append(f"footprint name or value says antenna ({name!r} / {f.value!r})")
            reasons.append(f"{area:.1f} mm2 of drawn copper in the footprint, {extent:.1f} mm long")
            value_says_chip = bool(re.search(r"chip|ceramic", f.value, re.I))
            if value_says_chip:
                reasons.append(f"the value text says {f.value!r}, but the footprint carries the antenna copper itself, so it is treated as a printed antenna")
            # which net feeds it, and which pads tie it to ground
            touching = lambda p: (geo.pad_geometry(p) is not None and own.distance(geo.pad_geometry(p)) < 0.05)  # noqa: E731
            feed_nets = {n: ps for n, ps in nets.items() if any(touching(p) for p in ps)}
            feed_net = feed_pad = None
            if cluster_net:
                feed_net = cluster_net
            elif len(feed_nets) == 1:
                feed_net = next(iter(feed_nets))
            elif len(nets) == 1:
                feed_net = next(iter(nets))
            if feed_net and not cluster_net:
                touching_pads = [p for p in nets[feed_net] if touching(p)] or nets[feed_net]
                feed_pad = touching_pads[0]
            gnd_pads = [p for p in pads if _is_ground(p.net) and touching(p)]
            if feed_net:
                reasons.append(f"one feed net ({feed_net})")
            if gnd_pads:
                reasons.append(f"copper ties to ground through {', '.join(p.id for p in gnd_pads)} (shorted stub)")
            # a copper-free area around it, beside ground
            clear = _clearance_facts(board, own, f.ref, feed_net)
            reasons.append(clear["text"])
            if (named_printed or antish) and own is not None:
                conf = "high" if (named_printed and feed_net and clear["foreign_fraction"] < 0.05 and not value_says_chip) else "medium"
                if feed_net is None:
                    conf = "low"
                    reasons.append("the feed net could not be told apart")
            else:
                # geometry only: nothing in the names says antenna. Only parts that are labelled like an antenna or an unnamed radio part qualify:
                # a connector, a battery holder or a socket that carries copper is not one.
                pm = re.match(r"[A-Za-z]+", f.ref)
                if not pm or pm.group(0).upper() not in ("A", "AE", "ANT", "E", "X", "W", "FL"):
                    continue
                    continue
                conf = "medium" if (feed_net and clear["foreign_fraction"] < 0.02 and clear["ground_near"] and gnd_pads) else "low"
                reasons.append("no antenna wording in the names: judged from the geometry alone")
            ant = {
                "type": "trace", "shape": "footprint", "source": "footprint", "footprint": f.ref, "net": feed_net, "targets": [f.id],
                "layer": layer, "area_mm2": round(area, 2), "length_mm": round(extent, 2), "bends": None,
                "bbox": [round(v, 3) for v in own.bounds], "open_ends": [], "in_keepout": clear["in_keepout"] or any(
                    k["blocks_copper"] and not (k["bbox"][2] < own.bounds[0] or k["bbox"][0] > own.bounds[2] or k["bbox"][3] < own.bounds[1] or k["bbox"][1] > own.bounds[3]) for k in keepouts),
                "feed_pad": feed_pad.id if feed_pad else None, "feed_point": [round(feed_pad.x, 4), round(feed_pad.y, 4)] if feed_pad else None,
                "ground_pads": [p.id for p in gnd_pads], "pad_antenna": bool(cluster_net), "confidence": conf, "confidence_reasons": reasons,
                "pads_of_antenna": [p.id for p in cluster],
            }
            if conf == "low":
                ant["unsure"] = True
            printed.append(ant)
            evidence.append(Evidence("antenna_shape", f"{f.ref} ({name}) is a printed antenna drawn in its footprint ({conf} confidence): " + "; ".join(reasons[:3]),
                                     [f.id] + ([f"net:{feed_net}"] if feed_net else []), 3 if conf != "low" else 1))
        elif antish and (chip_named or _CHIP_ANT_RE.search(f.value) or (area < 5.0 and not named_printed)) and f.ref[:1] in ("A", "E", "X"):
            chips.append(f)
    return printed, chips


def _clearance_facts(board: Board, own, ref: str, feed_net: str | None) -> dict[str, Any]:
    """How much foreign copper (other nets, other parts) sits inside the antenna's hull on any layer, and whether ground is close."""
    from shapely.geometry import box as sbox
    from shapely.ops import unary_union

    from ..rf import geometry as geo

    hull = own.convex_hull.buffer(0.3)
    near = sbox(*hull.buffer(4.0).bounds)
    foreign, ground = [], []
    for z in board.zones:
        if z.is_keepout:
            continue
        for rings in z.filled.values():
            for r in rings:
                b = geom.bbox_of(r)
                if b[2] < near.bounds[0] or b[0] > near.bounds[2] or b[3] < near.bounds[1] or b[1] > near.bounds[3]:
                    continue
                g = geo.ring_polygon(r)
                if g is None:
                    continue
                (ground if _is_ground(z.net) else foreign).append(g.intersection(near))
    for t in board.tracks:
        if t.net == feed_net:
            continue
        g = geo.track_polygon([tuple(p) for p in t.points()], t.width)
        if g is not None and g.intersects(near):
            (ground if _is_ground(t.net) else foreign).append(g)
    for p in board.pads:
        if p.ref == ref or p.net == feed_net:
            continue
        g = geo.pad_geometry(p)
        if g is not None and g.intersects(near):
            (ground if _is_ground(p.net) else foreign).append(g)
    f_u = unary_union(foreign).intersection(hull) if foreign else None
    frac = (f_u.area / hull.area) if f_u is not None and not f_u.is_empty and hull.area > 0 else 0.0
    gdist = unary_union(ground).distance(own) if ground else None
    ground_near = gdist is not None and gdist <= 3.0
    text = (f"{frac * 100:.0f}% of the area around it holds other copper" if frac >= 0.005 else "the area around it is free of other copper")
    text += f"; ground copper {gdist:.1f} mm away" if gdist is not None else "; no ground copper within 4 mm"
    return {"foreign_fraction": frac, "ground_near": ground_near, "ground_distance_mm": gdist, "text": text, "in_keepout": False}


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


def _find_antenna(board: Board, ant_nets: list[str], chain_nets: list[str], keepouts: list[dict[str, Any]], evidence: list[Evidence]) -> dict[str, Any] | None:
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
                    "confidence": "high",
                }
    # Trace antenna: tracks on an antenna-named net or on a net that runs from a radio's RF pin, or tracks inside an antenna keep-out.
    # Ground and supply nets and nets with a pour never count: ground tracks inside a keep-out are not an antenna.
    k_boxes = [k for k in keepouts if k["blocks_copper"]]
    named = set(ant_nets)
    chained = set(chain_nets) - named
    candidates: dict[str, list] = {}
    for t in board.tracks:
        if not t.net:
            continue
        inside = any(_in_box(t.start, k["bbox"]) and _in_box(t.end, k["bbox"]) and (t.layer in k["layers"]) for k in k_boxes)
        if (t.net in named or t.net in chained or inside) and _plausible_antenna_net(board, t.net):
            candidates.setdefault(t.net, []).append(t)
    best = None
    for net, tracks in candidates.items():
        info = _trace_shape(board, tracks, k_boxes)
        score = (net in named, info["inside_keepout"], info["bends"], info["length_mm"])
        if best is None or score > best[0]:
            best = (score, net, tracks, info)
    if best is None:
        return None
    _, net, tracks, info = best
    if info["length_mm"] < 5.0 or info["segments"] < 2:
        return None
    # a net that is only reached through matching parts, or only sits in a keep-out, must look like an antenna: a bent run with an open end
    if net not in named and not (info["open_ends"] and (info["bends"] >= 2 or info["inside_keepout"])):
        return None
    kind = "meander trace" if info["bends"] >= 4 else "trace"
    ids = [t.id for t in tracks]
    reasons = []
    if net in named:
        reasons.append("the net name says antenna")
    if net in chained:
        reasons.append("the net is reached from a radio's RF pin through matching parts")
    if info["inside_keepout"]:
        reasons.append("the tracks lie in a copper keep-out zone")
    if info["open_ends"]:
        reasons.append("the track has an open end")
    conf = "high" if (net in named and info["inside_keepout"]) or (net in named and info["open_ends"] and info["bends"] >= 2) else "medium" if (net in named or net in chained) else "low"
    evidence.append(
        Evidence(
            "antenna_shape",
            f"{len(tracks)} tracks on {net} form a {kind} antenna ({info['length_mm']:.1f} mm, {info['bends']} bends"
            + (", open end" if info["open_ends"] else "")
            + (", inside the keep-out" if info["inside_keepout"] else "")
            + ")",
            ids[:50] + [f"net:{net}"],
            3,
        )
    )
    out = {
        "type": "trace",
        "shape": "meander" if info["bends"] >= 4 else "straight",
        "net": net,
        "targets": [f"net:{net}"],
        "track_ids": ids,
        "length_mm": round(info["length_mm"], 3),
        "bends": info["bends"],
        "open_ends": info["open_ends"],
        "feed_point": info["feed_point"],
        "layer": tracks[0].layer,
        "in_keepout": bool(info["inside_keepout"]),
        "confidence": conf,
        "confidence_reasons": reasons,
    }
    if conf == "low":
        out["unsure"] = True
    return out


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
