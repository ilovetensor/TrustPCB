"""Placement what-if on a ghost copy of one part (spec section 17). Pure geometry on a parsed `Board`.

`try_placement(board, ref, target, rotation, extra)` moves a temporary copy of the part, measures what that
would change and never writes anything: not the board file, not the database. The RF effect of the move is
never simulated; the answer says so.

Rotation is the part's new absolute rotation in KiCad degrees (counter-clockwise on screen, board y pointing down).
`target` is a part reference (the ghost is placed beside that part, on the free side nearest to where it is now)
or an `[x, y]` position for the part origin.
"""
from __future__ import annotations

import math
from typing import Any

from shapely import affinity
from shapely.geometry import Point, Polygon, box

EDGE_MIN_MM = 0.3        # a courtyard closer than this to the board edge is a conflict
GAP_MM = 0.25            # spacing used when placing a part beside another
NEAR_REPORT_MM = 8.0     # distances to other parts are listed within this reach
MAX_ROWS = 8


def _poly(ring) -> Polygon:
    p = Polygon(ring)
    return p if p.is_valid else p.buffer(0)


def _shape(fp) -> Polygon:
    if fp.courtyard and len(fp.courtyard) >= 3:
        return _poly(fp.courtyard)
    return box(*fp.bbox)


def _ring(geom) -> list[list[float]]:
    g = geom if geom.geom_type == 'Polygon' else max(getattr(geom, 'geoms', [geom]), key=lambda x: x.area)
    return [[round(x, 4), round(y, 4)] for x, y in list(g.exterior.coords)[:-1]]


def _outline(board):
    rings = [_poly(r) for r in board.outline if len(r) >= 3]
    if not rings:
        return None
    rings.sort(key=lambda r: -r.area)
    out = rings[0]
    for hole in rings[1:]:
        if out.contains(hole):
            out = out.difference(hole)
    return out


def _is_ground(name: str | None) -> bool:
    from ..rf.geometry import is_ground_name
    return bool(name) and is_ground_name(name)


def _parse_target(target: Any) -> tuple[str | None, tuple[float, float] | None]:
    if isinstance(target, (list, tuple)) and len(target) == 2 and all(isinstance(v, (int, float)) for v in target):
        return None, (float(target[0]), float(target[1]))
    if isinstance(target, dict) and 'x' in target and 'y' in target:
        return None, (float(target['x']), float(target['y']))
    if isinstance(target, str) and target.strip():
        return target.strip().removeprefix('ref:'), None
    raise ValueError('Give near (a part reference) or position [x, y].')


def try_placement(board, ref: str, target: Any, rotation: float | None = None, extra: dict | None = None) -> dict:
    fp = board.footprint(ref)
    if fp is None:
        raise ValueError(f'Part {ref} is not on this board.')
    near_ref, point = _parse_target(target)
    new_rot = fp.rotation if rotation is None else float(rotation)
    turn = -(new_rot - fp.rotation)                      # shapely turns counter-clockwise in a y-up frame; the board is y-down
    base = _shape(fp)
    pads = board.pads_of(ref)
    others = [f for f in board.footprints if f.ref != ref]
    through = {p.ref for p in board.pads if p.drill}
    side_layer = 'F.Cu' if fp.side == 'top' else 'B.Cu'
    outline = _outline(board)

    def ghost_at(x: float, y: float):
        g = affinity.rotate(base, turn, origin=(fp.x, fp.y))
        return affinity.translate(g, x - fp.x, y - fp.y)

    def evaluate(x: float, y: float) -> dict:
        g = ghost_at(x, y)
        overlaps, conflicts, rows = [], [], []
        for o in others:
            if o.side != fp.side and o.ref not in through and ref not in through:
                continue
            shape = _shape(o)
            gap = g.distance(shape)
            if g.intersects(shape):
                area = g.intersection(shape).area
                if area > 1e-4:
                    overlaps.append({'with': o.id, 'kind': 'courtyard', 'area_mm2': round(area, 3)})
                    conflicts.append(f'Courtyard overlaps {o.ref} by {area:.2f} mm2')
            if gap <= NEAR_REPORT_MM:
                rows.append({'to': o.id, 'distance_mm': round(gap, 3), 'was_mm': round(base.distance(shape), 3)})
        rows.sort(key=lambda r: r['distance_mm'])
        for k in board.keepouts:
            if k.layers and side_layer not in k.layers:
                continue
            kp = _poly(k.ring) if len(k.ring) >= 3 else None
            if kp is None or not g.intersects(kp):
                continue
            area = g.intersection(kp).area
            if area > 1e-4:
                forbids = k.rules.get('footprints') == 'not_allowed'
                overlaps.append({'with': k.id, 'kind': 'keepout', 'area_mm2': round(area, 3), 'blocks_parts': forbids})
                if forbids:
                    conflicts.append(f'Inside keepout {k.name or k.id}, which does not allow parts ({area:.2f} mm2)')
        edge = None
        if outline is not None:
            if not outline.buffer(1e-6).contains(g):
                conflicts.append('Courtyard goes outside the board outline')
                edge = 0.0
            else:
                edge = float(g.distance(outline.boundary))
                if edge < EDGE_MIN_MM:
                    conflicts.append(f'Courtyard is {edge:.2f} mm from the board edge (needs at least {EDGE_MIN_MM:g} mm)')
        for label, shape in (extra or {}).items():
            gap = g.distance(shape)
            rows.append({'to': label, 'distance_mm': round(gap, 3), 'was_mm': round(base.distance(shape), 3)})
            if g.intersects(shape) and area_of(g, shape) > 1e-4:
                overlaps.append({'with': label, 'kind': 'region', 'area_mm2': round(area_of(g, shape), 3)})
        rows.sort(key=lambda r: r['distance_mm'])
        return {'ghost': g, 'overlaps': overlaps, 'conflicts': conflicts, 'rows': rows, 'edge': edge,
                'score': (len(conflicts), sum(o['area_mm2'] for o in overlaps if o['kind'] != 'region'))}

    chosen = None
    if point is not None:
        x, y = point
    else:
        anchor = board.footprint(near_ref)
        if anchor is None:
            raise ValueError(f'Part {near_ref} is not on this board.')
        if anchor.ref == ref:
            raise ValueError('A part cannot be placed beside itself.')
        ashape = _shape(anchor)
        g0 = affinity.rotate(base, turn, origin=(fp.x, fp.y))
        gx0, gy0, gx1, gy1 = g0.bounds
        ax0, ay0, ax1, ay1 = ashape.bounds
        cx, cy = fp.x - gx0, fp.y - gy0                   # the origin relative to the ghost's bounds
        w, h = gx1 - gx0, gy1 - gy0
        candidates = []
        for side in ('right', 'left', 'below', 'above'):
            for align in (-1, 0, 1):
                if side in ('right', 'left'):
                    left = ax1 + GAP_MM if side == 'right' else ax0 - GAP_MM - w
                    top = (ay0 + ay1) / 2 - h / 2 + align * (ay1 - ay0 - h) / 2
                else:
                    top = ay1 + GAP_MM if side == 'below' else ay0 - GAP_MM - h
                    left = (ax0 + ax1) / 2 - w / 2 + align * (ax1 - ax0 - w) / 2
                candidates.append((left + cx, top + cy, side))
        best = None
        for cxn, cyn, side in candidates:
            ev = evaluate(cxn, cyn)
            key = (ev['score'], math.hypot(cxn - fp.x, cyn - fp.y))
            if best is None or key < best[0]:
                best = (key, cxn, cyn, side, ev)
        _, x, y, chosen, result = best
    if point is not None:
        result = evaluate(x, y)
    g = result['ghost']

    # a rough straight-line change of each signal connection of the part, pad centre to the nearest other pad on the net
    zone_nets = {z.net for z in board.zones if z.net and not z.is_keepout}
    moved = []
    for p in pads:
        px, py = _move_point(p.x, p.y, fp, x, y, turn)
        moved.append((p, px, py))
    rows, total = [], 0.0
    for p, px, py in moved:
        if not p.net or _is_ground(p.net) or p.net in zone_nets:
            continue
        mates = [q for q in board.pads if q.net == p.net and q.ref != ref]
        if not mates or len(mates) > 12:
            continue
        before = min(math.hypot(q.x - p.x, q.y - p.y) for q in mates)
        after = min(math.hypot(q.x - px, q.y - py) for q in mates)
        rows.append({'net': f'net:{p.net}', 'pad': p.id, 'was_mm': round(before, 2), 'now_mm': round(after, 2), 'change_mm': round(after - before, 2)})
        total += after - before
    rows.sort(key=lambda r: -abs(r['change_mm']))
    ghost = {'ref': ref, 'x': round(x, 4), 'y': round(y, 4), 'rotation': round(new_rot, 3), 'side': fp.side, 'ring': _ring(g),
             'pads': [{'id': p.id, 'x': round(px, 4), 'y': round(py, 4)} for p, px, py in moved][:40]}
    return {
        'ref': ref, 'target': target, 'placed': chosen and f'beside {near_ref}, {chosen} side' or 'at the given position',
        'from': [round(fp.x, 4), round(fp.y, 4)], 'new_position': [round(x, 4), round(y, 4)], 'rotation': round(new_rot, 3),
        'moved_mm': round(math.hypot(x - fp.x, y - fp.y), 3),
        'distances': result['rows'][:MAX_ROWS], 'edge_distance_mm': None if result['edge'] is None else round(result['edge'], 3),
        'overlaps': result['overlaps'], 'conflicts': result['conflicts'], 'ok': not result['conflicts'],
        'connections': rows[:MAX_ROWS], 'rough_length_change_mm': round(total, 2), 'ghost': ghost,
        'rf_effect': 'Not tested',
        'note': 'What-if on a ghost copy. Your board file is not changed. Lengths are straight lines between pad centres, not routed lengths. The RF effect of this move is Not tested.',
    }


def area_of(a, b) -> float:
    return a.intersection(b).area


def _move_point(px: float, py: float, fp, x: float, y: float, turn: float) -> tuple[float, float]:
    r = affinity.rotate(Point(px, py), turn, origin=(fp.x, fp.y))
    return r.x + (x - fp.x), r.y + (y - fp.y)
