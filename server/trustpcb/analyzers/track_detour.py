"""track_detour: routed length of a connection against the straight line between its ends."""

from __future__ import annotations

import math
from typing import Any

from ..pcb.model import Board
from .base import AnalyzerContext, Emitter, mark, meas, point_target
from .classify import is_ground
from .topology import get_topology

RULE = "track_detour"


def analyze(board: Board, ctx: AnalyzerContext) -> list[dict[str, Any]]:
    topo = get_topology(board, ctx)
    ratio_min = float(ctx.t("detour_ratio"))
    extra_min = float(ctx.t("detour_extra_mm"))
    em = Emitter(RULE)
    for route in topo.routes:
        if route.net is None or is_ground(route.net):
            continue
        length = route.length
        direct = math.dist(route.start, route.end)
        extra = length - direct
        ratio = length / max(direct, 0.05)
        if ratio <= ratio_min or extra <= extra_min:
            continue
        tracks = route.tracks
        a, b = point_target(route.start), point_target(route.end)
        mid = route.runs[len(route.runs) // 2].vertices()
        em.add(
            f"{route.net} routed {length:.1f} mm for a {direct:.1f} mm straight line",
            {
                "routed_length": meas(length, "mm", 2),
                "direct_length": meas(direct, "mm", 2),
                "extra_length": meas(extra, "mm", 2),
                "ratio": meas(ratio, "ratio", 2),
                "vias": meas(len(route.vias), "count"),
            },
            tracks,
            [
                mark("path", tracks, label=f"{length:.1f} mm routed"),
                mark("sketch", [a, b]),
                mark("link", [a, b], label=f"straight {direct:.1f} mm", value=meas(length, "mm routed", 1)),
            ],
            "medium" if ratio > 3 else "low",
            position=mid[len(mid) // 2],
            net=route.net,
            layers=route.layers,
        )
    return em.items
