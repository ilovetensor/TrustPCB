"""Small pure-numpy graded mesh helper used by scenario builders.

The worker itself never meshes: it applies the lines found in scenario.json.
This module is only for tools that write scenarios (make_patch_scenario.py and,
later, the server's scenario builder). It needs numpy only.
"""
from __future__ import annotations

import numpy as np


def smooth_lines(fixed, max_res: float, ratio: float = 1.4):
    """Return sorted mesh lines containing every ``fixed`` line.

    Cells never exceed ``max_res``. Cell size grows by at most ``ratio`` per
    cell away from the finest spacing between fixed lines. Each gap between
    fixed lines is marched with the local target size, then stretched slightly
    so the gap is covered by a whole number of cells.
    """
    pts = np.unique(np.round(np.asarray(fixed, dtype=float), 9))
    if len(pts) < 2:
        raise ValueError("need at least two fixed lines")
    gaps = np.diff(pts)
    # finest cell implied at each fixed line: its smaller adjacent gap, capped
    seed = np.minimum(np.r_[gaps[0], gaps], np.r_[gaps, gaps[-1]])
    seed = np.minimum(seed, max_res)

    def target(x: float) -> float:
        return float(min(max_res, np.min(seed + (ratio - 1.0) * np.abs(x - pts))))

    out = [float(pts[0])]
    for a, b in zip(pts[:-1], pts[1:]):
        xs = [a]
        while xs[-1] < b - 1e-9:
            step = target(xs[-1])
            step = min(step, target(xs[-1] + step))  # do not outgrow the far side
            xs.append(xs[-1] + step)
        xs = a + (np.array(xs) - a) * ((b - a) / (xs[-1] - a))  # shrink to fit
        out.extend(float(v) for v in xs[1:])
    return out
