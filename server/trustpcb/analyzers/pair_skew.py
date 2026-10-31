"""pair_skew: routed-length mismatch inside differential pairs found by net name.

Pairs are nets whose names differ only in the polarity suffix: ``_DP``/``_DM``, ``D+``/``D-`` or ``_P``/``_N``
(anything before the suffix, such as ``/USB_``, must match). Both nets must carry tracks. The routed length of
a net is the sum of its track lengths on every layer; vias add no length here and are counted separately.
The mismatch in picoseconds is the length difference times ``skew_ps_per_mm`` (about 6 ps/mm for outer-layer
microstrip on FR4; the board's own stackup is not used, so it is an estimate). A pair is a candidate when the
mismatch is over ``skew_min_mm``. Every pair measured is listed in ``overlays``.
"""

from __future__ import annotations

import re
from typing import Any

from ..pcb.model import Board
from .base import AnalyzerContext, Emitter, mark, meas, point_target

RULE = "pair_skew"

#: (positive suffix, negative suffix) tried on the upper-cased net name
_SUFFIXES = (("_DP", "_DM"), ("DP", "DM"), ("D+", "D-"), ("_P", "_N"))
_BOUNDARY = re.compile(r"[^A-Z0-9]$")


def find_pairs(names: list[str]) -> list[tuple[str, str, str]]:
    """(stem, positive net, negative net) for every pair of nets named for one differential pair."""
    by_upper = {n.upper(): n for n in names}
    out: list[tuple[str, str, str]] = []
    used: set[str] = set()
    for plus, minus in _SUFFIXES:
        for up, name in sorted(by_upper.items()):
            if not up.endswith(plus) or name in used:
                continue
            stem = up[: -len(plus)]
            # a bare "DP" or "DM" needs a boundary before it: "ADP" is not a data line
            if plus == "DP" and stem and not _BOUNDARY.search(stem):
                continue
            other = by_upper.get(stem + minus)
            if other and other not in used and other != name:
                used.update((name, other))
                out.append((name[: len(name) - len(plus)], name, other))
    return out


def measure(board: Board, ctx: AnalyzerContext) -> list[dict[str, Any]]:
    length: dict[str, float] = {}
    tracks: dict[str, list] = {}
    for t in board.tracks:
        if t.net:
            length[t.net] = length.get(t.net, 0.0) + float(t.length)
            tracks.setdefault(t.net, []).append(t)
    vias: dict[str, int] = {}
    for v in board.vias:
        if v.net:
            vias[v.net] = vias.get(v.net, 0) + 1
    ps_per_mm = float(ctx.t("skew_ps_per_mm"))
    rows = []
    for stem, pos, neg in find_pairs([n for n in board.net_names() if n in length]):
        diff = abs(length[pos] - length[neg])
        longer, shorter = (pos, neg) if length[pos] >= length[neg] else (neg, pos)
        rows.append({"stem": stem, "pos": pos, "neg": neg, "lengths": {pos: length[pos], neg: length[neg]}, "mismatch_mm": diff, "skew_ps": diff * ps_per_mm,
                     "longer": longer, "shorter": shorter, "vias": {pos: vias.get(pos, 0), neg: vias.get(neg, 0)}, "tracks": {pos: [t.id for t in tracks[pos]], neg: [t.id for t in tracks[neg]]}})
    return rows


def analyze(board: Board, ctx: AnalyzerContext) -> list[dict[str, Any]]:
    limit = float(ctx.t("skew_min_mm"))
    ps_per_mm = float(ctx.t("skew_ps_per_mm"))
    em = Emitter(RULE)
    for r in measure(board, ctx):
        if r["mismatch_mm"] <= limit:
            continue
        pos, neg = r["pos"], r["neg"]
        ids = r["tracks"][pos] + r["tracks"][neg]
        t0 = next(t for t in board.tracks if t.id == r["tracks"][r["longer"]][0])
        em.add(
            f"{pos} and {neg} differ by {r['mismatch_mm']:.2f} mm ({r['skew_ps']:.0f} ps) in routed length",
            {
                "mismatch": meas(r["mismatch_mm"], "mm", 2),
                "skew_estimate": meas(r["skew_ps"], "ps", 1),
                "length_positive": meas(r["lengths"][pos], "mm", 2),
                "length_negative": meas(r["lengths"][neg], "mm", 2),
                "vias_positive": meas(r["vias"][pos], "count"),
                "vias_negative": meas(r["vias"][neg], "count"),
                "limit": meas(limit, "mm", 2),
                "delay_per_mm": meas(ps_per_mm, "ps/mm", 1),
            },
            ids,
            [mark("object", r["tracks"][r["longer"]], label=f"{r['longer']} is longer"), mark("pin", [point_target(t0.start)], label=f"{r['mismatch_mm']:.2f} mm / {r['skew_ps']:.0f} ps")],
            "medium" if r["skew_ps"] >= float(ctx.t("skew_medium_ps")) else "low",
            position=t0.start,
            pair=[pos, neg],
            longer=r["longer"],
            details=[f"Routed length is the sum of track lengths on every layer; {ps_per_mm:g} ps/mm is a typical outer-layer FR4 figure, not taken from this stackup."],
        )
    return em.items


def overlays(board: Board, ctx: AnalyzerContext) -> dict[str, Any]:
    rows = measure(board, ctx)
    if not rows:
        return {}
    return {
        "label": "Routed-length mismatch of every differential pair found by net name (not only those over the limit)",
        "pairs": [{"pair": [r["pos"], r["neg"]], "mismatch_mm": round(r["mismatch_mm"], 2), "skew_ps": round(r["skew_ps"], 1), "lengths_mm": {k: round(v, 2) for k, v in r["lengths"].items()}} for r in rows],
    }
