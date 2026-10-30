"""noise_proximity: distance from switching or RF parts to sensitive parts, below 10 mm."""

from __future__ import annotations

from typing import Any

from shapely.geometry import Polygon

from ..pcb.model import Board
from .base import AnalyzerContext, Emitter, mark, meas, shortest_between
from .classify import NOISE_ROLES, SENSITIVE_ROLES, classify_parts, footprint_ring, part_kind

RULE = "noise_proximity"
_PASSIVE = {"cap", "res", "ferrite", "diode", "led", "switch", "fuse", "testpoint", "fiducial", "mounting", "connector"}


def analyze(board: Board, ctx: AnalyzerContext) -> list[dict[str, Any]]:
    limit = float(ctx.t("noise_distance_mm"))
    per_source = int(ctx.t("noise_max_per_source"))
    roles = classify_parts(board, ctx)
    ctx.cache["part_roles"] = roles
    given = {k for k in (ctx.part_roles or {})}
    polys = {f.ref: Polygon(footprint_ring(f)).buffer(0) for f in board.footprints}
    kinds = {f.ref: part_kind(f) for f in board.footprints}
    # Heuristic roles only mark active parts as sources; a brief's roles are taken as given.
    sources = [f for f in sorted(board.footprints, key=lambda f: f.ref) if roles.get(f.ref) in NOISE_ROLES and (f.ref in given or kinds[f.ref] not in _PASSIVE)]
    sensitive = [f for f in sorted(board.footprints, key=lambda f: f.ref) if roles.get(f.ref) in SENSITIVE_ROLES and (f.ref in given or kinds[f.ref] not in _PASSIVE | {"ind"})]
    em = Emitter(RULE)
    seen: set[frozenset[str]] = set()
    for s in sources:
        pairs = []
        for t in sensitive:
            if t.ref == s.ref or frozenset((s.ref, t.ref)) in seen:
                continue
            d = polys[s.ref].distance(polys[t.ref])
            if d < limit:
                pairs.append((d, t))
        pairs.sort(key=lambda p: (p[0], p[1].ref))
        for d, t in pairs[:per_source]:
            seen.add(frozenset((s.ref, t.ref)))
            pa, pb, _ = shortest_between(polys[s.ref], polys[t.ref])
            marks = [
                mark("link", [s.id, t.id], label=f"{s.ref} to {t.ref}", value=meas(d, "mm", 2)),
                mark("object", [s.id, t.id], label=f"{roles[s.ref]} / {roles[t.ref]}"),
            ]
            em.add(
                f"{s.ref} ({roles[s.ref]}) to {t.ref} ({roles[t.ref]})",
                {"distance": meas(d, "mm", 2), "limit": meas(limit, "mm", 2)},
                [s.id, t.id],
                marks,
                "medium" if d < limit / 3 else "low",
                position=((pa[0] + pb[0]) / 2, (pa[1] + pb[1]) / 2),
                source_role=roles[s.ref],
                sensitive_role=roles[t.ref],
                nearest_points=[[round(pa[0], 3), round(pa[1], 3)], [round(pb[0], 3), round(pb[1], 3)]],
            )
    return em.items
