"""track_jitter: direction changes in a short distance (3 or more bends within 5x the track width).

Bends that all turn the same way are one curve drawn in facets (a rounded corner, a meander turn), not
jitter, so a window must contain turns in both directions."""

from __future__ import annotations

import math
from typing import Any

from ..pcb.model import Board
from .base import AnalyzerContext, Emitter, mark, meas, point_target
from .topology import Run, get_topology

RULE = "track_jitter"


def _unit(a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    n = math.hypot(dx, dy)
    return (dx / n, dy / n) if n > 1e-12 else (0.0, 0.0)


def signed_deflection_deg(seg_in, seg_out) -> float:
    """Turn angle at the joint between two oriented segments: 0 is straight on; the sign is the turn side."""
    u = _unit(seg_in.pts[-2], seg_in.pts[-1])
    v = _unit(seg_out.pts[0], seg_out.pts[1])
    dot = max(-1.0, min(1.0, u[0] * v[0] + u[1] * v[1]))
    angle = math.degrees(math.acos(dot))
    return angle if (u[0] * v[1] - u[1] * v[0]) >= 0 else -angle


def deflection_deg(seg_in, seg_out) -> float:
    """Unsigned turn angle: 0 is straight on, 180 a U-turn."""
    return abs(signed_deflection_deg(seg_in, seg_out))


def _bends(run: Run, min_deg: float) -> list[dict[str, Any]]:
    """Bends along a run: position along the run, turn angle, point and the two segment indexes."""
    out = []
    s = 0.0
    for i in range(len(run.segs) - 1):
        s += run.segs[i].length
        signed = signed_deflection_deg(run.segs[i], run.segs[i + 1])
        if abs(signed) >= min_deg:
            out.append({"s": s, "angle": abs(signed), "signed": signed, "point": run.segs[i].pts[-1], "i": i})
    return out


def analyze(board: Board, ctx: AnalyzerContext) -> list[dict[str, Any]]:
    topo = get_topology(board, ctx)
    need = int(ctx.t("jitter_min_bends"))
    mult = float(ctx.t("jitter_window_width_mult"))
    min_deg = float(ctx.t("jitter_min_bend_deg"))
    em = Emitter(RULE)
    for run in topo.runs:
        if len(run.segs) < need + 1:
            continue
        bends = _bends(run, min_deg)
        i = 0
        while i + need <= len(bends):
            width = max(s.width for s in run.segs[bends[i]["i"] : bends[min(i + need - 1, len(bends) - 1)]["i"] + 2])
            limit = mult * width
            j = i
            while j + 1 < len(bends) and bends[j + 1]["s"] - bends[i]["s"] <= limit:
                j += 1
            group = bends[i : j + 1]
            # a wiggle turns both ways; bends that all turn the same way are one smooth curve
            wiggle = any(b["signed"] > 0 for b in group) and any(b["signed"] < 0 for b in group)
            if j - i + 1 >= need and bends[j]["s"] - bends[i]["s"] <= limit and wiggle:
                segs = run.segs[group[0]["i"] : group[-1]["i"] + 2]
                tracks = list(dict.fromkeys(s.parent for s in segs))
                mid = group[len(group) // 2]["point"]
                span = group[-1]["s"] - group[0]["s"]
                em.add(
                    f"{len(group)} bends within {span:.2f} mm on {run.net or 'unnamed net'}",
                    {
                        "bends": meas(len(group), "count"),
                        "span": meas(span, "mm", 3),
                        "track_width": meas(width, "mm", 3),
                        "window_limit": meas(limit, "mm", 3),
                        "total_turning": meas(sum(b["angle"] for b in group), "deg", 1),
                    },
                    tracks,
                    [
                        mark("path", tracks, label=f"{len(group)} bends"),
                        mark("pin", [point_target(mid)], label=f"{len(group)} bends in {span:.2f} mm"),
                    ],
                    "low",
                    position=mid,
                    net=run.net,
                    layer=run.layer,
                )
                i = j + 1
            else:
                i += 1
    return em.items
