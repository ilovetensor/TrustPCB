"""rf_path: length, width changes, layer jumps and stubs along the RF nets.

RF nets are named for an antenna or RF function (or listed in the brief). Nets joined through a
series part (a two-pad capacitor, inductor, resistor or ferrite whose pads are on two different
signal nets) are one path. A candidate is emitted for a path with any width change, layer jump or
stub.

The path length is split into the feed and the radiating element. A trace antenna's feed point
(``intake.detect``) is followed along the antenna net while the track width stays the feed's width; the
tracks from the first width change on are the radiating element. Everything else on the path is feed.
``feed_length`` + ``radiating_length`` = ``path_length``, and ``net_breakdown`` gives the split per net with the
lengths by width, so a net that mixes feed and element (an antenna net with a 0.3 mm feed and a 1.0 mm
meander) is not read as one long antenna.

Stubs are open-ended track branches and the pads of parts that are not fitted (do-not-populate): a
shunt part left off a line leaves its pad hanging on the line as an open stub. ``stubs`` counts both;
``open_end_stubs`` and ``dnp_stubs`` give the parts.
"""

from __future__ import annotations

import math
from typing import Any

from ..intake.detect import detect
from ..pcb.model import Board
from .base import AnalyzerContext, Emitter, mark, meas, point_target
from .classify import is_dnp, is_ground, is_power, is_rf_net, part_kind
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


def _key(p) -> tuple[float, float]:
    return (round(p[0], 3), round(p[1], 3))


def feed_split(board: Board, ctx: AnalyzerContext) -> dict[str, Any]:
    """Which tracks of a trace antenna are feed and which are the radiating element.

    ``{net, radiating: set of track ids, feed: set of track ids, feed_width, basis}``. Empty sets when the
    board has no trace antenna with a known feed point, or when the antenna net has one width throughout (the
    feed cannot be told from the element then, and ``basis`` says so).
    """
    cached = ctx.cache.get("rf_feed_split")
    if cached is not None and cached[0] is board:
        return cached[1]
    out: dict[str, Any] = {"net": None, "radiating": set(), "feed": set(), "feed_width": None, "basis": "no trace antenna with a known feed point was detected"}
    try:
        ant = detect(board).antenna or {}
    except Exception:  # detection is best effort
        ant = {}
    feed_pt = ant.get("feed_point")
    ids = set(ant.get("track_ids") or [])
    tracks = {t.id: t for t in board.tracks if t.id in ids}
    if ant.get("type") == "trace" and feed_pt and tracks:
        out["net"] = ant.get("net")
        ends = {tid: (_key(t.start), _key(t.end)) for tid, t in tracks.items()}
        seeds = sorted(tracks, key=lambda tid: min(math.dist(tracks[tid].start, feed_pt), math.dist(tracks[tid].end, feed_pt)))
        near = [tid for tid in seeds if min(math.dist(tracks[tid].start, feed_pt), math.dist(tracks[tid].end, feed_pt)) <= 0.6]
        if near:
            width = round(tracks[near[0]].width, 4)
            feed = {tid for tid in near if round(tracks[tid].width, 4) == width}
            todo = list(feed)
            while todo:
                cur = todo.pop()
                for tid in tracks:
                    if tid in feed or round(tracks[tid].width, 4) != width:
                        continue
                    if set(ends[cur]) & set(ends[tid]):
                        feed.add(tid)
                        todo.append(tid)
            rest = set(tracks) - feed
            out["feed_width"] = width
            if rest:
                out["feed"], out["radiating"] = feed, rest
                out["basis"] = f"tracks from the feed point {list(feed_pt)} while the width stays {width:g} mm are feed; the tracks after the width change are the radiating element"
            else:
                out["basis"] = f"the antenna net is {width:g} mm wide throughout, so the feed cannot be told from the radiating element"
        else:
            out["basis"] = "no antenna track starts at the feed point"
    ctx.cache["rf_feed_split"] = (board, out)
    return out


def analyze(board: Board, ctx: AnalyzerContext) -> list[dict[str, Any]]:
    topo = get_topology(board, ctx)
    split = feed_split(board, ctx)
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
        # pads of parts that are not fitted hang on the line as open stubs
        dnp_stubs = []
        for f in sorted(board.footprints, key=lambda f: f.ref):
            if not is_dnp(f) or f.ref not in set(g["shunt"]) | set(g["series"]):
                continue
            for pad in board.pads_of(f.ref):
                if pad.net in nets:
                    dnp_stubs.append({"ref": f.ref, "pad": pad.id, "net": pad.net, "value": f.value, "role": "series" if f.ref in g["series"] else "shunt"})
        # feed and radiating length, per net
        breakdown = []
        rad_ids = split["radiating"]
        for net in g["nets"]:
            net_runs = [r for r in runs if r.net == net]
            if not net_runs:
                continue
            by_width: dict[float, float] = {}
            feed_len = rad_len = 0.0
            for r in net_runs:
                for sg in r.segs:
                    by_width[round(sg.width, 4)] = by_width.get(round(sg.width, 4), 0.0) + sg.length
                    if sg.parent in rad_ids:
                        rad_len += sg.length
                    else:
                        feed_len += sg.length
            breakdown.append(
                {
                    "net": net,
                    "length_mm": round(feed_len + rad_len, 2),
                    "feed_mm": round(feed_len, 2),
                    "radiating_mm": round(rad_len, 2),
                    "by_width": [{"width_mm": w, "length_mm": round(by_width[w], 2)} for w in sorted(by_width)],
                }
            )
        feed_total = sum(b["feed_mm"] for b in breakdown)
        rad_total = sum(b["radiating_mm"] for b in breakdown)
        stub_count = len(stubs) + len(dnp_stubs)
        if not (changes or vias or stub_count):
            continue
        m: dict[str, Any] = {
            "path_length": meas(length, "mm", 2),
            "feed_length": meas(feed_total, "mm", 2),
            "radiating_length": meas(rad_total, "mm", 2),
            "width_changes": meas(len(changes), "count"),
            "layer_jumps": meas(len(vias), "count"),
            "stubs": meas(stub_count, "count"),
            "open_end_stubs": meas(len(stubs), "count"),
            "dnp_stubs": meas(len(dnp_stubs), "count"),
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
        if dnp_stubs:
            marks.append(mark("object", [d["pad"] for d in dnp_stubs], label="pad of a part that is not fitted (open stub)"))
        details = []
        if g["series"]:
            details.append("series parts: " + ", ".join(g["series"]))
        if g["shunt"]:
            details.append("shunt parts: " + ", ".join(g["shunt"]))
        if dnp_stubs:
            details.append("not fitted, pads are open stubs: " + ", ".join(sorted({d["ref"] for d in dnp_stubs})))
        if rad_total:
            details.append(f"feed {feed_total:.2f} mm, radiating element {rad_total:.2f} mm ({split['basis']})")
        first = runs[0].vertices()[0]
        em.add(
            f"RF path {' / '.join(g['nets'])}",
            m,
            seg_tracks + vias,
            marks,
            "medium" if vias or stub_count else "low",
            position=first,
            details=details,
            nets=g["nets"],
            layers=layers,
            net_breakdown=breakdown,
            feed_radiating_basis=split["basis"],
            dnp_stub_pads=dnp_stubs,
        )
    return em.items
