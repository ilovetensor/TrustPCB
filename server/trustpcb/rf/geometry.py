"""Copper model: merge board copper per layer inside the region, simplify far copper, triangulate.

The solver cannot take polygons with holes (CSXCAD polygons are simple), so every merged copper
shape is split into triangles with a constrained Delaunay triangulation. Tracks become buffered
polylines, pads their rings, filled zones their fill rings, and vias cylinders.

Coordinates stay in board millimetres (x right, y down). The structure is therefore simulated
mirrored in y compared with a right-handed real world. Scalar results (S11, impedance, gain) are
unchanged by a mirror, and field images then line up with the board drawing.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

import shapely
from shapely import make_valid
from shapely.geometry import LineString, MultiPolygon, Point, Polygon, box
from shapely.ops import unary_union

from ..pcb.model import Board
from .types import Item, RfSetup

GROUND_RE = re.compile(r"^(/)?(A|D|P|S|E)?(GND|GROUND|VSS|0V)[A-Z0-9_]*$|^GND", re.I)
MIN_TRI_AREA = 1e-5  # mm2


def is_ground_name(name: str | None) -> bool:
    return bool(name and GROUND_RE.match(name.strip()))


# --------------------------------------------------------------------------- shapes


def ring_polygon(ring: Iterable[Iterable[float]]) -> Polygon | MultiPolygon | None:
    pts = [(float(p[0]), float(p[1])) for p in ring]
    if len(pts) < 3:
        return None
    poly = Polygon(pts)
    if not poly.is_valid:
        poly = make_valid(poly)
    return _polygonal(poly)


def _polygonal(g):
    """Keep only the area parts of a geometry."""
    if g is None or g.is_empty:
        return None
    if g.geom_type in ("Polygon", "MultiPolygon"):
        return g
    parts = [p for p in getattr(g, "geoms", []) if p.geom_type in ("Polygon", "MultiPolygon")]
    if not parts:
        return None
    return unary_union(parts)


def _circle(cx, cy, r, n=20):
    return [(cx + r * math.cos(2 * math.pi * k / n), cy + r * math.sin(2 * math.pi * k / n)) for k in range(n)]


def track_polygon(points: list[tuple[float, float]], width: float):
    if len(points) < 2 or width <= 0:
        return None
    return LineString(points).buffer(width / 2.0, cap_style="round", join_style="round", quad_segs=6)


# --------------------------------------------------------------------------- items


def collect_items(board: Board, setup: RfSetup | None = None, antenna_tracks: set[str] | None = None) -> list[Item]:
    """Every piece of copper on the board as an Item. Antenna copper is flagged."""
    ant_net = setup.antenna_net if setup else None
    items: list[Item] = []
    for t in board.tracks:
        pts = [tuple(p) for p in t.points()]
        poly = track_polygon(pts, t.width)
        if poly is None:
            continue
        is_ant = bool(ant_net and t.net == ant_net) or bool(antenna_tracks and t.id in antenna_tracks)
        items.append(Item("track", t.layer, t.net, poly, t.id, width=t.width, points=pts, antenna=is_ant))
    for p in board.pads:
        poly = ring_polygon(p.ring)
        if poly is None:
            continue
        for layer in p.layers:
            if layer.endswith(".Cu"):
                items.append(Item("pad", layer, p.net, poly, p.id, ref=p.ref, antenna=bool(ant_net and p.net == ant_net)))
    for v in board.vias:
        ring = ring_polygon(_circle(v.x, v.y, v.diameter / 2))
        for layer in v.layers:
            if layer.endswith(".Cu"):
                items.append(Item("via", layer, v.net, ring, v.id, antenna=bool(ant_net and v.net == ant_net)))
    for z in board.zones:
        if z.is_keepout:
            continue
        for layer, rings in z.filled.items():
            polys = [ring_polygon(r) for r in rings]
            polys = [q for q in polys if q is not None]
            if not polys:
                continue
            u = unary_union(polys)
            items.append(Item("zone", layer, z.net, u, z.id, antenna=bool(ant_net and z.net == ant_net)))
    return items


def union_of(items: Iterable[Item]):
    geoms = [i.geom for i in items if i.geom is not None and not i.geom.is_empty]
    return unary_union(geoms) if geoms else Polygon()


def board_outline_polygon(board: Board):
    rings = [ring_polygon(r) for r in board.outline]
    rings = [r for r in rings if r is not None]
    if not rings:
        x0, y0, x1, y1 = board.bbox
        return box(x0, y0, x1, y1)
    # first ring is the outer boundary; any further ring that lies inside it is a cut-out
    rings.sort(key=lambda g: -g.area)
    out = rings[0]
    for r in rings[1:]:
        if out.contains(r):
            out = out.difference(r)
        else:
            out = out.union(r)
    return out


# --------------------------------------------------------------------------- model


@dataclass
class CopperModel:
    layers: dict[str, Any]  # layer name -> shapely geometry, region clipped and simplified
    antenna: dict[str, Any]  # layer name -> antenna copper geometry
    vias: list[dict[str, Any]]
    region: list[float]
    fine_zone: list[float]
    outline: Any
    notes: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    items: list = field(default_factory=list)


def collect_items_cached(model):
    return model.items


def _far_clean(geom, close_mm: float, simplify_mm: float, min_hole: float, min_island: float):
    """Make far copper cheap: bridge slivers, drop tiny holes and islands, simplify the outline."""
    if geom is None or geom.is_empty:
        return Polygon()
    g = geom.buffer(close_mm, join_style="mitre", quad_segs=2).buffer(-close_mm, join_style="mitre", quad_segs=2)
    polys = []
    for p in getattr(g, "geoms", [g]):
        if p.geom_type != "Polygon" or p.area < min_island:
            continue
        holes = [h for h in p.interiors if Polygon(h).area >= min_hole]
        polys.append(Polygon(p.exterior, holes))
    if not polys:
        return Polygon()
    g = unary_union(polys).simplify(simplify_mm, preserve_topology=True)
    return g


def build_model(
    board: Board,
    setup: RfSetup,
    *,
    ops=None,
    region: list[float] | None = None,
    far: dict[str, float] | None = None,
) -> CopperModel:
    """Merge copper per layer for the simulation region.

    Exact inside ``setup.fine_zone``. Outside it: zone fills of any net, plus tracks and pads of the
    ground and antenna nets. Everything else is dropped and the far shapes are simplified.
    """
    from . import whatif  # local import: whatif builds on the types only

    reg = list(region or setup.region)
    fz = list(setup.fine_zone)
    fz = [max(fz[0], reg[0]), max(fz[1], reg[1]), min(fz[2], reg[2]), min(fz[3], reg[3])]
    far = {"close": 0.45, "simplify": 0.12, "min_hole": 1.5, "min_island": 1.5, **(far or {})}
    items = collect_items(board, setup, set(_antenna_track_ids(board, setup)))
    notes: list[str] = []
    if ops:
        items, op_notes = whatif.apply_to_items(items, ops, board, setup)
        notes += op_notes

    outline = board_outline_polygon(board)
    region_box = box(*reg)
    fine_box = box(*fz)
    clip = outline.intersection(region_box)

    ground_nets = {n.name for n in board.nets if is_ground_name(n.name)}
    layers: dict[str, Any] = {}
    antenna: dict[str, Any] = {}
    stats = {"items": len(items), "dropped_far_items": 0, "kept_far_items": 0}
    copper_layers = [l.name for l in board.layers if l.kind == "copper"]
    for layer in copper_layers:
        its = [i for i in items if i.layer == layer]
        near, far_items = [], []
        for i in its:
            if i.geom is None or i.geom.is_empty or not i.geom.intersects(region_box):
                continue
            keep_far = i.kind == "zone" or i.antenna or (i.net in ground_nets)
            if i.geom.intersects(fine_box):
                near.append(i)
            if keep_far and not i.geom.within(fine_box):
                far_items.append(i)
            elif not keep_far and not i.geom.within(fine_box):
                stats["dropped_far_items"] += 1
        exact = union_of(near).intersection(fine_box)
        far_u = union_of(far_items).intersection(region_box)
        far_u = _far_clean(far_u, far["close"], far["simplify"], far["min_hole"], far["min_island"]).difference(fine_box)
        stats["kept_far_items"] += len(far_items)
        merged = unary_union([exact, far_u]).intersection(clip)
        merged = _polygonal(merged) or Polygon()
        layers[layer] = merged
        antenna[layer] = _polygonal(union_of([i for i in its if i.antenna]).intersection(region_box)) or Polygon()

    # clear_copper_region operations act on the merged copper
    if ops:
        layers, antenna, op_notes = whatif.apply_to_layers(layers, antenna, ops, setup)
        notes += op_notes

    vias = _collect_vias(board, setup, reg, fz, ground_nets, ops)
    # a via only counts when copper of its layers lies under it: otherwise it joins nothing
    kept = []
    for v in vias:
        pt = Point(v["x"], v["y"])
        on = [l for l in v["layers"] if l in layers and layers[l].buffer(0.05).contains(pt)]
        if len(on) >= 2:
            kept.append(v)
    stats["vias_total"] = len(vias)
    stats["vias_kept"] = len(kept)
    return CopperModel(layers, antenna, kept, reg, fz, outline, notes, stats, items)


def _antenna_track_ids(board: Board, setup: RfSetup) -> list[str]:
    return [t.id for t in board.tracks if t.net == setup.antenna_net]


def _collect_vias(board, setup, reg, fz, ground_nets, ops) -> list[dict[str, Any]]:
    out = []
    removed = set()
    if ops:
        for op in ops:
            if getattr(op, "name", "") == "remove_nearby_part":
                removed.add(op.ref)
    for v in board.vias:
        if not (reg[0] <= v.x <= reg[2] and reg[1] <= v.y <= reg[3]):
            continue
        near = fz[0] <= v.x <= fz[2] and fz[1] <= v.y <= fz[3]
        if v.net in ground_nets or v.net == setup.antenna_net or near:
            out.append({"id": v.id, "net": v.net, "x": v.x, "y": v.y, "diameter": v.diameter, "layers": list(v.layers)})
    return out


# --------------------------------------------------------------------------- triangles


def triangulate(geom) -> list[list[list[float]]]:
    """Constrained Delaunay triangles of a polygonal geometry, holes respected."""
    tris: list[list[list[float]]] = []
    if geom is None or geom.is_empty:
        return tris
    for poly in getattr(geom, "geoms", [geom]):
        if poly.geom_type != "Polygon" or poly.is_empty:
            continue
        res = shapely.constrained_delaunay_triangles(poly)
        for t in getattr(res, "geoms", [res]):
            if t.geom_type != "Polygon" or t.area < MIN_TRI_AREA:
                continue
            c = list(t.exterior.coords)[:3]
            tris.append([[round(x, 4), round(y, 4)] for x, y in c])
    return tris


def triangles_area(tris) -> float:
    a = 0.0
    for (x0, y0), (x1, y1), (x2, y2) in tris:
        a += abs((x1 - x0) * (y2 - y0) - (x2 - x0) * (y1 - y0)) / 2
    return a


def polygon_edges(geom) -> Iterable[tuple[tuple[float, float], tuple[float, float], int]]:
    """Every boundary edge of a polygonal geometry with the side the metal is on.

    The third value is the metal side as +1 when the metal lies to the left of the direction of
    travel and -1 on the right, after orienting exteriors counter-clockwise and holes clockwise.
    """
    if geom is None or geom.is_empty:
        return
    for poly in getattr(geom, "geoms", [geom]):
        if poly.geom_type != "Polygon":
            continue
        poly = shapely.geometry.polygon.orient(poly, 1.0)  # exterior CCW, interiors CW: metal on the left
        for ring in [poly.exterior, *poly.interiors]:
            c = list(ring.coords)
            for a, b in zip(c[:-1], c[1:]):
                yield a, b, 1
