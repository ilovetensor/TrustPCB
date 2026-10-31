"""decoupling: capacitor distance from the IC power pin, and vias on the way.

A decoupling capacitor here is a capacitor with one pad on a power net and the other on ground.
Its IC pin is the nearest supply pad of an active part on the same power net: when the schematic is
there, the pins that are named or typed as power (VIN, VDD, DEC4 ...) are preferred over other pins that
only share the net (an enable pin tied to the input), and the pin name is stated. Without a schematic the
nearest pad is used and its function is not known. The distance is pad centre to pad centre. The track path between the two is followed through vias for the via count.

ESD, TVS and other protection parts, found from their symbol value, are not treated as parts that
need decoupling, and neither are capacitors that are not fitted (do-not-populate). Candidates are
the capacitors over the limit, but the distances of every capacitor measured are kept: each
candidate carries ``class_summary`` (min, max, count and the full sorted list for its pin class)
and ``overlays`` returns the same for every class. A pin class is the family of supply pins the
capacitor serves: all ``DEC*`` pins of a radio are the ``DEC`` class, other supplies are named after
their net.

The check also runs from the pin side: for each supply pin of an active part (a pin the schematic names or types as
supply, or any pin on a power net when the schematic does not know it; regulator outputs are not loads), the nearest
fitted capacitor on that net is found, pad centre to pad centre. A pin with no capacitor within the limit is a
candidate ("pin 11 has no capacitor within 3 mm") even when every capacitor on the net is placed well for another pin.
Those candidates are the ``decoupling_pin`` rule (``decoupling_pin.py``); this module measures them (``measure()["pins"]``).
"""

from __future__ import annotations

import heapq
import math
import re
from typing import Any

from ..pcb.model import Board, Pad
from .base import AnalyzerContext, Emitter, mark, meas
from .classify import is_dnp, is_ground, is_power, is_protection_part, part_kind, parse_value
from .schematic import schematic_info
from .topology import Topology, get_topology

RULE = "decoupling"


def track_path(topo: Topology, net: str, start_pad: str, end_pad: str) -> tuple[float, int] | None:
    """Shortest track route between two pads of a net: (length, vias), or None if no track joins them."""
    adj: dict[tuple, list[tuple[tuple, float, int]]] = {}

    def link(a, b, w=0.0, via_step=0):
        adj.setdefault(a, []).append((b, w, 0))
        adj.setdefault(b, []).append((a, w, via_step))

    for key in topo.nodes:
        if key[0] != net:
            continue
        n = ("n", key[1], key[2], key[3])
        for pid in topo.node_pads.get(key, []):
            link(n, ("pad", pid))
        for vid in topo.node_vias.get(key, []):
            # entering the via costs nothing; leaving it onto a track node counts the via
            adj.setdefault(n, []).append((("via", vid), 0.0, 0))
            adj.setdefault(("via", vid), []).append((n, 0.0, 1))
    # a via inside a pad joins the pad directly (a via-in-pad has no track on the pad layer)
    for v in topo.board.vias:
        if v.net != net:
            continue
        for pid in (start_pad, end_pad):
            pad = topo.pad_by_id[pid]
            if set(pad.layers) & set(v.layers) and math.hypot(pad.x - v.x, pad.y - v.y) <= max(pad.w, pad.h) / 2:
                adj.setdefault(("pad", pid), []).append((("via", v.id), 0.0, 0))
                adj.setdefault(("via", v.id), []).append((("pad", pid), 0.0, 0))
    for s in topo.segs:
        if s.net == net:
            a, b = ("n", s.layer, *s.a), ("n", s.layer, *s.b)
            adj.setdefault(a, []).append((b, s.length, 0))
            adj.setdefault(b, []).append((a, s.length, 0))
    start, goal = ("pad", start_pad), ("pad", end_pad)
    if start not in adj or goal not in adj:
        return None
    best: dict[tuple, tuple[float, int]] = {start: (0.0, 0)}
    heap: list[tuple[float, int, tuple]] = [(0.0, 0, start)]
    done: set[tuple] = set()
    while heap:
        d, vias, node = heapq.heappop(heap)
        if node in done:
            continue
        done.add(node)
        if node == goal:
            return d, vias
        for nxt, w, v in adj.get(node, []):
            nd = (d + w, vias + v)
            if nxt not in best or nd < best[nxt]:
                best[nxt] = nd
                heapq.heappush(heap, (nd[0], nd[1], nxt))
    return None


_DEC_RE = re.compile(r"^DEC", re.I)


def pin_class(net: str) -> str:
    """The class of supply pin a net belongs to: ``DEC`` for the radio's DEC pins, else the net name."""
    return "DEC" if _DEC_RE.match(net or "") else (net or "?")


#: roles of parts that make a supply rather than use it; a capacitor on a regulator's output is that regulator's output capacitor
_SUPPLY_ROLES = re.compile(r"regulator|ldo|buck|boost|dc.?dc|charger|pmic|power", re.I)


def choose_partner(pins: list[Pad], cap_fp, cap_pad: Pad, sch, ctx: AnalyzerContext) -> tuple[list[Pad], dict[str, Any]]:
    """Which part's supply pin a capacitor decouples, as far as the design says.

    ``pins`` are the supply pins sharing the capacitor's power net. When they all belong to one part the partner is that part. When several
    parts share the net the nearest pad is NOT taken as the partner (a capacitor moved away from its part would then be measured to a
    neighbour that happens to share the rail). The clues, in order:

    1. a regulator's output pin (the schematic types it ``power_out``) or a part whose role is a regulator is not a load that needs decoupling,
       so it is left out when another part remains;
    2. the capacitor's symbol in the schematic sits beside the pins of one part: the part whose supply pins on this net are the nearest on
       the sheet, at least twice as near as the next part. Both symbols must be in the schematic read.

    Returns the pins to measure against and ``{determined, basis, parts, pins}``. With no clue left, every pin stays a candidate, the nearest
    is measured and ``determined`` is False: the result must say the partner is not known.
    """
    def refs(ps):
        return sorted({p.ref for p in ps})

    if len(refs(pins)) <= 1:
        return pins, {"determined": True, "basis": "the only part with a supply pin on this net", "parts": refs(pins), "pins": [p.id for p in pins]}
    basis = None
    outs = [p for p in pins if (sch.pins.get((p.ref, str(p.number))) or {}).get("type") == "power_out"]
    keep = [p for p in pins if p not in outs]
    if outs and keep and len(refs(keep)) < len(refs(pins)):
        pins, basis = keep, "regulator output pins are not loads: " + ", ".join(refs(outs)) + " left out"
    roles = getattr(ctx, "part_roles", None) or {}
    if len(refs(pins)) > 1 and roles:
        loads = [p for p in pins if not _SUPPLY_ROLES.search(str(roles.get(p.ref, "")))]
        if loads and len(refs(loads)) < len(refs(pins)):
            pins, basis = loads, "part roles: " + ", ".join(sorted(set(refs(pins)) - set(refs(loads)))) + " supply the rail"
    if len(refs(pins)) > 1 and cap_fp.ref in sch.sym_at:
        cx, cy = sch.sym_at[cap_fp.ref]
        near: dict[str, float] = {}
        for p in pins:
            pin = sch.pins.get((p.ref, str(p.number)))
            if pin:
                near[p.ref] = min(near.get(p.ref, 1e9), math.hypot(pin["x"] - cx, pin["y"] - cy))
        if len(near) == len(refs(pins)):
            ranked = sorted(near.items(), key=lambda kv: kv[1])
            if ranked[0][1] * 2.0 <= ranked[1][1]:
                pins = [p for p in pins if p.ref == ranked[0][0]]
                basis = f"the schematic places {cap_fp.ref} beside {ranked[0][0]} ({ranked[0][1]:.0f} mm on the sheet, next part {ranked[1][1]:.0f} mm)"
    if len(refs(pins)) == 1:
        return pins, {"determined": True, "basis": basis or "the only part with a supply pin on this net", "parts": refs(pins), "pins": [p.id for p in pins]}
    return pins, {"determined": False, "basis": "several parts share this rail and nothing in the schematic or the part roles says which one the capacitor serves", "parts": refs(pins), "pins": [p.id for p in pins]}


def measure(board: Board, ctx: AnalyzerContext) -> dict[str, Any]:
    """Every capacitor on a supply pin of an active part: {rows, classes, excluded}. Cached on the context."""
    cached = ctx.cache.get("decoupling")
    if cached is not None and cached[0] is board:
        return cached[1]
    topo = get_topology(board, ctx)
    sch = schematic_info(board, ctx)
    ic_refs: set[str] = set()
    excluded: list[dict[str, str]] = []
    for f in board.footprints:
        if part_kind(f) in ("ic", "antenna") and len(f.pad_ids) > 2:
            if is_protection_part(f, ctx):
                excluded.append({"ref": f.ref, "value": f.value, "reason": "protection part (read from its symbol value): not treated as needing decoupling"})
            else:
                ic_refs.add(f.ref)
    # nets that an active part's pins treat as power (named supply nets plus anything a brief lists)
    pins_by_net: dict[str, list[Pad]] = {}
    for p in board.pads:
        if p.ref in ic_refs and p.net and not is_ground(p.net):
            pins_by_net.setdefault(p.net, []).append(p)
    rows: list[dict[str, Any]] = []
    for fp in sorted(board.footprints, key=lambda f: f.ref):
        if part_kind(fp) != "cap":
            continue
        if is_dnp(fp):
            excluded.append({"ref": fp.ref, "value": fp.value, "reason": "not fitted (do-not-populate)"})
            continue
        pads = board.pads_of(fp.ref)
        if len(pads) != 2:
            continue
        gnd = [p for p in pads if is_ground(p.net)]
        pwr = [p for p in pads if p.net and not is_ground(p.net) and is_power(p.net, ctx)]
        if len(gnd) != 1 or len(pwr) != 1:
            continue
        cap_pad = pwr[0]
        targets = [p for p in pins_by_net.get(cap_pad.net, []) if p.ref != fp.ref]
        if not targets:
            continue
        # a pin the schematic names or types as a supply pin is the one the capacitor serves; an enable or other pin that shares the net is passed over
        supply = [p for p in targets if sch.is_supply_pin(p.ref, p.number)]
        candidates_pins = supply or targets
        candidates_pins, partner = choose_partner(candidates_pins, fp, cap_pad, sch, ctx)
        pin = min(candidates_pins, key=lambda p: (math.dist((p.x, p.y), (cap_pad.x, cap_pad.y)), p.id))
        passed_over = [f"{p.ref} pin {p.number} ({sch.pin_name(p.ref, p.number)})" for p in targets if supply and p not in supply and p.ref == pin.ref and sch.pin_name(p.ref, p.number)]
        d = math.dist((pin.x, pin.y), (cap_pad.x, cap_pad.y))
        path = track_path(topo, cap_pad.net, cap_pad.id, pin.id)
        rows.append({"fp": fp, "cap_pad": cap_pad, "pin": pin, "distance": d, "path": path, "vias": path[1] if path else 0, "pin_class": pin_class(cap_pad.net),
                     "pin_name": sch.pin_name(pin.ref, pin.number), "passed_over": passed_over, "partner": partner})
    classes: dict[str, dict[str, Any]] = {}
    for r in sorted(rows, key=lambda r: (r["pin_class"], r["distance"], r["fp"].ref)):
        c = classes.setdefault(r["pin_class"], {"pin_class": r["pin_class"], "capacitors": []})
        c["capacitors"].append(
            {"capacitor": r["fp"].ref, "ic": r["pin"].ref, "ic_pin": r["pin"].number, **({"ic_pin_name": r["pin_name"]} if r["pin_name"] else {}), "net": r["cap_pad"].net,
             "distance_mm": round(r["distance"], 2), "vias": r["vias"]}
        )
    max_mm = float(ctx.t("decoupling_max_mm"))
    for c in classes.values():
        ds = [x["distance_mm"] for x in c["capacitors"]]
        c["count"] = len(ds)
        c["min_mm"], c["max_mm"] = min(ds), max(ds)
        c["over_limit"] = sum(1 for x in c["capacitors"] if x["distance_mm"] > max_mm)
        c["limit_mm"] = max_mm
    max_mm = float(ctx.t("decoupling_max_mm"))
    # the pin side: nearest fitted decoupling capacitor to each supply pin
    caps_by_net: dict[str, list[tuple[Any, Pad]]] = {}
    for fp in board.footprints:  # any fitted capacitor from a net to ground, whether or not the net is named like a supply
        if part_kind(fp) != "cap" or is_dnp(fp):
            continue
        cpads = board.pads_of(fp.ref)
        if len(cpads) == 2 and sum(1 for p in cpads if is_ground(p.net)) == 1:
            hot = next(p for p in cpads if not is_ground(p.net))
            if hot.net:
                caps_by_net.setdefault(hot.net, []).append((fp, hot))
    pin_rows: list[dict[str, Any]] = []
    seen_pins: set[tuple[str, str]] = set()
    for net, pins in sorted(pins_by_net.items()):
        for pin in sorted(pins, key=lambda p: (p.ref, p.number, p.id)):
            key = (pin.ref, str(pin.number))
            if not pin.number or key in seen_pins:
                continue
            known = sch.is_supply_pin(pin.ref, pin.number)
            if known is False or (known is None and not is_power(net, ctx)):
                continue
            if (sch.pins.get(key) or {}).get("type") == "power_out":
                continue
            seen_pins.add(key)
            cands = [(fp, cp, math.dist((pin.x, pin.y), (cp.x, cp.y))) for fp, cp in caps_by_net.get(net, []) if fp.ref != pin.ref]
            near = min(cands, key=lambda c: (c[2], c[0].ref)) if cands else None
            pin_rows.append({"pin": pin, "net": net, "pin_name": sch.pin_name(pin.ref, pin.number), "nearest": near, "on_net": len(cands),
                             "within": sum(1 for c in cands if c[2] <= max_mm)})
    result = {"rows": rows, "classes": classes, "excluded": excluded, "pins": pin_rows}
    ctx.cache["decoupling"] = (board, result)
    return result


def analyze(board: Board, ctx: AnalyzerContext) -> list[dict[str, Any]]:
    max_mm = float(ctx.t("decoupling_max_mm"))
    max_vias = int(ctx.t("decoupling_via_max"))
    data = measure(board, ctx)
    em = Emitter(RULE)
    for r in data["rows"]:
        fp, cap_pad, pin, d, path, vias = r["fp"], r["cap_pad"], r["pin"], r["distance"], r["path"], r["vias"]
        if d <= max_mm and vias <= max_vias:
            continue
        m: dict[str, Any] = {"distance": meas(d, "mm", 2), "limit": meas(max_mm, "mm", 2), "vias": meas(vias, "count")}
        if path:
            m["track_length"] = meas(path[0], "mm", 2)
        cap_value = parse_value(fp.value)
        cls = data["classes"][r["pin_class"]]
        name = r["pin_name"]
        part = r["partner"]
        details = [f"Pin {pin.number} of {pin.ref} is named {name} in the schematic." if name else "No schematic pin name is available: the nearest pad of the net is used and its function is not known."]
        if part["determined"] and len(part["pins"]) > 1:
            details.append(f"{pin.ref} has {len(part['pins'])} supply pins on this net ({', '.join(x.split(':')[-1] for x in part['pins'])}); the design does not say which one {fp.ref} serves, so the nearest is measured.")
        elif part["determined"] and part["basis"] != "the only part with a supply pin on this net":
            details.append(f"{pin.ref} is taken as the part {fp.ref} serves: {part['basis']}.")
        elif not part["determined"]:
            details.append(f"The part {fp.ref} serves cannot be determined: {part['basis']}. Candidates: {', '.join(x.replace('pad:', '') for x in part['pins'])}; the nearest, {pin.ref} pin {pin.number}, is measured.")
        if r["passed_over"]:
            details.append(f"{', '.join(r['passed_over'])} share{'s' if len(r['passed_over']) == 1 else ''} the net but {'is' if len(r['passed_over']) == 1 else 'are'} not a supply pin, so the distance is to the supply pin.")
        em.add(
            f"{fp.ref} is {d:.1f} mm from {pin.ref} pin {pin.number}{f' {name}' if name and name != cap_pad.net else ''} ({cap_pad.net})",
            m,
            [fp.id, cap_pad.id, pin.id],
            [
                mark("link", [cap_pad.id, pin.id], label=f"{fp.ref} to {pin.ref}.{pin.number}{f' {name}' if name else ''}", value=meas(d, "mm", 2)),
                mark("object", [fp.id], label=fp.ref),
            ],
            "medium" if d > 2 * max_mm or vias > 0 else "low",
            position=((cap_pad.x + pin.x) / 2, (cap_pad.y + pin.y) / 2),
            net=cap_pad.net,
            capacitor=fp.ref,
            capacitance_f=cap_value,
            ic=pin.ref,
            ic_pin=pin.number,
            ic_pin_name=name,
            details=details,
            pin_class=r["pin_class"],
            partner_determined=part["determined"],
            partner_basis=part["basis"],
            partner_candidates=part["pins"],
            class_summary=cls,
            measured_capacitors=len(data["rows"]),
        )
    return em.items


def overlays(board: Board, ctx: AnalyzerContext) -> dict[str, Any]:
    data = measure(board, ctx)
    ds = [r["distance"] for r in data["rows"]]
    return {
        "label": "Capacitor to supply-pin distances, pad centre to pad centre, for every capacitor measured (not only those over the limit)",
        "pin_classes": data["classes"],
        "measured_capacitors": len(ds),
        "all_min_mm": round(min(ds), 2) if ds else None,
        "all_max_mm": round(max(ds), 2) if ds else None,
        "excluded": data["excluded"],
        "supply_pins": [
            {"pin": f"{pr['pin'].ref}.{pr['pin'].number}", "name": pr["pin_name"], "net": pr["net"], "nearest_capacitor": pr["nearest"][0].ref if pr["nearest"] else None,
             "nearest_mm": round(pr["nearest"][2], 2) if pr["nearest"] else None, "within_limit": pr["within"]}
            for pr in data["pins"]
        ][:200],
    }
