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


def pad_geometry(p):
    """The copper of a pad: its ring, or for a custom-shaped pad the whole shape with holes and several parts."""
    polys = getattr(p, "polys", None)
    if p.shape == "custom" and polys:
        parts = []
        for rings in polys:
            if not rings or len(rings[0]) < 3:
                continue
            q = Polygon(rings[0], [r for r in rings[1:] if len(r) >= 3])
            if not q.is_valid:
                q = make_valid(q)
            parts.append(q)
        g = _polygonal(unary_union(parts)) if parts else None
        if g is not None:
            return g
    return ring_polygon(p.ring)


def graphic_geometry(g):
    """Copper drawn as a line, arc, rectangle, circle or polygon (``Board.graphics`` entry on a copper layer), or None.

    A filled shape is its area plus half the stroke width; an outline-only shape is a stroke along its boundary; a line is a stroke.
    Arcs arrive already as polylines. A zero-width line or outline has no copper.
    """
    pts = [(float(x), float(y)) for x, y in g.points]
    w = float(g.width or 0.0)
    if g.kind == "line":
        return LineString(pts).buffer(w / 2.0, cap_style="round", join_style="round", quad_segs=6) if len(pts) >= 2 and w > 0 else None
    if len(pts) < 3:
        return None
    if g.fill:
        poly = ring_polygon(pts)
        if poly is None:
            return None
        return poly.buffer(w / 2.0, join_style="round", quad_segs=4) if w > 0 else poly
    if w <= 0:
        return None
    return LineString(pts + [pts[0]]).buffer(w / 2.0, cap_style="round", join_style="round", quad_segs=4)


def copper_graphics(board: Board):
    """``[(id, layer, owner, geometry)]`` for every drawn copper shape of the board and of its footprints."""
    out = []
    for n, g in enumerate(getattr(board, "graphics", []) or []):
        if not str(g.layer).endswith(".Cu"):
            continue
        geo = graphic_geometry(g)
        if geo is None or geo.is_empty:
            continue
        out.append((f"gfx:{g.owner or 'board'}:{n}", g.layer, g.owner, _polygonal(geo)))
    return [o for o in out if o[3] is not None]


def narrowest_width(geom, floor: float = 0.05) -> float | None:
    """Narrowest width of a polygonal shape, found by morphological opening: the smallest trial width that removes area."""
    if geom is None or geom.is_empty or geom.area <= 0:
        return None
    for w in (0.1, 0.15, 0.2, 0.25, 0.3, 0.4, 0.5, 0.6, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0):
        opened = geom.buffer(-w / 2.0, join_style="mitre").buffer(w / 2.0, join_style="mitre")
        if geom.area - opened.area > 0.02 * geom.area:
            return w
    return None


def collect_items(board: Board, setup: RfSetup | None = None, antenna_tracks: set[str] | None = None) -> list[Item]:
    """Every piece of copper on the board as an Item. Antenna copper is flagged."""
    ant_net = setup.antenna_net if setup else None
    ant_fp = getattr(setup, "antenna_footprint", None) if setup else None
    items: list[Item] = []
    for t in board.tracks:
        pts = [tuple(p) for p in t.points()]
        poly = track_polygon(pts, t.width)
        if poly is None:
            continue
        is_ant = bool(ant_net and t.net == ant_net) or bool(antenna_tracks and t.id in antenna_tracks)
        items.append(Item("track", t.layer, t.net, poly, t.id, width=t.width, points=pts, antenna=is_ant))
    for p in board.pads:
        poly = pad_geometry(p)
        if poly is None:
            continue
        mw = narrowest_width(poly) if (p.shape == "custom" and getattr(p, "polys", None)) else None
        for layer in p.layers:
            if layer.endswith(".Cu"):
                items.append(Item("pad", layer, p.net, poly, p.id, ref=p.ref, antenna=bool(ant_net and p.net == ant_net), min_width=mw))
    # drawn copper (polygons, lines, arcs in footprints or on the board): a printed antenna from a library footprint is made of these
    for gid, layer, owner, gg in copper_graphics(board):
        is_ant = bool(ant_fp and owner == ant_fp)
        items.append(Item("gfx", layer, None, gg, gid, ref=owner, antenna=is_ant, min_width=narrowest_width(gg) if is_ant else None))
    if ant_fp:  # the footprint's own pads on the antenna net are antenna copper; its other pads (ground stub) join by overlap
        for it in items:
            if it.kind == "pad" and it.ref == ant_fp and it.net and it.net == ant_net:
                it.antenna = True
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
    # plated holes of the antenna footprint (the shorted stub of an inverted-F ties its radiator to the ground planes through one)
    ant_fp = getattr(setup, "antenna_footprint", None)
    if ant_fp:
        for p in board.pads_of(ant_fp):
            cu = [l for l in p.layers if l.endswith(".Cu")]
            if p.drill and p.drill > 0 and len(cu) >= 2 and reg[0] <= p.x <= reg[2] and reg[1] <= p.y <= reg[3]:
                out.append({"id": p.id, "net": p.net, "x": p.x, "y": p.y, "diameter": max(float(p.drill), 0.2), "layers": cu})
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
