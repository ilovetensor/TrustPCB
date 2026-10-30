"""return_path: fast signals that cross a gap in the ground copper beneath them.

Each routed run on a fast net is sampled along its length. A sample is supported when ground
copper (a filled ground zone) lies on an adjacent copper layer at that point. A stretch of
unsupported samples is a gap crossing. Samples inside an antenna keep-out, or on the detected radiating
element, are never gaps: an antenna is meant to be free of ground (see ``radiating``). Ground zones that have no fill data are approximated by
their outline and the candidate says so.
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


def adjacent_layers(board: Board, layer: str) -> list[str]:
    copper = board.copper_layers()
    if layer not in copper:
        return []
    i = copper.index(layer)
    return [copper[j] for j in (i - 1, i + 1) if 0 <= j < len(copper)]


def sample_run(run: Run, step: float) -> list[tuple[float, float, float, str]]:
    """Points every ``step`` mm along a run: (x, y, distance along, track id)."""
    out: list[tuple[float, float, float, str]] = []
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
                out.append((a[0] + f * (b[0] - a[0]), a[1] + f * (b[1] - a[1]), s0 + f * length, seg.parent))
            s0 += length
    return out


def analyze(board: Board, ctx: AnalyzerContext) -> list[dict[str, Any]]:
    topo = get_topology(board, ctx)
    step = float(ctx.t("return_path_step_mm"))
    min_gap = float(ctx.t("return_path_min_gap_mm"))
    merge = float(ctx.t("return_path_merge_mm"))
    gnd, approx = ground_copper(board)
    fast = set(fast_nets(board, ctx))
    rad = radiating(board, ctx)
    em = Emitter(RULE)
    for run in topo.runs:
        if run.net not in fast:
            continue
        adj = adjacent_layers(board, run.layer)
        if not adj:
            continue
        samples = sample_run(run, step)
        if not samples:
            continue
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
                tracks = list(dict.fromkeys(samples[k][3] for k in idx))
                mx, my = samples[(i + j) // 2][0], samples[(i + j) // 2][1]
                no_ref = all(gnd.get(layer) is None for layer in adj)
                refs = ", ".join(adj)
                em.add(
                    f"{run.net} crosses a {length:.1f} mm gap in ground on {refs}",
                    {"gap_length": meas(length, "mm", 2), "sample_step": meas(step, "mm", 3), "reference_layers": meas(len(adj), "count")},
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
                    reference_from_outline=any(layer in approx for layer in adj),
                )
            i = j + 1
    return em.items
