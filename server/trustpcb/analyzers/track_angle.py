"""track_angle: corners between two tracks that are sharper than 90 degrees."""

from __future__ import annotations

from typing import Any

from ..pcb.model import Board
from .base import AnalyzerContext, Emitter, mark, meas, point_target
from .classify import is_ground
from .topology import get_topology
from .track_jitter import deflection_deg

RULE = "track_angle"


def analyze(board: Board, ctx: AnalyzerContext) -> list[dict[str, Any]]:
    topo = get_topology(board, ctx)
    limit = float(ctx.t("angle_max_deg"))
    em = Emitter(RULE)
    for run in topo.runs:
        for node, seg_in, seg_out in run.joints():
            interior = 180.0 - deflection_deg(seg_in, seg_out)
            if interior >= limit:
                continue
            tracks = list(dict.fromkeys([seg_in.parent, seg_out.parent]))
            em.add(
                f"{interior:.0f} degree corner on {run.net or 'unnamed net'}",
                {"angle": meas(interior, "deg", 1), "limit": meas(limit, "deg", 1)},
                tracks,
                [
                    mark("pin", [point_target(node)], label="corner", value=meas(interior, "deg", 1)),
                    mark("object", tracks),
                ],
                "medium" if interior < 45 and not is_ground(run.net) else "low",
                position=node,
                net=run.net,
                layer=run.layer,
            )
    return em.items
