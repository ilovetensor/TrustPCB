"""rf_path: length, width changes, layer jumps and stubs along the RF nets.

RF nets are named for an antenna or RF function (or listed in the brief). Nets joined through a
series part (a two-pad capacitor, inductor, resistor or ferrite whose pads are on two different
signal nets) are one path. A candidate is emitted for a path with any width change, layer jump or
open stub.
"""

from __future__ import annotations

from typing import Any

from ..pcb.model import Board
from .base import AnalyzerContext, Emitter, mark, meas, point_target
from .classify import is_ground, is_power, is_rf_net, part_kind
from .topology import get_topology

RULE = "rf_path"
_SERIES = {"cap", "ind", "res", "ferrite"}


def rf_groups(board: Board, ctx: AnalyzerContext) -> list[dict[str, Any]]:
    """Groups of RF nets: {nets, series (refs), shunt (refs)} in a stable order."""
    seeds = [n for n in board.net_names() if is_rf_net(n, ctx)]
    parent = {n: n for n in seeds}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    series: list[tuple[str, str, str]] = []
    shunt: list[tuple[str, str]] = []
    for f in sorted(board.footprints, key=lambda f: f.ref):
        if part_kind(f) not in _SERIES:
            continue
        pads = board.pads_of(f.ref)
        nets = [p.net for p in pads if p.net]
        if len(pads) != 2 or len(nets) != 2:
            continue
        a, b = nets
        if a == b:
            continue
        if is_ground(b) and is_rf_net(a, ctx):
            shunt.append((f.ref, a))
        elif is_ground(a) and is_rf_net(b, ctx):
            shunt.append((f.ref, b))
        elif is_rf_net(a, ctx) or is_rf_net(b, ctx):
            if not (is_power(a, ctx) or is_power(b, ctx)) and not (is_ground(a) or is_ground(b)):
                series.append((f.ref, a, b))
    for ref, a, b in series:
        for n in (a, b):
            if n not in parent:
                parent[n] = n
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)
    groups: dict[str, dict[str, Any]] = {}
    for n in sorted(parent):
        g = groups.setdefault(find(n), {"nets": [], "series": [], "shunt": []})
        g["nets"].append(n)
    for ref, a, b in series:
        groups[find(a)]["series"].append(ref)
    for ref, n in shunt:
        if n in parent:
            groups[find(n)]["shunt"].append(ref)
    return [groups[k] for k in sorted(groups)]


def analyze(board: Board, ctx: AnalyzerContext) -> list[dict[str, Any]]:
    topo = get_topology(board, ctx)
    em = Emitter(RULE)
    for g in rf_groups(board, ctx):
        nets = set(g["nets"])
        runs = [r for r in topo.runs if r.net in nets]
        if not runs:
            continue
        seg_tracks = sorted({s.parent for r in runs for s in r.segs})
        length = sum(r.length for r in runs)
        widths = sorted({s.width for r in runs for s in r.segs})
        # width changes: a node where tracks of different width meet
        changes = []
        for key, idxs in sorted(topo.nodes.items(), key=lambda kv: (kv[0][0] or "", kv[0][1], kv[0][2], kv[0][3])):
            if key[0] not in nets:
                continue
            w = sorted({topo.segs[i].width for i in idxs})
            if len(w) > 1:
                changes.append(((key[2], key[3]), w, key[1]))
        vias = sorted(v.id for v in board.vias if v.net in nets)
        # stubs: a run with an open end that leaves a junction
        stubs = []
        for r in runs:
            for end, attach, other_attach in (("end", r.end_attach, r.start_attach), ("start", r.start_attach, r.end_attach)):
                pt = r.end if end == "end" else r.start
                if attach.get("pads") or attach.get("vias") or attach.get("zones"):
                    continue
                deg = len(topo.nodes.get((r.net, r.layer, pt[0], pt[1]), []))
                if deg != 1:
                    continue
                other = r.start if end == "end" else r.end
                if len(topo.nodes.get((r.net, r.layer, other[0], other[1]), [])) >= 3 or other_attach.get("pads") or other_attach.get("vias") or other_attach.get("zones"):
                    stubs.append(r)
                    break
        layers = sorted({r.layer for r in runs})
        if not (changes or vias or stubs):
            continue
        m: dict[str, Any] = {
            "path_length": meas(length, "mm", 2),
            "width_changes": meas(len(changes), "count"),
            "layer_jumps": meas(len(vias), "count"),
            "stubs": meas(len(stubs), "count"),
            "series_parts": meas(len(g["series"]), "count"),
            "shunt_parts": meas(len(g["shunt"]), "count"),
        }
        if widths:
            m["min_width"] = meas(widths[0], "mm", 3)
            m["max_width"] = meas(widths[-1], "mm", 3)
        marks = [mark("path", seg_tracks, label=f"RF path {length:.1f} mm")]
        for pt, w, layer in changes[:8]:
            marks.append(mark("pin", [point_target(pt)], label=" to ".join(f"{x:g}" for x in (w[0], w[-1])) + " mm width"))
        for vid in vias[:6]:
            marks.append(mark("pin", [vid], label="layer jump"))
        stub_tracks = sorted({t for r in stubs for t in r.tracks})
        if stub_tracks:
            marks.append(mark("object", stub_tracks, label="stub"))
        details = []
        if g["series"]:
            details.append("series parts: " + ", ".join(g["series"]))
        if g["shunt"]:
            details.append("shunt parts: " + ", ".join(g["shunt"]))
        first = runs[0].vertices()[0]
        em.add(
            f"RF path {' / '.join(g['nets'])}",
            m,
            seg_tracks + vias,
            marks,
            "medium" if vias or stubs else "low",
            position=first,
            details=details,
            nets=g["nets"],
            layers=layers,
        )
    return em.items
