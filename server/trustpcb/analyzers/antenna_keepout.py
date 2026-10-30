"""antenna_keepout: copper, parts and tracks inside an antenna clearance zone.

Clearance zones are the board's copper keep-outs that are named for an antenna or that contain or
touch antenna copper, plus, where an antenna part or patch has no such keep-out, a zone grown
around it by ``antenna_clearance_mm``. Items on the antenna's own nets (its feed, its matching
parts) are not intruders.
"""

from __future__ import annotations

import re
from typing import Any

from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union

from ..pcb.model import Board
from .base import AnalyzerContext, Emitter, bbox_target, mark, meas, polygon_geometry
from .classify import classify_parts, footprint_ring, part_kind

RULE = "antenna_keepout"
_NAME_RE = re.compile(r"ant|rf|keep.?clear|keep.?out", re.I)
_ANT_NET_RE = re.compile(r"(^|[^A-Z])(ANT|ANTENNA)([^A-Z]|$)")


def _antenna_nets(board: Board, ctx: AnalyzerContext) -> set[str]:
    return {n.name for n in board.nets if _ANT_NET_RE.search(n.name.upper()) or n.name in ctx.rf_nets and "ANT" in n.name.upper()}


def _antenna_parts(board: Board, ctx: AnalyzerContext) -> list:
    classify_parts(board, ctx)
    out = []
    for f in sorted(board.footprints, key=lambda f: f.ref):
        if part_kind(f) == "antenna" or str(ctx.part_roles.get(f.ref, "")).lower() in ("antenna", "ant"):
            out.append(f)
    return out


def _antenna_shapes(board: Board, ant_nets: set[str], ant_parts: list) -> list[tuple[str, Polygon, list[str]]]:
    """(label, polygon, targets) for antenna copper that needs a derived clearance zone."""
    shapes = []
    for f in ant_parts:
        shapes.append((f.ref, Polygon(footprint_ring(f)).buffer(0), [f.id]))
    for z in sorted(board.zones, key=lambda z: z.id):
        if z.net in ant_nets:
            rings = [r for rs in z.filled.values() for r in rs] or ([z.outline] if z.outline else [])
            polys = [Polygon(r).buffer(0) for r in rings if len(r) >= 3]
            if polys:
                shapes.append((z.net or z.id, unary_union(polys), [z.id]))
    return shapes


def _layers_hit(item_layers: list[str], zone_layers: list[str]) -> bool:
    return bool(set(item_layers) & set(zone_layers)) if zone_layers else True


def analyze(board: Board, ctx: AnalyzerContext) -> list[dict[str, Any]]:
    ant_nets = _antenna_nets(board, ctx) | {n for n in (ctx.rf_nets or []) if "ANT" in n.upper()}
    ant_parts = _antenna_parts(board, ctx)
    ant_refs = {f.ref for f in ant_parts}
    clearance = float(ctx.t("antenna_clearance_mm"))
    zones: list[dict[str, Any]] = []  # {id, label, poly, layers, target, derived}

    # copper antenna objects, used to decide which keep-outs are antenna zones
    ant_geoms = []
    for t in board.tracks:
        if t.net in ant_nets:
            ant_geoms.append(LineString(t.points()).buffer(t.width / 2))
    for p in board.pads:
        if p.net in ant_nets or p.ref in ant_refs:
            ant_geoms.append(Polygon(p.ring).buffer(0))
    ant_union = unary_union(ant_geoms) if ant_geoms else None
    shapes = _antenna_shapes(board, ant_nets, ant_parts)

    covered_shapes: set[int] = set()
    for k in sorted(board.keepouts, key=lambda k: k.id):
        if not any(v == "not_allowed" for v in k.rules.values()):
            continue
        poly = Polygon(k.ring).buffer(0)
        if poly.is_empty:
            continue
        touches = ant_union is not None and poly.intersects(ant_union)
        for i, (_, shp, _) in enumerate(shapes):
            if poly.buffer(clearance).intersects(shp):
                touches = True
                covered_shapes.add(i)
        if _NAME_RE.search(k.name or "") or touches:
            zones.append({"id": k.id, "label": k.name or k.id, "poly": poly, "layers": k.layers, "target": k.id, "derived": False})
    for i, (label, shp, targets) in enumerate(shapes):
        if i in covered_shapes:
            continue
        grown = shp.buffer(clearance, join_style=2)
        box = list(grown.bounds)
        zones.append({"id": f"antenna:{label}", "label": f"{label} clearance", "poly": grown, "layers": [], "target": bbox_target(box), "derived": True, "exempt_shape": shp, "ring": [[round(x, 4), round(y, 4)] for x, y in list(grown.exterior.coords)[:-1]] if grown.geom_type == "Polygon" else None})

    em = Emitter(RULE)
    for zn in zones:
        poly: Polygon = zn["poly"]
        layers: list[str] = zn["layers"]
        intr: dict[str, list[str]] = {"tracks": [], "vias": [], "pads": [], "parts": [], "zones": []}
        track_len = 0.0
        zone_area = 0.0
        first_pt = None
        part_hit: set[str] = set()
        for f in sorted(board.footprints, key=lambda f: f.ref):
            if f.ref in ant_refs or part_kind(f) in ("mounting", "fiducial", "testpoint"):
                continue
            side_layer = "F.Cu" if f.side == "top" else "B.Cu"
            if not _layers_hit([side_layer], layers):
                continue
            if any(p.net in ant_nets for p in board.pads_of(f.ref)):
                continue
            if Polygon(footprint_ring(f)).buffer(0).intersects(poly):
                intr["parts"].append(f.id)
                part_hit.add(f.ref)
                first_pt = first_pt or (f.x, f.y)
        for t in sorted(board.tracks, key=lambda t: t.id):
            if t.net in ant_nets or not _layers_hit([t.layer], layers):
                continue
            line = LineString(t.points())
            if line.buffer(t.width / 2).intersects(poly):
                intr["tracks"].append(t.id)
                track_len += line.intersection(poly).length
                first_pt = first_pt or (t.start[0], t.start[1])
        for v in sorted(board.vias, key=lambda v: v.id):
            if v.net in ant_nets or not _layers_hit(v.layers, layers):
                continue
            if Point(v.x, v.y).buffer(v.diameter / 2).intersects(poly):
                intr["vias"].append(v.id)
                first_pt = first_pt or (v.x, v.y)
        for p in sorted(board.pads, key=lambda p: p.id):
            if p.net in ant_nets or p.ref in part_hit or p.ref in ant_refs or not _layers_hit(p.layers, layers):
                continue
            if Polygon(p.ring).buffer(0).intersects(poly):
                intr["pads"].append(p.id)
                first_pt = first_pt or (p.x, p.y)
        for z in sorted(board.zones, key=lambda z: z.id):
            if z.net in ant_nets or z.is_keepout:
                continue
            for layer, rings in z.filled.items():
                if not _layers_hit([layer], layers):
                    continue
                area = sum(Polygon(r).buffer(0).intersection(poly).area for r in rings if len(r) >= 3)
                if area > 0.01:
                    zone_area += area
                    if z.id not in intr["zones"]:
                        intr["zones"].append(z.id)
        if not any(intr.values()):
            continue
        targets = [x for key in ("parts", "tracks", "vias", "pads", "zones") for x in intr[key]]
        region_target = zn["target"]
        region = mark("region", [region_target], label=zn["label"], role="antenna_keepout")
        if zn["derived"] and zn.get("ring"):
            region["resolved"] = [polygon_geometry(zn["ring"], region_target)]
        m: dict[str, Any] = {
            "tracks": meas(len(intr["tracks"]), "count"),
            "vias": meas(len(intr["vias"]), "count"),
            "pads": meas(len(intr["pads"]), "count"),
            "parts": meas(len(intr["parts"]), "count"),
            "track_length_inside": meas(track_len, "mm", 2),
        }
        if intr["zones"]:
            m["copper_pour_inside"] = meas(zone_area, "mm2", 2)
        c = poly.centroid
        em.add(
            f"{len(targets)} item(s) inside {zn['label']}",
            m,
            [region_target] + targets,
            [region, mark("object", targets, label="inside antenna zone")],
            "high" if intr["parts"] or intr["vias"] else "medium",
            position=first_pt or (c.x, c.y),
            zone=zn["id"],
            derived_zone=zn["derived"],
            intruders={k: v for k, v in intr.items() if v},
        )
    return em.items
