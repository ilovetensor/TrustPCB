"""zones: parts grouped by role, one outline per cluster of a role, and zones that overlap.

The outline is the convex hull of the cluster's courtyards, grown by a small pad. Each role is
registered on the board as ``zone_role:<role>`` (its part references). A region mark carries the
hull as its own ``resolved`` geometry, because no board id names a computed hull.
"""

from __future__ import annotations

from typing import Any

from shapely.geometry import Polygon
from shapely.ops import unary_union

from ..pcb.model import Board
from .base import AnalyzerContext, Emitter, mark, meas, polygon_geometry
from .classify import classify_parts, footprint_ring, role_basis

RULE = "zones"


def _clusters(parts: list[tuple[str, Polygon]], gap: float) -> list[list[tuple[str, Polygon]]]:
    """Single-link clusters: parts within ``gap`` mm of each other share a cluster."""
    remaining = list(parts)
    out: list[list[tuple[str, Polygon]]] = []
    while remaining:
        cluster = [remaining.pop(0)]
        grew = True
        while grew:
            grew = False
            for item in list(remaining):
                if any(item[1].distance(c[1]) <= gap for c in cluster):
                    cluster.append(item)
                    remaining.remove(item)
                    grew = True
        out.append(sorted(cluster, key=lambda c: c[0]))
    out.sort(key=lambda c: c[0][0])
    return out


def _inside_board(hull: Polygon, board: Board) -> Polygon:
    """The hull trimmed to the board, so a connector overhanging the edge does not draw a zone off the board."""
    from ..rf.geometry import board_outline_polygon
    trimmed = hull.intersection(board_outline_polygon(board))
    return trimmed if trimmed.geom_type == "Polygon" and not trimmed.is_empty else hull


def compute_regions(board: Board, ctx: AnalyzerContext) -> list[dict[str, Any]]:
    """Regions as ``{role, index, refs, polygon (shapely), ring, area}`` in a stable order."""
    roles = classify_parts(board, ctx)
    by_role: dict[str, list[tuple[str, Polygon]]] = {}
    for fp in sorted(board.footprints, key=lambda f: f.ref):
        role = roles.get(fp.ref)
        if not role or role == "mechanical":
            continue
        ring = footprint_ring(fp)
        poly = Polygon(ring).buffer(0)
        if poly.is_empty:
            continue
        by_role.setdefault(role, []).append((fp.ref, poly))
    pad = float(ctx.t("zone_pad_mm"))
    gap = float(ctx.t("zone_cluster_mm"))
    regions: list[dict[str, Any]] = []
    for role in sorted(by_role):
        clusters = _clusters(by_role[role], gap)
        for i, cluster in enumerate(clusters, start=1):
            hull = unary_union([c[1] for c in cluster]).convex_hull
            hull = hull.buffer(pad, join_style=2) if pad > 0 else hull
            hull = hull.convex_hull if hull.geom_type != "Polygon" else hull
            hull = _inside_board(hull, board)
            ring = [[round(x, 4), round(y, 4)] for x, y in list(hull.exterior.coords)[:-1]]
            regions.append({"role": role, "index": i, "of": len(clusters), "refs": [c[0] for c in cluster], "polygon": hull, "ring": ring, "area": hull.area})
    return regions


def role_refs(regions: list[dict[str, Any]]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for r in regions:
        out.setdefault(r["role"], []).extend(r["refs"])
    return {k: sorted(set(v)) for k, v in out.items()}


def _basis_lines(region: dict[str, Any], basis: dict[str, dict[str, Any]]) -> list[str]:
    """Why passives sit in this zone: each was grouped with the active part it shares a net with (its own pull-ups, filter or decoupling). That is a grouping
    by net, so it says nothing about noise between the passive and that part."""
    follows: dict[str, tuple[list[str], set[str]]] = {}
    for ref in region["refs"]:
        b = basis.get(ref)
        if b:
            refs, nets = follows.setdefault(b["follows"], ([], set()))
            refs.append(ref)
            nets.update(b["via_nets"])
    return [f"{region['role']} zone: {', '.join(sorted(refs))} follow{'s' if len(refs) == 1 else ''} {anchor} (shared nets {', '.join(sorted(nets)[:4])}); they are {anchor}'s own connected parts grouped by net, not separate {region['role']} parts."
            for anchor, (refs, nets) in sorted(follows.items())]


def analyze(board: Board, ctx: AnalyzerContext) -> list[dict[str, Any]]:
    regions = compute_regions(board, ctx)
    ctx.cache["zone_regions"] = regions
    em = Emitter(RULE)
    for role, refs in sorted(role_refs(regions).items()):
        board.register_zone_role(role, [f"ref:{r}" for r in refs])
    for r in regions:
        role = r["role"]
        label = role if r["of"] == 1 else f"{role} {r['index']}"
        target = f"zone_role:{role}"
        m = mark("region", [target], label=label, role=role)
        m["resolved"] = [polygon_geometry(r["ring"], target)]
        em.add(
            f"Zone {label}",
            {"parts": meas(len(r["refs"]), "count"), "area": meas(r["area"], "mm2", 2)},
            [f"ref:{ref}" for ref in r["refs"]],
            [m],
            "info",
            position=r["polygon"].centroid.coords[0],
            role=role,
            parts=list(r["refs"]),
            ring=r["ring"],
        )
    # overlaps between different roles
    basis = role_basis(board, ctx)
    min_area = float(ctx.t("zone_overlap_min_mm2"))
    for i, a in enumerate(regions):
        for b in regions[i + 1 :]:
            if a["role"] == b["role"]:
                continue
            inter = a["polygon"].intersection(b["polygon"])
            if inter.is_empty or inter.area < min_area:
                continue
            smaller = min(a["area"], b["area"])
            ring = [[round(x, 4), round(y, 4)] for x, y in list(inter.convex_hull.exterior.coords)[:-1]] if inter.convex_hull.geom_type == "Polygon" else []
            ta, tb = f"zone_role:{a['role']}", f"zone_role:{b['role']}"
            marks = []
            if ring:
                ov = mark("region", [ta, tb], label=f"{a['role']} / {b['role']} overlap", role="overlap")
                ov["resolved"] = [polygon_geometry(ring, ta)]
                marks.append(ov)
            details = _basis_lines(a, basis) + _basis_lines(b, basis)
            em.add(
                f"Zones {a['role']} and {b['role']} overlap",
                {"overlap_area": meas(inter.area, "mm2", 2), "overlap_share_of_smaller": meas(100.0 * inter.area / smaller if smaller else 0.0, "percent", 1)},
                [f"ref:{ref}" for ref in a["refs"][:3]] + [f"ref:{ref}" for ref in b["refs"][:3]],
                marks,
                "low",
                position=inter.centroid.coords[0],
                details=details or None,
                roles=[a["role"], b["role"]],
                zone_parts={a["role"]: list(a["refs"]), b["role"]: list(b["refs"])},
            )
    return em.items


def overlays(board: Board, ctx: AnalyzerContext) -> dict[str, Any]:
    regions = ctx.cache.get("zone_regions") or compute_regions(board, ctx)
    return {
        "label": "Function zones: hull of each role's parts (computed, not drawn on the board)",
        "zones": [{"role": r["role"], "index": r["index"], "parts": r["refs"], "ring": r["ring"], "area_mm2": round(r["area"], 2)} for r in regions],
    }
