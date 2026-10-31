"""Placement what-if on a ghost copy of one part. Pure geometry on a parsed `Board`.

`try_placement(board, ref, target, rotation, extra)` moves a temporary copy of the part, measures what that
would change and never writes anything: not the board file, not the database. The RF effect of the move is
never simulated; the answer says so.

Rotation is the part's new absolute rotation in KiCad degrees (counter-clockwise on screen, board y pointing down).
`target` is a part reference (the ghost is placed beside that part, on the free side nearest to where it is now), a pad id such as
`pad:U3:E24` (beside that pad, as close as the courtyards allow), a net id such as `net:ANT_FEED` (beside the pad of that net nearest
to the part's current place), or an `[x, y]` position for the part origin.
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


def _parse_target(target: Any) -> tuple[str, Any]:
    """(kind, value): point (x, y), part (ref), pad (pad id) or net (net name)."""
    if isinstance(target, (list, tuple)) and len(target) == 2 and all(isinstance(v, (int, float)) for v in target):
        return 'point', (float(target[0]), float(target[1]))
    if isinstance(target, dict) and 'x' in target and 'y' in target:
        return 'point', (float(target['x']), float(target['y']))
    if isinstance(target, str) and target.strip():
        text = target.strip()
        if text.startswith('pad:'):
            return 'pad', text
        if text.startswith('net:'):
            return 'net', text.removeprefix('net:')
        return 'part', text.removeprefix('ref:')
    raise ValueError('Give near (a part reference, a pad id such as pad:U3:E24, or a net id) or position [x, y].')


def _find_pad(board, pad_id: str):
    return next((p for p in board.pads if p.id == pad_id), None)


def try_placement(board, ref: str, target: Any, rotation: float | None = None, extra: dict | None = None) -> dict:
    fp = board.footprint(ref)
    if fp is None:
        raise ValueError(f'Part {ref} is not on this board.')
    kind, value = _parse_target(target)
    point = value if kind == 'point' else None
    anchor_pad = None
    if kind == 'pad':
        anchor_pad = _find_pad(board, value)
        if anchor_pad is None:
            raise ValueError(f'Pad {value} is not on this board.')
    elif kind == 'net':
        mates = [p for p in board.pads if p.net == value and p.ref != ref]
        if not mates:
            raise ValueError(f'Net {value} has no pad on another part, so there is nothing to place the part beside.')
        anchor_pad = min(mates, key=lambda p: math.hypot(p.x - fp.x, p.y - fp.y))
    if anchor_pad is not None and anchor_pad.ref == ref:
        raise ValueError('A part cannot be placed beside its own pad.')
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
        if anchor_pad is not None:
            ashape = _poly(anchor_pad.ring) if anchor_pad.ring and len(anchor_pad.ring) >= 3 else box(anchor_pad.x - anchor_pad.w / 2, anchor_pad.y - anchor_pad.h / 2,
                                                                                                          anchor_pad.x + anchor_pad.w / 2, anchor_pad.y + anchor_pad.h / 2)
            offsets = [0.0] + [0.25 * k for k in range(1, 17)]       # a pad sits inside its part's courtyard: widen the gap until the courtyards clear
        else:
            anchor = board.footprint(value)
            if anchor is None:
                raise ValueError(f'Part {value} is not on this board.')
            if anchor.ref == ref:
                raise ValueError('A part cannot be placed beside itself.')
            ashape = _shape(anchor)
            offsets = [0.0]
        g0 = affinity.rotate(base, turn, origin=(fp.x, fp.y))
        gx0, gy0, gx1, gy1 = g0.bounds
        ax0, ay0, ax1, ay1 = ashape.bounds
        cx, cy = fp.x - gx0, fp.y - gy0                   # the origin relative to the ghost's bounds
        w, h = gx1 - gx0, gy1 - gy0
        candidates = []
        for offset in offsets:
            gap = GAP_MM + offset
            for side in ('right', 'left', 'below', 'above'):
                for align in (-1, 0, 1):
                    if side in ('right', 'left'):
                        left = ax1 + gap if side == 'right' else ax0 - gap - w
                        top = (ay0 + ay1) / 2 - h / 2 + align * (ay1 - ay0 - h) / 2
                    else:
                        top = ay1 + gap if side == 'below' else ay0 - gap - h
                        left = (ax0 + ax1) / 2 - w / 2 + align * (ax1 - ax0 - w) / 2
                    candidates.append((left + cx, top + cy, side, offset))
        best = None
        for cxn, cyn, side, offset in candidates:
            ev = evaluate(cxn, cyn)
            key = (ev['score'], offset, math.hypot(cxn - fp.x, cyn - fp.y))
            if best is None or key < best[0]:
                best = (key, cxn, cyn, side, ev)
        _, x, y, chosen, result = best
    if point is not None:
        result = evaluate(x, y)
    g = result['ghost']
    to_target = None
    if anchor_pad is not None:
        pad_shape = _poly(anchor_pad.ring) if anchor_pad.ring and len(anchor_pad.ring) >= 3 else box(anchor_pad.x - anchor_pad.w / 2, anchor_pad.y - anchor_pad.h / 2,
                                                                                                        anchor_pad.x + anchor_pad.w / 2, anchor_pad.y + anchor_pad.h / 2)
        to_target = {'to': anchor_pad.id, 'distance_mm': round(g.distance(pad_shape), 3), 'was_mm': round(base.distance(pad_shape), 3)}
        result['rows'] = sorted([to_target] + result['rows'], key=lambda r: r['distance_mm'])

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
        'ref': ref, 'target': target,
        'placed': (f'beside {anchor_pad.id}' + (f' (the pad of net:{value} nearest to {ref})' if kind == 'net' else '') + f', {chosen} side') if chosen and anchor_pad is not None
        else chosen and f'beside {value}, {chosen} side' or 'at the given position',
        **({'distance_to_target_mm': to_target['distance_mm'], 'target_pad': anchor_pad.id} if to_target else {}),
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
