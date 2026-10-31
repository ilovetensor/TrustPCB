"""What-if operations. A fixed list of typed changes with hard limits.

They exist only inside the simulation model. Nothing here reads or writes a KiCad file; the
operations edit an in-memory list of copper items and the merged shapes. ``tune_matching`` is not
here because it changes no geometry (see matching.py).

| Operation | Limits |
|---|---|
| adjust_antenna_length | +-12 mm along the detected antenna |
| clear_copper_region | polygon or box near the antenna, area up to 150 mm2, never over antenna copper |
| remove_nearby_part | named part inside the investigation region, not a matching part |
| set_material | permittivity 3.8 to 4.8 (loss tangent 0.005 to 0.05 when given) |
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any

from shapely.geometry import LineString, Point, Polygon, box

from .types import Item, RfSetup

MAX_LENGTH_DELTA_MM = 12.0
MAX_CLEAR_AREA_MM2 = 150.0
CLEAR_NEAR_MM = 12.0  # a cleared region must come within this distance of the antenna copper
REMOVE_NEAR_MM = 20.0
EPS_RANGE = (3.8, 4.8)
TAND_RANGE = (0.005, 0.05)
MIN_EXTEND_CLEARANCE_MM = 0.15


class WhatIfError(ValueError):
    """A what-if request outside its limits. ``code`` is machine readable."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def to_dict(self) -> dict[str, str]:
        return {"error": self.code, "message": self.message}


@dataclass(frozen=True)
class AdjustAntennaLength:
    delta_mm: float
    name: str = "adjust_antenna_length"


@dataclass(frozen=True)
class ClearCopperRegion:
    bbox: tuple[float, float, float, float] | None = None
    polygon: tuple[tuple[float, float], ...] | None = None
    layers: tuple[str, ...] | None = None
    name: str = "clear_copper_region"


@dataclass(frozen=True)
class RemoveNearbyPart:
    ref: str
    name: str = "remove_nearby_part"


@dataclass(frozen=True)
class SetMaterial:
    epsilon_r: float
    loss_tangent: float | None = None
    name: str = "set_material"


Op = AdjustAntennaLength | ClearCopperRegion | RemoveNearbyPart | SetMaterial


def op_to_dict(op: Op) -> dict[str, Any]:
    d = {k: (list(v) if isinstance(v, tuple) else v) for k, v in asdict(op).items()}
    d["op"] = d.pop("name")
    if "polygon" in d and d["polygon"] is not None:
        d["polygon"] = [list(p) for p in d["polygon"]]
    return d


def op_from_dict(d: dict[str, Any]) -> Op:
    kind = d.get("op") or d.get("name")
    try:
        if kind == "adjust_antenna_length":
            return AdjustAntennaLength(float(d["delta_mm"]))
        if kind == "clear_copper_region":
            bb = d.get("bbox")
            poly = d.get("polygon")
            return ClearCopperRegion(
                tuple(float(v) for v in bb) if bb else None,
                tuple((float(p[0]), float(p[1])) for p in poly) if poly else None,
                tuple(d["layers"]) if d.get("layers") else None,
            )
        if kind == "remove_nearby_part":
            return RemoveNearbyPart(str(d["ref"]))
        if kind == "set_material":
            lt = d.get("loss_tangent")
            return SetMaterial(float(d["epsilon_r"]), float(lt) if lt is not None else None)
    except (KeyError, TypeError, ValueError) as e:
        raise WhatIfError("bad_operation", f"{kind}: {e}") from e
    raise WhatIfError("unknown_operation", f"unknown what-if operation {kind!r}; allowed: adjust_antenna_length, clear_copper_region, remove_nearby_part, set_material")


def material_of(ops) -> dict[str, float | None] | None:
    m = [o for o in ops or [] if isinstance(o, SetMaterial)]
    if not m:
        return None
    return {"epsilon_r": m[-1].epsilon_r, "loss_tangent": m[-1].loss_tangent}


# --------------------------------------------------------------------------- validation


def validate_ops(ops, board, setup: RfSetup) -> None:
    """Raise WhatIfError when an operation breaks a hard limit. Pure checks, no geometry edits."""
    from .geometry import collect_items, union_of

    seen_len = 0
    items = None
    for op in ops or []:
        if isinstance(op, AdjustAntennaLength):
            seen_len += 1
            if seen_len > 1:
                raise WhatIfError("duplicate_operation", "only one adjust_antenna_length per scenario")
            if setup.kind != "trace":
                raise WhatIfError("not_a_trace_antenna", "adjust_antenna_length applies to trace antennas only")
            if getattr(setup, "antenna_footprint", None):
                raise WhatIfError(
                    "footprint_antenna",
                    f"the radiating copper is the drawn shape of footprint {setup.antenna_footprint}, not tracks, so its length cannot be trimmed or extended here; "
                    "change the footprint (a different library antenna) instead",
                )
            if not math.isfinite(op.delta_mm) or abs(op.delta_mm) > MAX_LENGTH_DELTA_MM + 1e-9:
                raise WhatIfError("out_of_limits", f"adjust_antenna_length is limited to +-{MAX_LENGTH_DELTA_MM:g} mm, got {op.delta_mm}")
            if setup.open_end is None:
                raise WhatIfError("no_open_end", "the antenna has no detected open end to adjust")
        elif isinstance(op, ClearCopperRegion):
            poly = _region_polygon(op)
            items = items or collect_items(board, setup)
            if poly.area > MAX_CLEAR_AREA_MM2 + 1e-6:
                raise WhatIfError("out_of_limits", f"clear_copper_region is limited to {MAX_CLEAR_AREA_MM2:g} mm2, got {poly.area:.1f}")
            ant = union_of([i for i in items if i.antenna])
            if not ant.is_empty:
                if poly.distance(ant) > CLEAR_NEAR_MM:
                    raise WhatIfError("not_near_antenna", f"clear_copper_region must be within {CLEAR_NEAR_MM:g} mm of the antenna copper")
                if poly.intersects(ant):
                    raise WhatIfError("overlaps_antenna", "clear_copper_region may not remove antenna copper; use adjust_antenna_length")
            if poly.distance(Point(*setup.port_xy)) < 1.5:
                raise WhatIfError("overlaps_port", "clear_copper_region may not come within 1.5 mm of the feed port")
            x0, y0, x1, y1 = setup.region
            if not box(x0, y0, x1, y1).contains(poly):
                raise WhatIfError("outside_region", "clear_copper_region must lie inside the simulated region")
            if op.layers:
                names = {l.name for l in board.layers if l.kind == "copper"}
                bad = [l for l in op.layers if l not in names]
                if bad:
                    raise WhatIfError("bad_layer", f"unknown copper layer {bad[0]}")
        elif isinstance(op, RemoveNearbyPart):
            fp = board.footprint(op.ref)
            if fp is None:
                raise WhatIfError("unknown_part", f"no part {op.ref} on the board")
            x0, y0, x1, y1 = setup.region
            if not (x0 <= fp.x <= x1 and y0 <= fp.y <= y1):
                raise WhatIfError("outside_region", f"{op.ref} is outside the simulated region")
            ant_box = box(*setup.antenna_bbox)
            if Point(fp.x, fp.y).distance(ant_box) > REMOVE_NEAR_MM:
                raise WhatIfError("not_near_antenna", f"{op.ref} is more than {REMOVE_NEAR_MM:g} mm from the antenna")
            if op.ref == getattr(setup, "antenna_footprint", None):
                raise WhatIfError("antenna_part", f"{op.ref} is the antenna itself; removing it is not a what-if the model supports")
            matching = {e.ref for e in (setup.network.elements if setup.network else [])}
            port_ref = setup.port_on.split(":")[1] if setup.port_on and setup.port_on.count(":") >= 2 else None
            if op.ref in matching or op.ref == port_ref:
                raise WhatIfError("matching_part", f"{op.ref} is part of the matching network; use tune_matching instead")
        elif isinstance(op, SetMaterial):
            if not (EPS_RANGE[0] <= op.epsilon_r <= EPS_RANGE[1]):
                raise WhatIfError("out_of_limits", f"set_material permittivity must be {EPS_RANGE[0]} to {EPS_RANGE[1]}, got {op.epsilon_r}")
            if op.loss_tangent is not None and not (TAND_RANGE[0] <= op.loss_tangent <= TAND_RANGE[1]):
                raise WhatIfError("out_of_limits", f"set_material loss tangent must be {TAND_RANGE[0]} to {TAND_RANGE[1]}")
        else:
            raise WhatIfError("unknown_operation", f"not a what-if operation: {op!r}")


def _region_polygon(op: ClearCopperRegion):
    if op.bbox is not None:
        x0, y0, x1, y1 = op.bbox
        if x1 <= x0 or y1 <= y0:
            raise WhatIfError("bad_operation", "clear_copper_region bbox must be [x0, y0, x1, y1] with x1 > x0 and y1 > y0")
        return box(x0, y0, x1, y1)
    if op.polygon:
        p = Polygon(op.polygon)
        if not p.is_valid or p.area <= 0:
            raise WhatIfError("bad_operation", "clear_copper_region polygon is not a valid area")
        return p
    raise WhatIfError("bad_operation", "clear_copper_region needs a bbox or a polygon")


# --------------------------------------------------------------------------- application


def apply_to_items(items: list[Item], ops, board, setup: RfSetup) -> tuple[list[Item], list[str]]:
    notes: list[str] = []
    for op in ops:
        if isinstance(op, AdjustAntennaLength):
            items, n = _adjust_length(items, op.delta_mm, board, setup)
            notes.append(n)
        elif isinstance(op, RemoveNearbyPart):
            before = len(items)
            items = [i for i in items if not (i.kind in ("pad", "gfx") and i.ref == op.ref)]
            notes.append(f"removed the copper of {op.ref} ({before - len(items)} pad shapes)")
    return items, notes


def apply_to_layers(layers, antenna, ops, setup: RfSetup):
    notes: list[str] = []
    for op in ops:
        if isinstance(op, ClearCopperRegion):
            poly = _region_polygon(op)
            for name in list(layers):
                if op.layers is None or name in op.layers:
                    layers[name] = layers[name].difference(poly)
            notes.append(f"cleared copper in a {poly.area:.1f} mm2 region")
    return layers, antenna, notes


def _key(p, nd=3):
    return (round(p[0], nd), round(p[1], nd))


def _chain_from_open_end(ant_tracks: list[Item], open_end):
    """Order antenna track items from the open end back toward the feed."""
    ends = {}
    for it in ant_tracks:
        for p in (it.points[0], it.points[-1]):
            ends.setdefault(_key(p), []).append(it)
    start = min(ends, key=lambda k: math.dist(k, open_end))
    if math.dist(start, open_end) > 0.2:
        raise WhatIfError("no_open_end", "could not find the antenna open end in the copper")
    order, used, here = [], set(), start
    while True:
        nxt = [t for t in ends.get(here, []) if id(t) not in used]
        if not nxt:
            break
        t = nxt[0]
        used.add(id(t))
        pts = list(t.points)
        if _key(pts[0]) != here:
            pts = pts[::-1]
        order.append((t, pts))
        here = _key(pts[-1])
    return order


def _adjust_length(items: list[Item], delta: float, board, setup: RfSetup):
    from .geometry import board_outline_polygon, track_polygon, union_of

    if abs(delta) < 1e-9:
        return items, "antenna length unchanged"
    ant = [i for i in items if i.kind == "track" and i.antenna and i.layer == setup.antenna_layer and i.points]
    order = _chain_from_open_end(ant, setup.open_end)
    if not order:
        raise WhatIfError("no_open_end", "no antenna track chain to adjust")
    if delta < 0:
        remaining = -delta
        dropped: set[int] = set()
        replace: dict[int, Item] = {}
        for it, pts in order:
            ln = LineString(pts).length
            if remaining >= ln - 1e-9:
                dropped.add(id(it))
                remaining -= ln
                if remaining <= 1e-9:
                    break
                continue
            keep = LineString(pts[::-1])  # from the feed side toward the open end
            cut = _substring(keep, 0, keep.length - remaining)
            cut_pts = list(cut.coords)
            poly = track_polygon(cut_pts, it.width or 0.2)
            replace[id(it)] = Item("track", it.layer, it.net, poly, it.source, width=it.width, points=cut_pts, antenna=True)
            break
        else:
            raise WhatIfError("out_of_limits", "the antenna is shorter than the requested trim")
        out = []
        for i in items:
            if id(i) in dropped:
                continue
            out.append(replace.get(id(i), i))
        return out, f"shortened the antenna by {-delta:.2f} mm from its open end"
    # extend past the open end: along the last segment, else turned by 45 degree steps
    it, pts = order[0]
    (x1, y1), (x0, y0) = pts[0], pts[1] if len(pts) > 1 else (pts[0][0] - 1, pts[0][1])
    base = math.atan2(y1 - y0, x1 - x0)
    outline = board_outline_polygon(board).buffer(-0.3)
    others = union_of([i for i in items if i.layer == it.layer and not i.antenna and i.kind != "zone"])
    zones_other = union_of([i for i in items if i.layer == it.layer and i.kind == "zone" and i.net != it.net])
    own = union_of([i for i in items if i.antenna and i.layer == it.layer])
    reasons = []
    for turn in (0, 45, -45, 90, -90, 135, -135):
        ang = base + math.radians(turn)
        ex, ey = x1 + math.cos(ang) * delta, y1 + math.sin(ang) * delta
        new_pts = [(x1, y1), (ex, ey)]
        poly = track_polygon(new_pts, it.width or 0.2)
        fresh = poly.difference(own.buffer(0.05)).area / max(poly.area, 1e-9)
        if not outline.contains(poly):
            reasons.append("board edge")
        elif not others.is_empty and poly.distance(others) < MIN_EXTEND_CLEARANCE_MM:
            reasons.append("other copper")
        elif not zones_other.is_empty and poly.intersects(zones_other):
            reasons.append("a copper pour")
        elif fresh < 0.7:
            reasons.append("its own copper")
        else:
            ext = Item("track", it.layer, it.net, poly, it.source + "+ext", width=it.width, points=new_pts, antenna=True)
            how = "straight on" if turn == 0 else f"turned {turn} degrees"
            return items + [ext], f"lengthened the antenna by {delta:.2f} mm past its open end, {how}"
    raise WhatIfError("blocked", f"extending the antenna by {delta:.1f} mm in any direction from its open end runs into the board edge, other copper or the antenna itself")


def _substring(line: LineString, a: float, b: float) -> LineString:
    from shapely.ops import substring

    return substring(line, a, b)
