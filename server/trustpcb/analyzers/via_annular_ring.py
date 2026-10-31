"""via_annular_ring: vias whose annular ring is below a manufacturability threshold.

The annular ring is the copper left around the drill: ``(pad diameter - drill) / 2``. Vias are grouped
by pad and drill size, and one candidate is emitted per group below ``via_min_annular_mm``.

The threshold is a setting, not a law, and it is stated in every candidate with its source:

* default ``via_min_annular_mm`` = 0.127 mm (5 mil), the annular ring OSH Park lists for its two-layer
  service (https://docs.oshpark.com/services, read 2026-10-31). JLCPCB's help centre quotes 0.13 mm for its
  minimum annular ring; that figure was read in a customer question that cites the capabilities page,
  not from the capabilities page itself. Fabs differ: set the threshold to the chosen fab's number;
* the project's own minimum (``min_via_annular_width`` in the ``.kicad_pro`` rules) is reported beside it. KiCad's
  DRC checks only that setting, so a via can pass DRC and still sit under what a fab quotes.

Nothing here judges the design; it measures a ring against a stated number.
"""

from __future__ import annotations

from typing import Any

from ..pcb.model import Board
from .base import AnalyzerContext, Emitter, mark, meas
from .project import design_rules

RULE = "via_annular_ring"
THRESHOLD_SOURCE = "OSH Park two-layer service minimum annular ring, 5 mil (docs.oshpark.com/services); a typical fab figure, not your fab's. Set via_min_annular_mm to your fab's number."


def groups(board: Board) -> list[dict[str, Any]]:
    """Vias grouped by (pad diameter, drill): {diameter, drill, ring, count, ids}, smallest ring first."""
    by: dict[tuple[float, float], list[str]] = {}
    for v in sorted(board.vias, key=lambda v: v.id):
        by.setdefault((round(v.diameter, 4), round(v.drill, 4)), []).append(v.id)
    out = [{"diameter": d, "drill": dr, "ring": round((d - dr) / 2, 4), "count": len(ids), "ids": ids} for (d, dr), ids in by.items()]
    out.sort(key=lambda g: (g["ring"], g["diameter"], g["drill"]))
    return out


def analyze(board: Board, ctx: AnalyzerContext) -> list[dict[str, Any]]:
    limit = float(ctx.t("via_min_annular_mm"))
    rules = design_rules(board, ctx)
    project_min = rules.get("min_via_annular_width")
    allg = groups(board)
    under = [g for g in allg if g["ring"] < limit - 1e-9]
    total = len(board.vias)
    flagged = sum(g["count"] for g in under)
    em = Emitter(RULE)
    for g in under:
        m: dict[str, Any] = {
            "annular_ring": meas(g["ring"], "mm", 4),
            "threshold": meas(limit, "mm", 4),
            "shortfall": meas(limit - g["ring"], "mm", 4),
            "pad_diameter": meas(g["diameter"], "mm", 3),
            "drill": meas(g["drill"], "mm", 3),
            "vias": meas(g["count"], "count"),
            "vias_total": meas(total, "count"),
        }
        if isinstance(project_min, (int, float)):
            m["project_min_annular"] = meas(float(project_min), "mm", 4)
        ids = g["ids"]
        first = board._via[ids[0]]
        em.add(
            f"{g['count']} of {total} vias have a {g['diameter']:g} mm pad on a {g['drill']:g} mm drill: annular ring {g['ring']:.3f} mm, under {limit:g} mm",
            m,
            ids,
            [mark("object", ids, label=f"ring {g['ring']:.3f} mm")],
            "medium",
            position=(first.x, first.y),
            details=[
                f"Annular ring = (pad {g['diameter']:g} - drill {g['drill']:g}) / 2 = {g['ring']:.4f} mm.",
                f"Threshold {limit:g} mm: {THRESHOLD_SOURCE}",
                (f"The project's own minimum is {float(project_min):g} mm, so KiCad's design rule check does not report these vias." if isinstance(project_min, (int, float)) and project_min <= g["ring"] else "")
                or "",
            ],
            threshold_source=THRESHOLD_SOURCE,
            pad_diameter_mm=g["diameter"],
            drill_mm=g["drill"],
            via_count=g["count"],
            rule_summary={"vias_total": total, "vias_below_threshold": flagged, "groups_below_threshold": len(under), "threshold_mm": limit},
        )
    for c in em.items:
        c["details"] = [d for d in c["details"] if d]
    return em.items


def overlays(board: Board, ctx: AnalyzerContext) -> dict[str, Any]:
    limit = float(ctx.t("via_min_annular_mm"))
    allg = groups(board)
    if not allg:
        return {}
    return {
        "label": "Via annular ring by pad and drill size",
        "threshold_mm": limit,
        "threshold_source": THRESHOLD_SOURCE,
        "project_min_annular_mm": design_rules(board, ctx).get("min_via_annular_width"),
        "vias_total": len(board.vias),
        "groups": [{k: g[k] for k in ("diameter", "drill", "ring", "count")} | {"below_threshold": g["ring"] < limit - 1e-9} for g in allg],
    }
