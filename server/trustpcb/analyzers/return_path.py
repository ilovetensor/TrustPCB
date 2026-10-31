"""return_path: fast signals that cross a gap in the ground copper beneath them.

Each routed run on a fast net is sampled along its length. A sample is supported when ground
copper (a filled ground zone) lies on an adjacent copper layer at that point. A stretch of
unsupported samples is a gap crossing (an event). Samples inside an antenna keep-out, or on the detected radiating
element, are never gaps: an antenna is meant to be free of ground (see ``radiating``).

Lengths are reported three ways and must not be mixed up:

* ``gap_length`` is the merged span of the event, first unsupported sample to last, plus one step.
  Short supported stretches (up to ``return_path_merge_mm``) between two unsupported stretches do not
  separate them, so the span includes them.
* ``unsupported_length`` is the length of the unsupported samples alone, each weighted by the
  stretch of track it stands for. ``bridged_length`` is what is left of the span: supported copper that
  the merge rule absorbed.
* the per-net summary (``net_summary`` on a candidate, ``overlays``) counts events, sums both lengths and
  gives the routed length of the net, so "41 gap events on 9 nets" is read directly, not counted by eye.

Ground zones that have no fill data are approximated by their outline. That treats the whole outline as
copper, so it can hide gaps and understate unsupported length; it never inflates a gap. The candidate
says so (``reference_from_outline``, ``reference_note``).
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import shapely
from shapely.geometry import Polygon
from shapely.ops import unary_union

from ..pcb.model import Board
from .base import AnalyzerContext, Emitter, mark, meas, point_target
from .classify import fast_nets, is_ground
from .radiating import radiating
from .topology import Run, get_topology

RULE = "return_path"


def ground_copper(board: Board) -> tuple[dict[str, Any], set[str]]:
    """Union of ground zone copper per layer, and the layers that relied on unfilled outlines."""
    parts: dict[str, list[Polygon]] = {}
    approx: set[str] = set()
    for z in sorted(board.zones, key=lambda z: z.id):
        if z.is_keepout or not is_ground(z.net):
            continue
        if z.filled:
            for layer, rings in z.filled.items():
                for r in rings:
                    if len(r) >= 3:
                        p = Polygon(r).buffer(0)
                        if not p.is_empty:
                            parts.setdefault(layer, []).append(p)
        elif len(z.outline) >= 3:
            p = Polygon(z.outline).buffer(0)
            for layer in z.layers:
                parts.setdefault(layer, []).append(p)
                approx.add(layer)
    out = {layer: unary_union(polys) for layer, polys in parts.items()}
    for g in out.values():
        shapely.prepare(g)
    return out, approx


def ground_for(board: Board, ctx: AnalyzerContext | None) -> tuple[dict[str, Any], set[str]]:
    """``ground_copper`` cached on the context (it unions every ground polygon, so it is built once per board)."""
    if ctx is None:
        return ground_copper(board)
    cached = ctx.cache.get("ground_copper")
    if cached is None or cached[0] is not board:
        cached = (board, ground_copper(board))
        ctx.cache["ground_copper"] = cached
    return cached[1]


def adjacent_layers(board: Board, layer: str) -> list[str]:
    copper = board.copper_layers()
    if layer not in copper:
        return []
    i = copper.index(layer)
    return [copper[j] for j in (i - 1, i + 1) if 0 <= j < len(copper)]


def sample_run(run: Run, step: float) -> list[tuple[float, float, float, str, float]]:
    """Points every ``step`` mm along a run: (x, y, distance along, track id, length of track the sample stands for)."""
    out: list[tuple[float, float, float, str, float]] = []
    s0 = 0.0
    for seg in run.segs:
        pts = seg.pts
        for a, b in zip(pts, pts[1:]):
            length = math.dist(a, b)
            if length < 1e-9:
                continue
            n = max(1, int(math.ceil(length / step)))
            for k in range(n):
                f = (k + 0.5) / n
                out.append((a[0] + f * (b[0] - a[0]), a[1] + f * (b[1] - a[1]), s0 + f * length, seg.parent, length / n))
            s0 += length
    return out


OUTLINE_NOTE = (
    "The ground zone on {layers} has no fill data, so its outline was used as the reference. That counts everything inside the outline as "
    "copper: it can hide gaps and understate unsupported length, and never inflates a gap."
)


def analyze(board: Board, ctx: AnalyzerContext) -> list[dict[str, Any]]:
    topo = get_topology(board, ctx)
    step = float(ctx.t("return_path_step_mm"))
    min_gap = float(ctx.t("return_path_min_gap_mm"))
    merge = float(ctx.t("return_path_merge_mm"))
    gnd, approx = ground_for(board, ctx)
    fast = set(fast_nets(board, ctx))
    rad = radiating(board, ctx)
    em = Emitter(RULE)
    net_stats: dict[str, dict[str, Any]] = {}
    for run in topo.runs:
        if run.net not in fast:
            continue
        adj = adjacent_layers(board, run.layer)
        if not adj:
            continue
        samples = sample_run(run, step)
        if not samples:
            continue
        st = net_stats.setdefault(run.net, {"net": run.net, "gap_events": 0, "merged_span_mm": 0.0, "unsupported_mm": 0.0, "bridged_mm": 0.0, "longest_gap_mm": 0.0, "routed_length_mm": 0.0, "layers": set(), "reference_layers": set()})
        st["routed_length_mm"] += sum(x[4] for x in samples)
        st["layers"].add(run.layer)
        st["reference_layers"].update(adj)
        xs = np.array([s[0] for s in samples])
        ys = np.array([s[1] for s in samples])
        supported = np.zeros(len(samples), dtype=bool)
        for layer in adj:
            g = gnd.get(layer)
            if g is not None:
                supported |= shapely.contains_xy(g, xs, ys)
        if rad:
            # A printed antenna must stay clear of ground: its keep-out and the radiating element are not judged.
            supported |= rad.contains_xy(xs, ys, run.layer)
            supported |= np.array([s[3] in rad.track_ids for s in samples], dtype=bool)
        i = 0
        n = len(samples)
        while i < n:
            if supported[i]:
                i += 1
                continue
            j = i
            while j + 1 < n and not supported[j + 1]:
                j += 1
            # a short supported stretch between two gaps does not separate them
            while True:
                k = j + 1
                while k < n and supported[k]:
                    k += 1
                if k < n and samples[k][2] - samples[j][2] <= merge:
                    j = k
                    while j + 1 < n and not supported[j + 1]:
                        j += 1
                else:
                    break
            length = (samples[j][2] - samples[i][2]) + step
            if length >= min_gap:
                idx = range(i, j + 1)
                unsupported = sum(samples[k][4] for k in idx if not supported[k])
                bridged = sum(samples[k][4] for k in idx if supported[k])
                tracks = list(dict.fromkeys(samples[k][3] for k in idx))
                mx, my = samples[(i + j) // 2][0], samples[(i + j) // 2][1]
                no_ref = all(gnd.get(layer) is None for layer in adj)
                refs = ", ".join(adj)
                from_outline = any(layer in approx for layer in adj)
                st["gap_events"] += 1
                st["merged_span_mm"] += length
                st["unsupported_mm"] += unsupported
                st["bridged_mm"] += bridged
                st["longest_gap_mm"] = max(st["longest_gap_mm"], length)
                extra: dict[str, Any] = {}
                if from_outline:
                    extra["reference_note"] = OUTLINE_NOTE.format(layers=", ".join(layer for layer in adj if layer in approx))
                em.add(
                    f"{run.net} crosses a {length:.1f} mm gap in ground on {refs}",
                    {
                        "gap_length": meas(length, "mm", 2),
                        "unsupported_length": meas(unsupported, "mm", 2),
                        "bridged_length": meas(bridged, "mm", 2),
                        "merge_rule": meas(merge, "mm", 2),
                        "sample_step": meas(step, "mm", 3),
                        "reference_layers": meas(len(adj), "count"),
                    },
                    tracks,
                    [
                        mark("path", tracks, label=f"{length:.1f} mm over gap"),
                        mark("pin", [point_target((mx, my))], label="gap crossing"),
                    ],
                    "high" if no_ref or length > 5 else "medium",
                    position=(mx, my),
                    net=run.net,
                    layer=run.layer,
                    reference_layers=adj,
                    no_ground_on_reference=no_ref,
                    reference_from_outline=from_outline,
                    **extra,
                )
            i = j + 1
    summary = _summaries(em.items, net_stats, merge, min_gap, step, approx)
    for cand in em.items:
        cand["net_summary"] = summary["per_net"][cand["net"]]
        cand["rule_summary"] = summary["rule"]
    ctx.cache["return_path_summary"] = (board, summary)
    return em.items


def _summaries(events: list[dict[str, Any]], net_stats: dict[str, dict[str, Any]], merge: float, min_gap: float, step: float, approx: set[str]) -> dict[str, Any]:
    per_net: dict[str, dict[str, Any]] = {}
    for net in sorted(n for n, st in net_stats.items() if st["gap_events"]):
        st = net_stats[net]
        per_net[net] = {
            "net": net,
            "gap_events": st["gap_events"],
            "merged_span_mm": round(st["merged_span_mm"], 2),
            "unsupported_mm": round(st["unsupported_mm"], 2),
            "bridged_mm": round(st["bridged_mm"], 2),
            "longest_gap_mm": round(st["longest_gap_mm"], 2),
            "routed_length_mm": round(st["routed_length_mm"], 2),
            "layers": sorted(st["layers"]),
            "reference_layers": sorted(st["reference_layers"]),
        }
    rule: dict[str, Any] = {
        "gap_events": len(events),
        "nets_with_gaps": len(per_net),
        "nets": list(per_net),
        "merged_span_mm": round(sum(p["merged_span_mm"] for p in per_net.values()), 2),
        "unsupported_mm": round(sum(p["unsupported_mm"] for p in per_net.values()), 2),
        "merge_rule_mm": merge,
        "min_gap_mm": min_gap,
        "sample_step_mm": step,
        "meaning": "gap_events counts crossings, not signals or tracks: one net can cross several gaps. unsupported_mm is the real length without ground; merged_span_mm also counts supported stretches of up to merge_rule_mm that the merge rule absorbed.",
    }
    outline_layers = sorted(approx)
    if outline_layers:
        rule["reference_from_outline_layers"] = outline_layers
        rule["reference_note"] = OUTLINE_NOTE.format(layers=", ".join(outline_layers))
    return {"rule": rule, "per_net": per_net}


def overlays(board: Board, ctx: AnalyzerContext) -> dict[str, Any]:
    cached = ctx.cache.get("return_path_summary")
    if cached is None or cached[0] is not board:
        analyze(board, ctx)
        cached = ctx.cache["return_path_summary"]
    summary = cached[1]
    if not summary["rule"]["gap_events"]:
        return {}
    return {
        "label": "Fast nets crossing gaps in the ground on the adjacent layer (counts are gap events, not signals)",
        **summary["rule"],
        "per_net": list(summary["per_net"].values()),
    }
