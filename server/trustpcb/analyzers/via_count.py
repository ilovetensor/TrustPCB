"""via_count: layer jumps along a connection on important nets (more than 2 by default).

Important nets are the fast and RF nets plus any the brief names. A connection is a route: tracks
joined through plain vias.
"""

from __future__ import annotations

from typing import Any

from ..pcb.model import Board
from .base import AnalyzerContext, Emitter, mark, meas
from .classify import is_fast_net
from .topology import get_topology

RULE = "via_count"


def analyze(board: Board, ctx: AnalyzerContext) -> list[dict[str, Any]]:
    topo = get_topology(board, ctx)
    limit = int(ctx.t("via_max"))
    important = set(ctx.important_nets)
    em = Emitter(RULE)
    for route in topo.routes:
        if route.net is None or not (route.net in important or is_fast_net(route.net, ctx)):
            continue
        n = len(route.vias)
        if n <= limit:
            continue
        tracks = route.tracks
        mid_via = topo.via_by_id[route.vias[len(route.vias) // 2]]
        em.add(
            f"{n} vias on one connection of {route.net}",
            {"vias": meas(n, "count"), "limit": meas(limit, "count"), "routed_length": meas(route.length, "mm", 2), "layers": meas(len(route.layers), "count")},
            tracks + route.vias,
            [mark("path", tracks, label=f"{n} layer jumps")] + [mark("object", route.vias, label="vias")],
            "medium" if n > limit + 2 else "low",
            position=(mid_via.x, mid_via.y),
            net=route.net,
            layers=route.layers,
        )
    return em.items
