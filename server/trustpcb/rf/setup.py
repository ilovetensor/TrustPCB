"""Choose what to simulate: antenna, port, ground reference, band, region.

``plan_setup(board, detection)`` returns an ``RfSetup`` or a ``NotSimulatable`` with a reason. It
never guesses: a feed that cannot be told apart, a port without a ground plane next to it, a
chip antenna or a module all end as ``NotSimulatable``.
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

from shapely.geometry import Point, box
from shapely.ops import unary_union

from ..pcb.model import Board
from . import geometry as geo
from .types import C0, MatchElement, MatchNetwork, NotSimulatable, RfSetup

PASSIVE_PREFIXES = {"R": "R", "L": "L", "C": "C", "FB": "FB"}
IC_SKIP_PREFIXES = {"H", "MH", "TP", "MK", "FID", "G", "LOGO", "REF"}
_UNIT = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3, "k": 1e3, "M": 1e6, "G": 1e9, "": 1.0}

DEFAULT_PORT_OHM = 50.0
MIN_RESOLVED_GAP_MM = 0.25
STRIP_PORT_MIN_WIDTH_MM = 1.0


# --------------------------------------------------------------------------- helpers


def ref_prefix(ref: str) -> str:
    m = re.match(r"^([A-Za-z]+)", ref or "")
    return m.group(1).upper() if m else ""


def parse_value(kind: str, text: str) -> tuple[float | None, bool]:
    """Read a part value such as ``2.2nH``, ``DNP 0.5pF`` or ``100k``. Returns (value, dnp)."""
    t = (text or "").strip()
    dnp = bool(re.search(r"\b(DNP|DNF|NC|NP|NOT[_ ]FITTED)\b|^DNP", t, re.I)) or bool(re.search(r"(^|[_\s])DNP", t, re.I))
    t2 = re.sub(r"\b(DNP|DNF|NP|NC)\b", " ", t, flags=re.I).strip()
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*([pnuµmkMG]?)\s*([HhFfΩRr]?)", t2)
    if not m or not re.search(r"\d", t2):
        return None, dnp
    num = float(m.group(1).replace(",", "."))
    unit = m.group(2)
    tail = m.group(3)
    # "4n7" style: digit-letter-digit
    m2 = re.search(r"(\d+)([pnuµkMG])(\d+)", t2)
    if m2:
        num = float(f"{m2.group(1)}.{m2.group(3)}")
        unit = m2.group(2)
    val = num * _UNIT.get(unit, 1.0)
    if kind == "R" and not unit and not tail and not re.search(r"[RΩr]", t2):
        return None, dnp  # a bare number is ambiguous
    if kind in ("L", "C") and not unit and not tail:
        return None, dnp
    return val, dnp


def infer_kind(ref_kind: str, text: str) -> str:
    """The kind of a passive part. The unit in the value text wins over the reference letter: a series capacitor can sit at ref L1.

    ``0.47pF`` is a capacitor and ``2.2nH`` an inductor wherever they are fitted. A value with no unit letter (``100k``, ``4p7``)
    keeps the kind the reference letter suggests. A ferrite bead (FB) is kept as FB.
    """
    if ref_kind in ("FB",):
        return ref_kind
    t = re.sub(r"\b(DNP|DNF|NP|NC)\b", " ", text or "", flags=re.I)
    m = re.search(r"\d\s*[pnuµμm]?\s*([FfHhΩ])(?![A-Za-z])", t)
    if m:
        return {"f": "C", "h": "L", "ω": "R"}[m.group(1).lower()]
    return ref_kind


def _stackup(board: Board) -> tuple[dict[str, Any] | None, NotSimulatable | None, list[str]]:
    st = board.stackup or {}
    layers = st.get("layers") or []
    copper = [l for l in layers if l.get("kind") == "copper"]
    if len(copper) < 2:
        return None, NotSimulatable("no_stackup", "the board stackup has fewer than two copper layers"), []
    assumptions = []
    if st.get("assumed") or st.get("source") == "default":
        assumptions.append("stackup is a default (the board file has no stackup): FR4, no measured permittivity")
    # bottom to top
    bottom_up = list(reversed(layers))
    z = 0.0
    zmap: dict[str, float] = {}
    slabs = []
    prev_cu_t = None
    pending: list[dict] = []
    for l in bottom_up:
        if l["kind"] == "copper":
            t = float(l.get("thickness_mm") or 0.035)
            if prev_cu_t is not None and pending:
                diel = sum(p["thickness_mm"] for p in pending)
                z0 = z
                z = z + diel + (prev_cu_t + t) / 2
                slabs_z0 = z0 - prev_cu_t / 2 if False else z0
                # one slab per dielectric layer between the two copper sheets, scaled to fill the gap
                gap = z - z0
                cur = z0
                for p in pending:
                    h = gap * p["thickness_mm"] / diel
                    slabs.append({"zmin": cur, "zmax": cur + h, "epsilon_r": p["epsilon_r"], "loss_tangent": p["loss_tangent"], "name": p["name"]})
                    cur += h
                pending = []
            zmap[l["name"]] = round(z, 5)
            prev_cu_t = t
        elif l["kind"] == "dielectric":
            if not l.get("epsilon_r") or not l.get("thickness_mm"):
                return None, NotSimulatable("stackup_incomplete", f"dielectric layer {l.get('name')} has no permittivity or thickness"), []
            pending.append({"name": l["name"], "thickness_mm": float(l["thickness_mm"]), "epsilon_r": float(l["epsilon_r"]),
                            "loss_tangent": float(l.get("loss_tangent") or 0.02)})
    if pending:
        return None, NotSimulatable("stackup_incomplete", "the stackup has dielectric below the lowest copper layer"), []
    names = {l.name for l in board.layers if l.kind == "copper"}
    missing = names - set(zmap)
    if missing:
        return None, NotSimulatable("stackup_mismatch", f"copper layers missing from the stackup: {sorted(missing)}"), []
    out = {"layer_z": zmap, "slabs": slabs, "total_mm": round(z, 5), "copper_order": sorted(zmap, key=zmap.get)}
    return out, None, assumptions


def _ground_nets(board: Board) -> set[str]:
    g = {n.name for n in board.nets if geo.is_ground_name(n.name)}
    if g:
        return g
    # no net is named like ground: the net with the most zone area is the best candidate
    area: dict[str, float] = {}
    for z in board.zones:
        if z.net and not z.is_keepout:
            for rs in z.filled.values():
                for r in rs:
                    area[z.net] = area.get(z.net, 0.0) + abs(_ring_area(r))
    return {max(area, key=area.get)} if area else set()


def _ring_area(r):
    a = 0.0
    for (x0, y0), (x1, y1) in zip(r, r[1:] + r[:1]):
        a += x0 * y1 - x1 * y0
    return a / 2


def _is_passive(ref: str) -> bool:
    return ref_prefix(ref) in PASSIVE_PREFIXES


# --------------------------------------------------------------------------- network


def discover_network(board: Board, antenna_net: str, ground: set[str]) -> MatchNetwork:
    """Walk from the antenna net toward the radio through two-pad passives."""
    pads_by_net: dict[str, list] = {}
    for p in board.pads:
        if p.net:
            pads_by_net.setdefault(p.net, []).append(p)
    parts: dict[str, list] = {}
    for p in board.pads:
        parts.setdefault(p.ref, []).append(p)
    notes: list[str] = []
    understood = True
    chain = [antenna_net]
    elements: list[MatchElement] = []
    source = None
    visited = {antenna_net}
    cur = antenna_net
    position = 0
    for _ in range(8):
        shunts, series = [], []
        for p in pads_by_net.get(cur, []):
            if not _is_passive(p.ref):
                continue
            pp = parts[p.ref]
            if len(pp) != 2:
                notes.append(f"{p.ref} has {len(pp)} pads; ignored")
                continue
            other = pp[1] if pp[0] is p else pp[0]
            if other.net == cur:
                notes.append(f"{p.ref} has both pads on {cur}: bypassed copper, ignored")
                continue
            (shunts if (other.net in ground or other.net is None and False) else series).append((p, other))
        for p, other in shunts:
            elements.append(_element(board, p, other, "shunt", cur, position))
            position += 1
        series = [(p, o) for p, o in series if o.net not in visited or o.net is None]
        nonpassive = [p for p in pads_by_net.get(cur, []) if not _is_passive(p.ref) and ref_prefix(p.ref) not in IC_SKIP_PREFIXES]
        if nonpassive:
            source = nonpassive[0].id
            if series:
                notes.append(f"{cur} reaches {nonpassive[0].id} and also continues through {series[0][0].ref}; the part is treated as a branch")
                understood = False
            break
        if not series:
            break
        if len(series) > 1:
            notes.append(f"{cur} branches through {', '.join(p.ref for p, _ in series)}")
            understood = False
            break
        p, other = series[0]
        if other.net is None:
            notes.append(f"{p.ref} continues to an unconnected pad")
            understood = False
            break
        elements.append(_element(board, p, other, "series", cur, position))
        position += 1
        visited.add(other.net)
        chain.append(other.net)
        cur = other.net
    for e in elements:
        if e.populated and (e.value is None or e.kind not in ("L", "C", "R")):
            understood = False
            notes.append(f"{e.ref} is fitted but its value {e.value_text!r} could not be read as a {e.kind}")
    return MatchNetwork(chain, elements, source, understood, notes)


def _element(board: Board, p, other, topology: str, chain_net: str, position: int) -> MatchElement:
    fp = board.footprint(p.ref)
    text = fp.value if fp else ""
    kind = infer_kind(PASSIVE_PREFIXES.get(ref_prefix(p.ref), "?"), text)
    value, dnp_text = parse_value("L" if kind == "FB" else kind, text)
    dnp = bool(fp.dnp) if fp else False
    if dnp_text:
        dnp = True
    return MatchElement(p.ref, kind, value, text, not dnp, topology, (p.net or "", other.net or ""), chain_net, position)


# --------------------------------------------------------------------------- main


def plan_setup(
    board: Board,
    detection=None,
    *,
    schematic: str | Path | None = None,
    band_ghz: tuple[float, float] | None = None,
    margin_mm: float | None = None,
) -> RfSetup | NotSimulatable:
    from ..intake.detect import detect

    det = detection if detection is not None else detect(board, schematic)
    if not det.wireless:
        return NotSimulatable("not_wireless", "no wireless part or antenna was found on this board")
    ant = det.antenna
    if ant is None:
        return NotSimulatable("no_antenna", "no printed antenna was detected; keep-clear review only", {"confidence": det.confidence})
    if ant["type"] == "chip":
        return NotSimulatable("chip_antenna", "chip antenna: keep-clear review only, simulation Not tested", {"targets": ant.get("targets")})
    if ant["type"] == "module":
        return NotSimulatable("module", "module with a built-in antenna: placement review only, simulation Not tested", {"targets": ant.get("targets")})
    if ant["type"] not in ("trace", "patch"):
        return NotSimulatable("unsupported_antenna", f"antenna type {ant['type']!r} is not simulated in V1")

    if band_ghz is None:
        b = det.band
        if not b:
            return NotSimulatable("band_unknown", "the operating band is not known; confirm it on the goal form")
        band_ghz = (float(b["low_ghz"]), float(b["high_ghz"]))
    lo, hi = band_ghz
    if not (0.1 <= lo < hi <= 10.0):
        return NotSimulatable("band_invalid", f"band {lo}-{hi} GHz is not usable")

    st, err, assumptions = _stackup(board)
    if err:
        return err
    net = ant.get("net")
    if not net:
        return NotSimulatable("antenna_net_unknown", "the antenna copper has no net, so the feed cannot be found")
    ground = _ground_nets(board)
    if not ground:
        return NotSimulatable("no_ground", "no ground net or ground copper was found")

    items = geo.collect_items(board, _Net(net))
    ant_items = [i for i in items if i.antenna]
    if not ant_items:
        return NotSimulatable("no_antenna_copper", f"no copper found on net {net}")

    # ---- feed pad ------------------------------------------------------------
    pads = [p for p in board.pads if p.net == net]
    cands = []
    for p in pads:
        pref = ref_prefix(p.ref)
        if pref in IC_SKIP_PREFIXES:
            continue
        if _is_passive(p.ref):
            pp = board.pads_of(p.ref)
            if len(pp) != 2:
                continue
            other = pp[1] if pp[0] is p else pp[0]
            if other.net and other.net != net and other.net not in ground:
                cands.append((p, "series"))
        else:
            cands.append((p, "source"))
    fp_hint = ant.get("feed_point")
    chosen = None
    if fp_hint:
        near = [(math.dist((p.x, p.y), fp_hint), p, kind) for p, kind in cands]
        near = [n for n in near if n[0] < 0.6]
        near.sort(key=lambda n: n[0])
        if near:
            chosen = (near[0][1], near[0][2])
    if chosen is None:
        sources = [c for c in cands if c[1] == "source"]
        pool = sources if sources else cands
        if len(pool) == 1:
            chosen = pool[0]
    if chosen is None:
        return NotSimulatable(
            "feed_ambiguous" if cands else "no_feed",
            "cannot tell where the radio feeds the antenna" if cands else f"no feed pad found on net {net}: no connector pin, radio pin or series part",
            {"candidates": [c[0].id for c in cands]},
        )
    feed_pad, feed_kind = chosen
    copper_names = [l.name for l in board.layers if l.kind == "copper"]
    port_layer = ant.get("layer") if ant.get("layer") in feed_pad.layers else next((l for l in feed_pad.layers if l in copper_names), None)
    if port_layer is None:
        return NotSimulatable("feed_not_copper", f"feed pad {feed_pad.id} has no copper layer")
    port_xy = [round(feed_pad.x, 4), round(feed_pad.y, 4)]

    # ---- port width and direction ----------------------------------------
    pb = box(feed_pad.x - feed_pad.w / 2 - 0.05, feed_pad.y - feed_pad.h / 2 - 0.05, feed_pad.x + feed_pad.w / 2 + 0.05, feed_pad.y + feed_pad.h / 2 + 0.05)
    port_width, port_axis = 0.0, "x"
    attached = [i for i in ant_items if i.kind == "track" and i.layer == port_layer and i.points and (pb.contains(Point(i.points[0])) or pb.contains(Point(i.points[-1])))]
    if attached:
        t = max(attached, key=lambda i: i.width or 0)
        a, b = t.points[0], t.points[-1]
        d = (b[0] - a[0], b[1] - a[1]) if pb.contains(Point(a)) else (a[0] - b[0], a[1] - b[1])
        if (t.width or 0) >= STRIP_PORT_MIN_WIDTH_MM:
            port_axis = "x" if abs(d[1]) >= abs(d[0]) else "y"
            across = min(feed_pad.w, feed_pad.h) if False else (feed_pad.w if port_axis == "x" else feed_pad.h)
            port_width = round(0.9 * min(t.width, across), 3)

    # ---- ground reference ----------------------------------------------------
    order = st["copper_order"]
    zp = st["layer_z"][port_layer]
    others = [l for l in order if l != port_layer]
    others.sort(key=lambda l: abs(st["layer_z"][l] - zp))
    probe = Point(port_xy).buffer(max(port_width / 2, 0.3))
    ground_layer = None
    why = []
    for l in others:
        gz = unary_union([i.geom for i in items if i.layer == l and i.kind == "zone" and i.net in ground]) if any(
            i.layer == l and i.kind == "zone" and i.net in ground for i in items) else None
        if gz is None or gz.is_empty:
            why.append(f"{l}: no ground pour")
            continue
        if not gz.buffer(1e-6).contains(probe):
            why.append(f"{l}: ground pour does not cover the feed point")
            continue
        ground_layer = l
        break
    if ground_layer is None:
        return NotSimulatable("no_ground_reference", "no ground copper under the feed point, so the port has no real return path",
                              {"port_xy": port_xy, "checked": why})
    iz = order.index
    if abs(iz(ground_layer) - iz(port_layer)) != 1:
        return NotSimulatable("ground_not_adjacent", f"the nearest ground under the feed is {ground_layer}, with copper layers between it and {port_layer}",
                              {"port_xy": port_xy})
    # copper of another net on the ground layer at the port would short the lumped port
    # (it is covered by the ground pour test: the pour contains the probe, so no clearance cut)

    # ---- geometry-derived numbers --------------------------------------------
    abox = geo.union_of(ant_items).bounds
    abox = [min(abox[0], port_xy[0]), min(abox[1], port_xy[1]), max(abox[2], port_xy[0]), max(abox[3], port_xy[1])]
    bx0, by0, bx1, by1 = board.bbox
    f0 = (lo + hi) / 2 * 1e9
    sweep_fc = 0.35 * f0
    lam = C0 / f0 * 1e3
    margin = margin_mm if margin_mm is not None else lam / 4
    region = [max(bx0, abox[0] - margin), max(by0, abox[1] - margin), min(bx1, abox[2] + margin), min(by1, abox[3] + margin)]
    snap = 0.2 * margin
    if region[0] - bx0 < snap:
        region[0] = bx0
    if region[1] - by0 < snap:
        region[1] = by0
    if bx1 - region[2] < snap:
        region[2] = bx1
    if by1 - region[3] < snap:
        region[3] = by1
    fz_pad = 4.0
    fine = [abox[0] - fz_pad, abox[1] - fz_pad, abox[2] + fz_pad, abox[3] + fz_pad]
    for k in det.keepouts:
        kb = k["bbox"]
        if k.get("blocks_copper") and not (kb[2] < abox[0] or kb[0] > abox[2] or kb[3] < abox[1] or kb[1] > abox[3]):
            fine = [min(fine[0], kb[0]), min(fine[1], kb[1]), max(fine[2], kb[2]), max(fine[3], kb[3])]
    fine = [max(fine[0], region[0]), max(fine[1], region[1]), min(fine[2], region[2]), min(fine[3], region[3])]

    narrowest, what, small_gaps = _narrowest(items, ant_items, port_layer, fine)
    network = discover_network(board, net, ground)

    assumptions += [
        "copper is modelled as zero-thickness sheets; solder mask, silkscreen and component bodies are ignored",
        "only the copper of components is modelled (pads); part bodies, shields and cables are not",
        "outside the fine zone only pours and ground/antenna copper are kept, and they are simplified",
        "50 ohm lumped port between the feed copper and the ground layer; matching parts are added afterwards as ideal lumped parts",
        "the board edge is cut to a rectangle at the simulated region; absorbing boundary instead of an open field",
    ]
    if network and not network.understood:
        assumptions.append("matching network could not be fully understood; only the antenna-alone result is reliable")
    warnings: list[str] = []
    if small_gaps:
        warnings.append(
            f"{len(small_gaps)} gaps between antenna copper and other copper are under {MIN_RESOLVED_GAP_MM} mm "
            f"(smallest {min(small_gaps):.2f} mm: component pad gaps and pour clearance); they are not resolved by the mesh and are not part of the 2-cell rule"
        )
    return RfSetup(
        kind=ant["type"],
        antenna_id=(ant.get("targets") or [f"net:{net}"])[0],
        antenna_net=net,
        antenna_layer=ant.get("layer") or port_layer,
        antenna_targets=list(ant.get("targets") or []),
        antenna_bbox=[round(v, 4) for v in abox],
        open_end=ant.get("open_ends", [None])[0] if ant.get("open_ends") else None,
        port_xy=port_xy,
        port_on=feed_pad.id,
        port_layer=port_layer,
        ground_layer=ground_layer,
        ground_net=next(iter(sorted(ground))),
        port_width_mm=port_width,
        port_axis=port_axis,
        port_impedance_ohm=DEFAULT_PORT_OHM,
        band_ghz=[lo, hi],
        f0_hz=f0,
        fc_hz=sweep_fc,
        region=[round(v, 4) for v in region],
        fine_zone=[round(v, 4) for v in fine],
        narrowest_mm=round(narrowest, 4),
        narrowest_what=what,
        network=network,
        stackup=st,
        assumptions=assumptions,
        warnings=warnings,
    )


class _Net:
    """Minimal stand-in so collect_items can flag antenna copper before a setup exists."""

    def __init__(self, net: str):
        self.antenna_net = net


def _narrowest(items, ant_items, layer: str, fine) -> tuple[float, str, list[float]]:
    """Narrowest antenna copper width, or gap to other copper, inside the fine zone.

    Gaps under MIN_RESOLVED_GAP_MM (pad gaps inside a footprint, pour clearance) are returned
    separately: the mesh does not resolve them and the 2-cell rule does not apply to them.
    """
    fb = box(*fine)
    best, what = 1e9, ""
    small: list[float] = []
    for i in ant_items:
        if i.kind == "track" and i.width and i.geom.intersects(fb) and i.width < best:
            best, what = i.width, f"antenna track width on {i.layer} ({i.source})"
        if i.kind == "pad" and i.geom.intersects(fb):
            b = i.geom.bounds
            m = min(b[2] - b[0], b[3] - b[1])
            if m < best:
                best, what = m, f"antenna pad width ({i.source})"
    for l in {i.layer for i in ant_items}:
        a = geo.union_of([i for i in ant_items if i.layer == l])
        others = [i for i in items if i.layer == l and not i.antenna and i.geom.intersects(fb.buffer(0.5))]
        for o in others:
            d = a.distance(o.geom)
            if d < 1e-6:
                continue
            if d < MIN_RESOLVED_GAP_MM:
                small.append(d)
            elif d < best:
                best, what = d, f"gap between antenna copper and {o.source} on {l}"
    return (best if best < 1e8 else 1.0), what or "no feature measured", small
