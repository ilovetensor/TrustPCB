"""decoupling: capacitor distance from the IC power pin, and vias on the way.

A decoupling capacitor here is a capacitor with one pad on a power net and the other on ground.
Its IC pin is the nearest pad of an active part on the same power net. The distance is pad centre
to pad centre. The track path between the two is followed through vias for the via count.

ESD, TVS and other protection parts, found from their symbol value, are not treated as parts that
need decoupling, and neither are capacitors that are not fitted (do-not-populate). Candidates are
the capacitors over the limit, but the distances of every capacitor measured are kept: each
candidate carries ``class_summary`` (min, max, count and the full sorted list for its pin class)
and ``overlays`` returns the same for every class. A pin class is the family of supply pins the
capacitor serves: all ``DEC*`` pins of a radio are the ``DEC`` class, other supplies are named after
their net.
"""

from __future__ import annotations

import heapq
import math
import re
from typing import Any

from ..pcb.model import Board, Pad
from .base import AnalyzerContext, Emitter, mark, meas
from .classify import is_dnp, is_ground, is_power, is_protection_part, part_kind, parse_value
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


def measure(board: Board, ctx: AnalyzerContext) -> dict[str, Any]:
    """Every capacitor on a supply pin of an active part: {rows, classes, excluded}. Cached on the context."""
    cached = ctx.cache.get("decoupling")
    if cached is not None and cached[0] is board:
        return cached[1]
    topo = get_topology(board, ctx)
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
        pin = min(targets, key=lambda p: (math.dist((p.x, p.y), (cap_pad.x, cap_pad.y)), p.id))
        d = math.dist((pin.x, pin.y), (cap_pad.x, cap_pad.y))
        path = track_path(topo, cap_pad.net, cap_pad.id, pin.id)
        rows.append({"fp": fp, "cap_pad": cap_pad, "pin": pin, "distance": d, "path": path, "vias": path[1] if path else 0, "pin_class": pin_class(cap_pad.net)})
    classes: dict[str, dict[str, Any]] = {}
    for r in sorted(rows, key=lambda r: (r["pin_class"], r["distance"], r["fp"].ref)):
        c = classes.setdefault(r["pin_class"], {"pin_class": r["pin_class"], "capacitors": []})
        c["capacitors"].append(
            {"capacitor": r["fp"].ref, "ic": r["pin"].ref, "ic_pin": r["pin"].number, "net": r["cap_pad"].net, "distance_mm": round(r["distance"], 2), "vias": r["vias"]}
        )
    max_mm = float(ctx.t("decoupling_max_mm"))
    for c in classes.values():
        ds = [x["distance_mm"] for x in c["capacitors"]]
        c["count"] = len(ds)
        c["min_mm"], c["max_mm"] = min(ds), max(ds)
        c["over_limit"] = sum(1 for x in c["capacitors"] if x["distance_mm"] > max_mm)
        c["limit_mm"] = max_mm
    result = {"rows": rows, "classes": classes, "excluded": excluded}
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
        em.add(
            f"{fp.ref} is {d:.1f} mm from {pin.ref} pin {pin.number} ({cap_pad.net})",
            m,
            [fp.id, cap_pad.id, pin.id],
            [
                mark("link", [cap_pad.id, pin.id], label=f"{fp.ref} to {pin.ref}.{pin.number}", value=meas(d, "mm", 2)),
                mark("object", [fp.id], label=fp.ref),
            ],
            "medium" if d > 2 * max_mm or vias > 0 else "low",
            position=((cap_pad.x + pin.x) / 2, (cap_pad.y + pin.y) / 2),
            net=cap_pad.net,
            capacitor=fp.ref,
            capacitance_f=cap_value,
            ic=pin.ref,
            ic_pin=pin.number,
            pin_class=r["pin_class"],
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
    }
