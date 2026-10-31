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

from shapely.geometry import LineString, Point, box
from shapely.ops import unary_union

from ..partvalue import parse_value as read_value
from ..pcb.model import Board
from . import geometry as geo
from .types import C0, MatchElement, MatchNetwork, NotSimulatable, RfSetup

PASSIVE_PREFIXES = {"R": "R", "L": "L", "C": "C", "FB": "FB"}
IC_SKIP_PREFIXES = {"H", "MH", "TP", "MK", "FID", "G", "LOGO", "REF"}

DEFAULT_PORT_OHM = 50.0
MIN_RESOLVED_GAP_MM = 0.25
STRIP_PORT_MIN_WIDTH_MM = 1.0


# --------------------------------------------------------------------------- helpers


def ref_prefix(ref: str) -> str:
    m = re.match(r"^([A-Za-z]+)", ref or "")
    return m.group(1).upper() if m else ""


def parse_value(kind: str, text: str) -> tuple[float | None, bool]:
    """Read a part value such as ``2.2nH``, ``DNP 0.5pF`` or ``100k``. Returns (value, dnp).

    The value comes from ``trustpcb.partvalue``: ``None`` when the unit cannot be known (a bare number on
    a capacitor or inductor) or the text is not one value.
    """
    t = (text or "").strip()
    dnp = bool(re.search(r"\b(DNP|DNF|NC|NP|NOT[_ ]FITTED)\b|^DNP", t, re.I)) or bool(re.search(r"(^|[_\s])DNP", t, re.I))
    return read_value(t, kind), dnp


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


DEFAULT_ASSUMED_EPSILON_R = 4.4
DEFAULT_ASSUMED_LOSS_TANGENT = 0.02
#: boards in the wild run from about 0.4 mm to 2.4 mm; the total outside this range is a stackup that was not set by the designer
TYPICAL_THICKNESS_MM = (0.4, 2.4)
#: dielectric thickness KiCad writes for every dielectric of a new stackup
KICAD_DEFAULT_DIELECTRIC_MM = 1.51


def typical_stackup(board: Board, total_mm: float, *, epsilon_r: float = DEFAULT_ASSUMED_EPSILON_R, loss_tangent: float = DEFAULT_ASSUMED_LOSS_TANGENT,
                    outer_dielectric_mm: float | None = None) -> dict[str, Any]:
    """An assumed stackup for the board's copper layers, with a stated total thickness (the user's stand-in for the real one).

    Outer copper 35 um and inner copper 17.5 um. With more than two copper layers the two outer dielectrics (prepreg) are
    ``outer_dielectric_mm`` thick (0.2 mm for four layers, 0.11 mm for more) and the rest of the thickness is shared by the inner
    dielectrics. One permittivity and loss tangent for every dielectric. The result is labelled ``assumed_by_user``.
    """
    cu = board.copper_layers()
    if len(cu) < 2:
        raise ValueError("the board has fewer than two copper layers")
    if not (0.2 <= float(total_mm) <= 6.0):
        raise ValueError(f"total thickness {total_mm} mm is outside 0.2 to 6 mm")
    n = len(cu)
    outer_t, inner_t = 0.035, 0.0175
    copper_total = 2 * outer_t + (n - 2) * inner_t
    diel_total = float(total_mm) - copper_total
    gaps = n - 1
    if n == 2:
        thick = [diel_total]
    else:
        outer = outer_dielectric_mm if outer_dielectric_mm else (0.2 if n == 4 else 0.11)
        outer = min(outer, diel_total / (gaps + 1))
        inner = (diel_total - 2 * outer) / max(gaps - 2, 1)
        thick = [outer] + [inner] * (gaps - 2) + [outer]
    layers = []
    for i, name in enumerate(cu):
        layers.append({"name": name, "kind": "copper", "thickness_mm": outer_t if i in (0, n - 1) else inner_t, "epsilon_r": None, "loss_tangent": None, "material": "Copper"})
        if i < n - 1:
            layers.append({"name": f"dielectric {i + 1}", "kind": "dielectric", "thickness_mm": round(thick[i], 4), "epsilon_r": float(epsilon_r),
                           "loss_tangent": float(loss_tangent), "material": "FR4 (assumed)", "user_provided": True})
    return {"source": "assumed_by_user", "assumed": True, "layers": layers, "total_thickness_mm": float(total_mm)}


def stackup_check(st: dict[str, Any]) -> list[str]:
    """Reasons to doubt that a stackup describes a real board. Empty when it looks plausible."""
    layers = st.get("layers") or []
    cu = sum(float(l.get("thickness_mm") or 0) for l in layers if l.get("kind") == "copper")
    diel = [l for l in layers if l.get("kind") == "dielectric"]
    total = cu + sum(float(l.get("thickness_mm") or 0) for l in diel)
    out = []
    if total and not (TYPICAL_THICKNESS_MM[0] <= total <= TYPICAL_THICKNESS_MM[1]):
        out.append(f"the stackup totals {total:.2f} mm, far from the 0.4 to 2.4 mm of real boards")
    if len(diel) >= 2 and all(abs(float(l.get("thickness_mm") or 0) - KICAD_DEFAULT_DIELECTRIC_MM) < 1e-6 for l in diel):
        out.append(f"every dielectric is {KICAD_DEFAULT_DIELECTRIC_MM} mm, the value KiCad writes for a new stackup: it was never set to the fabricator's")
    return out


def _stackup(board: Board, override: dict[str, Any] | None = None) -> tuple[dict[str, Any] | None, NotSimulatable | None, list[str]]:
    st = override or board.stackup or {}
    layers = st.get("layers") or []
    copper = [l for l in layers if l.get("kind") == "copper"]
    if len(copper) < 2:
        return None, NotSimulatable("no_stackup", "the board stackup has fewer than two copper layers"), []
    assumptions = []
    if st.get("source") == "assumed_by_user":
        d = [l for l in layers if l.get("kind") == "dielectric"]
        assumptions.append(
            f"ASSUMED STACKUP supplied by the user, not read from the board file: {st.get('total_thickness_mm')} mm total, dielectric "
            f"{', '.join(str(l['thickness_mm']) for l in d)} mm (top to bottom), permittivity {d[0]['epsilon_r']}, loss tangent {d[0]['loss_tangent']}; "
            "an antenna's resonant frequency depends strongly on these"
        )
    elif st.get("assumed") or st.get("source") == "default":
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


def discover_network(board: Board, antenna_net: str, ground: set[str], ignore_refs: set[str] | None = None) -> MatchNetwork:
    """Walk from the antenna net toward the radio through two-pad passives. Pads of ``ignore_refs`` (the antenna footprint) are not parts of the path."""
    pads_by_net: dict[str, list] = {}
    for p in board.pads:
        if p.net and not (ignore_refs and p.ref in ignore_refs):
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
    assumed_stackup: dict[str, Any] | None = None,
) -> RfSetup | NotSimulatable:
    """Plan the simulation.

    ``assumed_stackup`` replaces the board's stackup with a labelled assumption: ``{"total_thickness_mm": 1.6}`` plus optionally
    ``epsilon_r``, ``loss_tangent`` and ``outer_dielectric_mm`` (see :func:`typical_stackup`). Without it the stackup in the board file
    is used, and a stackup that does not look like a real board (a KiCad default) is flagged in the assumptions and warnings.
    """
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
    if ant["type"] == "coil":
        return NotSimulatable("coil_antenna", "NFC or RFID coil antenna: not an RF antenna this model simulates", {"targets": ant.get("targets")})
    if ant["type"] == "external":
        return NotSimulatable("external_antenna", ant.get("note") or "the antenna is external to this board", {"targets": ant.get("targets")})
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

    if ant.get("confidence") == "low" or ant.get("unsure"):
        return NotSimulatable(
            "antenna_uncertain",
            "something that may be a printed antenna was found, but the evidence is too thin to simulate it: " + "; ".join(ant.get("confidence_reasons") or ["no reason recorded"]),
            {"candidate": ant.get("footprint") or ant.get("net"), "confidence": "low"},
        )
    user_stack = None
    if assumed_stackup:
        try:
            user_stack = typical_stackup(board, float(assumed_stackup.get("total_thickness_mm") or assumed_stackup.get("total_mm")),
                                         epsilon_r=float(assumed_stackup.get("epsilon_r") or DEFAULT_ASSUMED_EPSILON_R),
                                         loss_tangent=float(assumed_stackup.get("loss_tangent") or DEFAULT_ASSUMED_LOSS_TANGENT),
                                         outer_dielectric_mm=assumed_stackup.get("outer_dielectric_mm"))
        except (TypeError, ValueError) as exc:
            return NotSimulatable("bad_assumed_stackup", f"the assumed stackup cannot be used: {exc}")
    st, err, assumptions = _stackup(board, user_stack)
    if err:
        return err
    stack_doubts = [] if user_stack else stackup_check(board.stackup or {})
    net = ant.get("net")
    if not net:
        return NotSimulatable("antenna_net_unknown", "the antenna copper has no net, so the feed cannot be found")
    ground = _ground_nets(board)
    if not ground:
        return NotSimulatable("no_ground", "no ground net or ground copper was found")
    ant_fp = ant.get("footprint") if ant.get("source") == "footprint" else None

    items = geo.collect_items(board, _Net(net, ant_fp))
    ant_items = [i for i in items if i.antenna]
    if not ant_items:
        return NotSimulatable("no_antenna_copper", f"no copper found on net {net}")

    # ---- feed pad ------------------------------------------------------------
    # The antenna's own pads are not candidates for the feed: for a footprint antenna they are the radiator (or its ground stub).
    own_refs = {ant_fp} if ant_fp else set()
    # a footprint with several pads on the antenna net (an antenna drawn as pads) is the radiator, not a place to feed it
    per_ref: dict[str, int] = {}
    for p in board.pads:
        if p.net == net:
            per_ref[p.ref] = per_ref.get(p.ref, 0) + 1
    own_refs |= {r for r, n in per_ref.items() if n >= 3}
    own_pads = {p.id for r in own_refs for p in board.pads_of(r)}
    pads = [p for p in board.pads if p.net == net and p.id not in own_pads]
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
    fp_hint = ant.get("feed_point") if not ant_fp else None
    chosen = None
    if fp_hint:
        near = [(math.dist((p.x, p.y), fp_hint), p, kind) for p, kind in cands]
        near = [n for n in near if n[0] < 0.6]
        near.sort(key=lambda n: n[0])
        if near:
            chosen = (near[0][1], near[0][2])
    if chosen is None and ant_fp and ant.get("feed_pad") and not ant.get("pad_antenna"):
        # a footprint antenna with one feed pad of its own: the radiator starts at that pad
        fpad = next((p for p in board.pads_of(ant_fp) if p.id == ant["feed_pad"]), None)
        if fpad is not None:
            chosen = (fpad, "antenna_pad")
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
            across = feed_pad.w if port_axis == "x" else feed_pad.h
            port_width = round(0.9 * min(t.width, across), 3)

    # ---- ground reference ----------------------------------------------------
    order = st["copper_order"]
    zp = st["layer_z"][port_layer]
    others = [l for l in order if l != port_layer]
    others.sort(key=lambda l: abs(st["layer_z"][l] - zp))
    ground_pour = {}
    for l in others:
        gz = [i.geom for i in items if i.layer == l and i.kind == "zone" and i.net in ground]
        ground_pour[l] = unary_union(gz) if gz else None

    def ground_under(pt, radius):
        """The nearest copper layer whose ground pour covers a disc of ``radius`` around the point, with the reasons the others failed."""
        probe, why_ = Point(pt).buffer(radius), []
        for l in others:
            gz = ground_pour[l]
            if gz is None or gz.is_empty:
                why_.append(f"{l}: no ground pour")
            elif not gz.buffer(1e-6).contains(probe):
                why_.append(f"{l}: ground pour does not cover the feed point")
            else:
                return l, why_
        return None, why_

    port_mode, feed_line_mm = "pad", 0.0
    ground_layer, why = ground_under(port_xy, max(port_width / 2, 0.3))
    if ground_layer is None and ant_fp:
        # The feed pad lies in the copper-free antenna area, so a port straight down from it has no ground under it. A real inverted-F is
        # fed at the edge of the ground plane, through a feed line that crosses onto ground. The port goes where that line is a microstrip
        # over solid ground (the textbook lumped port, between the strip and the plane directly below); the line between the antenna pad
        # and the port stays in the model, so the feed is physical and the matching network is applied at the port plane.
        found = _feed_line_port(board, items, net, port_layer, (feed_pad.x, feed_pad.y), order, st, ground_under)
        if found is not None:
            port_xy, ground_layer, feed_line_mm, why_line = found
            port_mode = "feed_line"
            port_width = 0.0
        else:
            why = why + ["no point along the feed line has solid ground under it"]
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
    network = discover_network(board, net, ground, own_refs or None)

    stack_origin, stack_note = ("assumed_by_user", None) if user_stack else ("board", None)
    if stack_doubts:
        stack_origin = "suspect_default"
        stack_note = ("The stackup in the board file does not look like a real board: " + "; ".join(stack_doubts)
                      + ". The resonant frequency of a printed antenna depends strongly on the dielectric thickness and permittivity, so a result on this stackup says "
                      "little about the built board. Supply the fabricator's stackup (or an assumed one, which is then labelled as such).")
        assumptions.append(stack_note)
        warnings_pre = [stack_note]
    else:
        warnings_pre = []
    if port_mode == "feed_line":
        assumptions.append(
            f"the feed pad {feed_pad.id} lies in the copper-free antenna area with no ground under it, so the 50 ohm port sits on the feed line {feed_line_mm:.2f} mm from the pad, "
            f"where {ground_layer} is solid ground under the line (a microstrip port); the line between the pad and the port is simulated, the matching network is applied at the port plane"
        )
    if ant_fp:
        assumptions.append(f"the radiator is the drawn copper of footprint {ant_fp}; plated holes of that footprint join its layers, and copper on one layer is merged whatever its net (a shorted stub to ground is therefore part of the model)")
    assumptions += [
        "copper is modelled as zero-thickness sheets; solder mask, silkscreen and component bodies are ignored",
        "only the copper of components is modelled (pads); part bodies, shields and cables are not",
        "outside the fine zone only pours and ground/antenna copper are kept, and they are simplified",
        "50 ohm lumped port between the feed copper and the ground layer; matching parts are added afterwards as ideal lumped parts",
        "the board edge is cut to a rectangle at the simulated region; absorbing boundary instead of an open field",
    ]
    if network and not network.understood:
        assumptions.append("matching network could not be fully understood; only the antenna-alone result is reliable")
    warnings: list[str] = list(warnings_pre)
    if ant_fp and not ant.get("ground_pads"):
        warnings.append(f"no ground pad of {ant_fp} touches its copper: a shorted stub to ground was not found, so the model has no such connection")
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
        antenna_footprint=ant_fp,
        port_mode=port_mode,
        feed_line_mm=round(feed_line_mm, 3),
        stackup_origin=stack_origin,
        stackup_note=stack_note,
        antenna_confidence=ant.get("confidence"),
    )


class _Net:
    """Minimal stand-in so collect_items can flag antenna copper before a setup exists."""

    def __init__(self, net: str, antenna_footprint: str | None = None):
        self.antenna_net = net
        self.antenna_footprint = antenna_footprint


def _feed_line_port(board: Board, items, net: str, layer: str, start_xy, order, st, ground_under):
    """Walk the antenna net's tracks from the antenna feed pad and find where the feed line first runs over solid ground.

    Candidate points are taken every 0.1 mm along the line. A point qualifies when ground covers a disc around it (half the line width
    plus twice the dielectric height, between 0.4 and 1 mm, at least 0.3 mm) on the neighbouring copper layer, and keeps covering it for the next 0.8 mm of line,
    so the port sits on a settled microstrip and not on the fringing at the edge of the plane. Returns
    ``(xy, ground_layer, distance from the pad in mm, notes)`` or None.
    """
    tracks = [t for t in board.tracks if t.net == net and t.layer == layer]
    if not tracks:
        return None
    key = lambda p: (round(p[0], 3), round(p[1], 3))  # noqa: E731
    pts: list[tuple[float, float]] = [tuple(start_xy)]
    used: set[str] = set()
    here = tuple(start_xy)
    for _ in range(60):
        best = None
        for t in tracks:
            if t.id in used:
                continue
            tp = [tuple(p) for p in t.points()]
            for rev in (False, True):
                seq = tp[::-1] if rev else tp
                d = math.dist(seq[0], here)
                if d < 0.25 and (best is None or d < best[0]):
                    best = (d, t, seq)
        if best is None:
            break
        _, t, seq = best
        used.add(t.id)
        pts += seq[1:] if math.dist(seq[0], pts[-1]) < 0.05 else seq
        here = seq[-1]
    if len(pts) < 2:
        return None
    line = LineString(pts)
    total = min(line.length, 25.0)
    width = max((t.width for t in tracks), default=0.2)
    zp = st["layer_z"][layer]
    n = int(total / 0.1) + 1
    samples = [line.interpolate(k * 0.1) for k in range(n + 1) if k * 0.1 <= total]
    ok: list[str | None] = []
    for pt in samples:
        gl = None
        for l in sorted((l for l in order if l != layer), key=lambda l: abs(st["layer_z"][l] - zp)):
            h = abs(st["layer_z"][l] - zp)
            got, _ = ground_under((pt.x, pt.y), max(0.3, width / 2 + min(max(2 * h, 0.4), 1.0)))
            if got == l:
                gl = l
                break
            if got is not None:
                gl = got
                break
        ok.append(gl)
    run = 8  # samples in 0.8 mm
    for k in range(len(samples)):
        window = ok[k:k + run]
        if window and all(w is not None for w in window) and len(set(window)) == 1:
            if k + run > len(samples) and total < line.length - 1e-6:
                continue
            return [round(samples[k].x, 4), round(samples[k].y, 4)], window[0], k * 0.1, []
    return None


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
        if i.kind in ("gfx", "pad") and i.min_width and i.geom.intersects(fb):
            if i.min_width < best:
                best, what = i.min_width, f"antenna copper width ({i.source}, drawn shape)"
            continue
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
