"""edge_track: copper close to the board edge, or running along it.

Gap is measured from the copper edge (track centre line minus half the width, via rim) to the
nearest Edge.Cuts line, including cut-outs. A track counts as running along the edge when it stays
within ``edge_parallel_band_mm`` of the edge and within ``edge_parallel_angle_deg`` of its direction.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
from shapely.geometry import LineString, MultiLineString, Point

from ..pcb.model import Board
from .base import AnalyzerContext, Emitter, mark, meas, point_target, shortest_between
from .topology import get_topology

RULE = "edge_track"


def _edge_arrays(board: Board):
    segs = []
    for ring in board.outline:
        n = len(ring)
        for i in range(n):
            segs.append((ring[i][0], ring[i][1], ring[(i + 1) % n][0], ring[(i + 1) % n][1]))
    arr = np.array(segs, dtype=float) if segs else np.zeros((0, 4))
    return arr


def _nearest_edge(arr: np.ndarray, px: float, py: float) -> tuple[float, int]:
    ax, ay, bx, by = arr[:, 0], arr[:, 1], arr[:, 2], arr[:, 3]
    dx, dy = bx - ax, by - ay
    ll = dx * dx + dy * dy
    s = np.clip(np.where(ll > 1e-18, ((px - ax) * dx + (py - ay) * dy) / np.where(ll > 1e-18, ll, 1), 0.0), 0.0, 1.0)
    d = np.hypot(px - (ax + s * dx), py - (ay + s * dy))
    j = int(np.argmin(d))
    return float(d[j]), j


def analyze(board: Board, ctx: AnalyzerContext) -> list[dict[str, Any]]:
    arr = _edge_arrays(board)
    if len(arr) == 0:
        return []
    clearance = float(ctx.t("edge_clearance_mm"))
    par_min = float(ctx.t("edge_parallel_mm"))
    band = float(ctx.t("edge_parallel_band_mm"))
    par_angle = math.radians(float(ctx.t("edge_parallel_angle_deg")))
    boundary = MultiLineString([[(p[0], p[1]) for p in ring] + [(ring[0][0], ring[0][1])] for ring in board.outline])
    topo = get_topology(board, ctx)
    em = Emitter(RULE)

    for run in topo.runs:
        close: list[tuple[float, str, LineString]] = []  # (gap, track id, line)
        best_par = (0.0, [], 1e9)  # length, tracks, min gap within
        cur_len, cur_tracks, cur_gap = 0.0, [], 1e9
        for seg in run.segs:
            line = LineString(seg.pts)
            gap = line.distance(boundary) - seg.width / 2
            if gap < clearance:
                close.append((gap, seg.parent, line))
            parallel_here = False
            if gap < band:
                for p0, p1 in zip(seg.pts, seg.pts[1:]):
                    mx, my = (p0[0] + p1[0]) / 2, (p0[1] + p1[1]) / 2
                    d, j = _nearest_edge(arr, mx, my)
                    if d - seg.width / 2 > band:
                        continue
                    ex, ey = arr[j, 2] - arr[j, 0], arr[j, 3] - arr[j, 1]
                    sx, sy = p1[0] - p0[0], p1[1] - p0[1]
                    cosv = abs(ex * sx + ey * sy) / ((math.hypot(ex, ey) * math.hypot(sx, sy)) or 1.0)
                    if math.acos(min(1.0, cosv)) <= par_angle:
                        parallel_here = True
                        cur_len += math.hypot(sx, sy)
                        cur_gap = min(cur_gap, d - seg.width / 2)
                        if seg.parent not in cur_tracks:
                            cur_tracks.append(seg.parent)
                    else:
                        parallel_here = parallel_here or False
                if not parallel_here and cur_len > 0:
                    pass
            if not parallel_here:
                if cur_len > best_par[0]:
                    best_par = (cur_len, cur_tracks, cur_gap)
                cur_len, cur_tracks, cur_gap = 0.0, [], 1e9
        if cur_len > best_par[0]:
            best_par = (cur_len, cur_tracks, cur_gap)
        long_run = best_par[0] >= par_min
        if not close and not long_run:
            continue
        # the worst point: where the flagged copper is nearest the edge
        flagged = set(c[1] for c in close) | (set(best_par[1]) if long_run else set())
        worst = None
        for seg in run.segs:
            if seg.parent not in flagged:
                continue
            pa, pb, d = shortest_between(LineString(seg.pts), boundary)
            if worst is None or d - seg.width / 2 < worst[0]:
                worst = (d - seg.width / 2, pa, pb, seg.parent)
        min_gap, pa, pb, _ = worst
        tracks = list(dict.fromkeys([c[1] for c in close] + (best_par[1] if long_run else [])))
        m: dict[str, Any] = {"min_gap": meas(max(min_gap, 0.0), "mm", 3), "limit": meas(clearance, "mm", 3)}
        if best_par[0] > 0:
            m["parallel_run"] = meas(best_par[0], "mm", 2)
        what = []
        if close:
            what.append(f"{max(min_gap, 0.0):.2f} mm from the edge")
        if long_run:
            what.append(f"{best_par[0]:.1f} mm run along the edge")
        em.add(
            f"{run.net or 'unnamed net'}: " + ", ".join(what),
            m,
            tracks,
            [
                mark("object", tracks, label="near edge"),
                mark("link", [point_target(pa), point_target(pb)], label="to edge", value=meas(max(min_gap, 0.0), "mm", 2)),
            ],
            "medium" if min_gap < clearance / 2 else "low",
            position=pa,
            net=run.net,
            layer=run.layer,
            kind="clearance" if close else "parallel",
        )

    for via in sorted(board.vias, key=lambda v: v.id):
        pt = Point(via.x, via.y)
        gap = pt.distance(boundary) - via.diameter / 2
        if gap >= clearance:
            continue
        pa, pb, _ = shortest_between(pt, boundary)
        em.add(
            f"Via on {via.net or 'unnamed net'}: {max(gap, 0.0):.2f} mm from the edge",
            {"min_gap": meas(max(gap, 0.0), "mm", 3), "limit": meas(clearance, "mm", 3)},
            [via.id],
            [
                mark("object", [via.id], label="near edge"),
                mark("link", [point_target((via.x, via.y)), point_target(pb)], label="to edge", value=meas(max(gap, 0.0), "mm", 2)),
            ],
            "medium" if gap < clearance / 2 else "low",
            position=(via.x, via.y),
            net=via.net,
            kind="via",
        )
    return em.items
