"""antenna_keepout: copper, parts and tracks inside an antenna clearance zone.

Clearance zones are the board's copper keep-outs that are named for an antenna or that contain or
touch antenna copper, plus, where an antenna part or patch has no such keep-out, a zone grown
around it by ``antenna_clearance_mm``. Items on the antenna's own network (its nets and the nets reached
through series parts, never ground or power) are not intruders, and neither are the vias of a ground via
fence (three or more ground vias in a line); a via with no net is reported.

Copper pours are measured per layer for any copper layer, with the ground ones marked. Ground pour on a layer
other than the antenna's own, under the antenna itself, is reported (``ground_under_antenna``), and so is the
distance from the antenna to the nearest ground copper on each layer (``ground_distance_by_layer``).
"""

from __future__ import annotations

import math
import re
from typing import Any

from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union

from ..pcb.model import Board
from .base import AnalyzerContext, Emitter, bbox_target, mark, meas, polygon_geometry
from .classify import classify_parts, footprint_ring, is_ground, is_power, part_kind

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


def _own_nets(board: Board, ctx: AnalyzerContext, ant_nets: set[str]) -> set[str]:
    """The antenna's nets plus nets reached through series parts (a matching network), at most two parts away."""
    own = set(ant_nets)
    for _ in range(2):
        add: set[str] = set()
        for f in board.footprints:
            if part_kind(f) not in ("cap", "res", "ind", "ferrite"):
                continue
            pads = board.pads_of(f.ref)
            if len(pads) != 2:
                continue
            a, b = pads[0].net, pads[1].net
            for x, y in ((a, b), (b, a)):
                if x in own and y and y not in own and not is_ground(y) and not is_power(y, ctx):
                    add.add(y)
        if not add:
            break
        own |= add
    return own


def _via_fences(vias: list, max_gap: float = 2.5, min_deg: float = 155.0) -> set[str]:
    """Ids of ground vias that stand in a line of three or more (a via fence): a via with two neighbours within
    ``max_gap`` on opposite sides (the angle at it at least ``min_deg``) joins them."""
    fence: set[str] = set()
    for b in vias:
        near = [v for v in vias if v is not b and math.dist((v.x, v.y), (b.x, b.y)) <= max_gap]
        for i, a in enumerate(near):
            for c in near[i + 1:]:
                ux, uy, wx, wy = a.x - b.x, a.y - b.y, c.x - b.x, c.y - b.y
                nu, nw = math.hypot(ux, uy), math.hypot(wx, wy)
                if nu > 1e-6 and nw > 1e-6 and math.degrees(math.acos(max(-1.0, min(1.0, (ux * wx + uy * wy) / (nu * nw))))) >= min_deg:
                    fence |= {a.id, b.id, c.id}
    return fence


def _zone_polys(z, layer: str) -> list[Polygon]:
    rings = z.filled.get(layer)
    if rings:
        return [p for p in (Polygon(r).buffer(0) for r in rings if len(r) >= 3) if not p.is_empty]
    if not z.filled and len(z.outline) >= 3 and layer in z.layers:
        p = Polygon(z.outline).buffer(0)
        return [] if p.is_empty else [p]
    return []


def _antenna_copper(board: Board, ant_parts: list, ant_shape):
    """The antenna's own copper: its copper graphics and pads on its parts; the whole footprint when none is drawn."""
    geoms = []
    for f in ant_parts:
        if not Polygon(footprint_ring(f)).buffer(0).intersects(ant_shape):
            continue
        for g in board.graphics:
            if g.owner == f.ref and g.layer.endswith(".Cu") and len(g.points) >= 2:
                geoms.append((Polygon(g.points).buffer(max(g.width, 0.0) / 2) if g.kind == "poly" and len(g.points) >= 3 else LineString(g.points).buffer(max(g.width, 0.01) / 2)))
        geoms.extend(Polygon(p.ring).buffer(0) for p in board.pads_of(f.ref))
    geoms = [g for g in geoms if not g.is_empty]
    return unary_union(geoms) if geoms else ant_shape


def _ground_distances(board: Board, ant_shape, own: set[str], ant_refs: set[str]) -> dict[str, float]:
    """Per copper layer: distance from the antenna to the nearest ground copper (zone fill, track, pad or via) other than the antenna's own."""
    copper = board.copper_layers()
    out: dict[str, float] = {}
    for layer in copper:
        geoms = []
        for z in board.zones:
            if not z.is_keepout and is_ground(z.net) and z.net not in own:
                geoms.extend(_zone_polys(z, layer))
        for t in board.tracks:
            if t.layer == layer and is_ground(t.net) and t.net not in own:
                geoms.append(LineString(t.points()).buffer(t.width / 2))
        for p in board.pads:
            if layer in p.layers and is_ground(p.net) and p.net not in own and p.ref not in ant_refs:
                geoms.append(Polygon(p.ring).buffer(0))
        for v in board.vias:
            if is_ground(v.net) and v.net not in own and (layer in v.layers or len(v.layers) >= 2):
                geoms.append(Point(v.x, v.y).buffer(v.diameter / 2))
        if geoms:
            out[layer] = round(float(unary_union(geoms).distance(ant_shape)), 3)
    return out


def analyze(board: Board, ctx: AnalyzerContext) -> list[dict[str, Any]]:
    ant_nets = _antenna_nets(board, ctx) | {n for n in (ctx.rf_nets or []) if "ANT" in n.upper()}
    ant_parts = _antenna_parts(board, ctx)
    ant_refs = {f.ref for f in ant_parts}
    clearance = float(ctx.t("antenna_clearance_mm"))
    own = _own_nets(board, ctx, ant_nets)
    ant_box = unary_union([Polygon(footprint_ring(f)).buffer(0) for f in ant_parts]).buffer(clearance + 3.0).bounds if ant_parts else None
    fence_ids = _via_fences([v for v in board.vias if is_ground(v.net) and v.net not in own and (ant_box is None or ant_box[0] <= v.x <= ant_box[2] and ant_box[1] <= v.y <= ant_box[3])])
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
            near = [shp for _, shp, _ in shapes if poly.buffer(clearance).intersects(shp)]
            zones.append({"id": k.id, "label": k.name or k.id, "poly": poly, "layers": k.layers, "target": k.id, "derived": False,
                          "ant_shape": unary_union(near) if near else ant_union})
    for i, (label, shp, targets) in enumerate(shapes):
        if i in covered_shapes:
            continue
        grown = shp.buffer(clearance, join_style=2)
        box = list(grown.bounds)
        zones.append({"id": f"antenna:{label}", "label": f"{label} clearance", "poly": grown, "layers": [], "target": bbox_target(box), "derived": True, "exempt_shape": shp, "ant_shape": shp, "ring": [[round(x, 4), round(y, 4)] for x, y in list(grown.exterior.coords)[:-1]] if grown.geom_type == "Polygon" else None})

    em = Emitter(RULE)
    for zn in zones:
        poly: Polygon = zn["poly"]
        layers: list[str] = zn["layers"]
        intr: dict[str, list[str]] = {"tracks": [], "vias": [], "pads": [], "parts": [], "zones": []}
        track_len = 0.0
        zone_area = 0.0
        pour: dict[tuple[str, str | None], float] = {}
        ground_under: dict[str, float] = {}
        fence_vias: list[str] = []
        floating_vias: list[str] = []
        ant_shape = zn.get("ant_shape")
        first_pt = None
        part_hit: set[str] = set()
        for f in sorted(board.footprints, key=lambda f: f.ref):
            if f.ref in ant_refs or part_kind(f) in ("mounting", "fiducial", "testpoint"):
                continue
            side_layer = "F.Cu" if f.side == "top" else "B.Cu"
            if not _layers_hit([side_layer], layers):
                continue
            if any(p.net in own for p in board.pads_of(f.ref)):
                continue
            if Polygon(footprint_ring(f)).buffer(0).intersects(poly):
                intr["parts"].append(f.id)
                part_hit.add(f.ref)
                first_pt = first_pt or (f.x, f.y)
        for t in sorted(board.tracks, key=lambda t: t.id):
            if t.net in own or not _layers_hit([t.layer], layers):
                continue
            line = LineString(t.points())
            if line.buffer(t.width / 2).intersects(poly):
                intr["tracks"].append(t.id)
                track_len += line.intersection(poly).length
                first_pt = first_pt or (t.start[0], t.start[1])
        for v in sorted(board.vias, key=lambda v: v.id):
            if v.net in own or not _layers_hit(v.layers, layers):
                continue
            if Point(v.x, v.y).buffer(v.diameter / 2).intersects(poly):
                if v.id in fence_ids:
                    fence_vias.append(v.id)
                    continue
                intr["vias"].append(v.id)
                if not v.net:
                    floating_vias.append(v.id)
                first_pt = first_pt or (v.x, v.y)
        for p in sorted(board.pads, key=lambda p: p.id):
            if p.net in own or p.ref in part_hit or p.ref in ant_refs or not _layers_hit(p.layers, layers):
                continue
            if Polygon(p.ring).buffer(0).intersects(poly):
                intr["pads"].append(p.id)
                first_pt = first_pt or (p.x, p.y)
        side_layer = None
        if ant_shape is not None:
            ant_fps = [f for f in ant_parts if Polygon(footprint_ring(f)).buffer(0).intersects(ant_shape)]
            side_layer = "B.Cu" if ant_fps and all(f.side != "top" for f in ant_fps) else "F.Cu"
        for z in sorted(board.zones, key=lambda z: z.id):
            if z.net in own or z.is_keepout:
                continue
            for layer in board.copper_layers():
                if not _layers_hit([layer], layers):
                    continue
                polys = _zone_polys(z, layer)
                area = sum(g.intersection(poly).area for g in polys)
                if area > 0.01:
                    zone_area += area
                    pour[(layer, z.net)] = pour.get((layer, z.net), 0.0) + area
                    if z.id not in intr["zones"]:
                        intr["zones"].append(z.id)
                if ant_shape is not None and is_ground(z.net) and layer != side_layer:
                    under = sum(g.intersection(ant_shape).area for g in polys)
                    if under > 0.01:
                        ground_under[layer] = ground_under.get(layer, 0.0) + under
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
        pour_rows = [{"layer": layer, "net": net, "area_mm2": round(a, 2), "ground": is_ground(net)} for (layer, net), a in sorted(pour.items(), key=lambda kv: (kv[0][0], kv[0][1] or ""))]
        for row in pour_rows:
            m[f"pour_{row['layer']}_{row['net'] or 'no_net'}"] = meas(row["area_mm2"], "mm2", 2)
        dist = _ground_distances(board, _antenna_copper(board, ant_parts, ant_shape), own, ant_refs) if ant_shape is not None else {}
        if dist:
            m["ground_distance_min"] = meas(min(dist.values()), "mm", 2)
            outer = [d for layer, d in dist.items() if layer in ("F.Cu", "B.Cu")]
            if outer:
                m["ground_distance_surface"] = meas(min(outer), "mm", 2)
        if ground_under:
            m["ground_under_antenna"] = meas(sum(ground_under.values()), "mm2", 2)
        if fence_vias:
            m["via_fence_vias"] = meas(len(fence_vias), "count")
        if floating_vias:
            m["vias_without_net"] = meas(len(floating_vias), "count")
        c = poly.centroid
        em.add(
            f"{len(targets)} item(s) inside {zn['label']}",
            m,
            [region_target] + targets,
            [region, mark("object", targets, label="inside antenna zone")],
            "high" if intr["parts"] or intr["vias"] or ground_under else "medium",
            position=first_pt or (c.x, c.y),
            zone=zn["id"],
            derived_zone=zn["derived"],
            intruders={k: v for k, v in intr.items() if v},
            copper_pour_by_layer=pour_rows,
            ground_distance_by_layer=dist,
            ground_under_antenna_by_layer={k: round(v, 2) for k, v in sorted(ground_under.items())},
            via_fence=fence_vias,
            vias_without_net=floating_vias,
            own_network=sorted(own),
        )
    return em.items
