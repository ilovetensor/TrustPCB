"""Small geometry helpers shared by the board model and the renderer.

Coordinates are KiCad board millimetres: x right, y down. KiCad angles are in degrees and a
positive angle turns the item counter-clockwise *as drawn on screen* (so with y down the
rotation matrix has the opposite sign from the usual maths one).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence

Point = tuple[float, float]
Ring = list[list[float]]

#: Maximum distance between a flattened arc and the true arc, in millimetres.
ARC_TOLERANCE = 0.01


def rotate(x: float, y: float, degrees: float) -> Point:
    """Rotate ``(x, y)`` about the origin by a KiCad angle."""
    if degrees == 0:
        return x, y
    a = math.radians(degrees)
    c, s = math.cos(a), math.sin(a)
    # Snap tiny values from exact right angles so ring coordinates stay clean.
    if degrees % 90 == 0:
        c, s = round(c), round(s)
    return x * c + y * s, -x * s + y * c


def circle_from_three(a: Point, b: Point, c: Point) -> tuple[Point, float] | None:
    """Centre and radius of the circle through three points, or ``None`` if collinear."""
    ax, ay = a
    bx, by = b
    cx, cy = c
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-12:
        return None
    ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay) + (cx * cx + cy * cy) * (ay - by)) / d
    uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx) + (cx * cx + cy * cy) * (bx - ax)) / d
    return (ux, uy), math.hypot(ax - ux, ay - uy)


def arc_points(start: Point, mid: Point, end: Point, tolerance: float = ARC_TOLERANCE) -> list[Point]:
    """Flatten a three-point arc into a polyline that includes both end points."""
    circle = circle_from_three(start, mid, end)
    if circle is None:
        return [start, end]
    (cx, cy), r = circle
    a0 = math.atan2(start[1] - cy, start[0] - cx)
    a1 = math.atan2(mid[1] - cy, mid[0] - cx)
    a2 = math.atan2(end[1] - cy, end[0] - cx)
    tau = 2 * math.pi
    # Sweep direction is the one that passes through ``mid``.
    d_mid = (a1 - a0) % tau
    d_end = (a2 - a0) % tau
    sweep = d_end if d_mid <= d_end else d_end - tau
    steps = _arc_steps(r, abs(sweep), tolerance)
    pts = [(cx + r * math.cos(a0 + sweep * i / steps), cy + r * math.sin(a0 + sweep * i / steps)) for i in range(steps + 1)]
    pts[0] = start
    pts[-1] = end
    return pts


def arc_length(start: Point, mid: Point, end: Point) -> float:
    circle = circle_from_three(start, mid, end)
    if circle is None:
        return math.dist(start, end)
    (cx, cy), r = circle
    a0 = math.atan2(start[1] - cy, start[0] - cx)
    a1 = math.atan2(mid[1] - cy, mid[0] - cx)
    a2 = math.atan2(end[1] - cy, end[0] - cx)
    tau = 2 * math.pi
    d_mid = (a1 - a0) % tau
    d_end = (a2 - a0) % tau
    sweep = d_end if d_mid <= d_end else tau - d_end
    return r * sweep


def _arc_steps(r: float, sweep: float, tolerance: float) -> int:
    if r <= tolerance:
        return max(2, int(sweep / (math.pi / 4)) + 1)
    step = 2 * math.acos(max(-1.0, min(1.0, 1 - tolerance / r)))
    return max(2, int(math.ceil(sweep / step)))


def circle_ring(cx: float, cy: float, r: float, tolerance: float = ARC_TOLERANCE, minimum: int = 16) -> Ring:
    steps = max(minimum, _arc_steps(r, 2 * math.pi, tolerance))
    return [[cx + r * math.cos(2 * math.pi * i / steps), cy + r * math.sin(2 * math.pi * i / steps)] for i in range(steps)]


def rect_ring(cx: float, cy: float, w: float, h: float, angle: float = 0.0) -> Ring:
    hw, hh = w / 2, h / 2
    out: Ring = []
    for px, py in ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)):
        rx, ry = rotate(px, py, angle)
        out.append([cx + rx, cy + ry])
    return out


def roundrect_ring(cx: float, cy: float, w: float, h: float, ratio: float, angle: float = 0.0) -> Ring:
    r = min(w, h) * ratio
    if r <= 1e-6:
        return rect_ring(cx, cy, w, h, angle)
    hw, hh = w / 2, h / 2
    steps = max(4, _arc_steps(r, math.pi / 2, 0.005))
    local: list[Point] = []
    # Corners in order: top-right, bottom-right, bottom-left, top-left (screen y down).
    for (ccx, ccy), start in (((hw - r, -hh + r), -90), ((hw - r, hh - r), 0), ((-hw + r, hh - r), 90), ((-hw + r, -hh + r), 180)):
        for i in range(steps + 1):
            a = math.radians(start + 90 * i / steps)
            local.append((ccx + r * math.cos(a), ccy + r * math.sin(a)))
    return [[cx + rx, cy + ry] for rx, ry in (rotate(px, py, angle) for px, py in local)]


def oval_ring(cx: float, cy: float, w: float, h: float, angle: float = 0.0) -> Ring:
    """A stadium: a rectangle with fully rounded short ends."""
    if abs(w - h) < 1e-9:
        return circle_ring(cx, cy, w / 2)
    r = min(w, h) / 2
    steps = max(8, _arc_steps(r, math.pi, 0.005))
    local: list[Point] = []
    if w > h:
        half = w / 2 - r
        for i in range(steps + 1):
            a = -math.pi / 2 + math.pi * i / steps
            local.append((half + r * math.cos(a), r * math.sin(a)))
        for i in range(steps + 1):
            a = math.pi / 2 + math.pi * i / steps
            local.append((-half + r * math.cos(a), r * math.sin(a)))
    else:
        half = h / 2 - r
        for i in range(steps + 1):
            a = math.pi * i / steps
            local.append((r * math.cos(a), half + r * math.sin(a)))
        for i in range(steps + 1):
            a = math.pi + math.pi * i / steps
            local.append((r * math.cos(a), -half + r * math.sin(a)))
    return [[cx + rx, cy + ry] for rx, ry in (rotate(px, py, angle) for px, py in local)]


def bbox_of(points: Iterable[Sequence[float]]) -> list[float] | None:
    xs: list[float] = []
    ys: list[float] = []
    for p in points:
        xs.append(p[0])
        ys.append(p[1])
    if not xs:
        return None
    return [min(xs), min(ys), max(xs), max(ys)]


def union_bbox(boxes: Iterable[Sequence[float] | None]) -> list[float] | None:
    out: list[float] | None = None
    for b in boxes:
        if b is None:
            continue
        if out is None:
            out = [b[0], b[1], b[2], b[3]]
        else:
            out[0] = min(out[0], b[0])
            out[1] = min(out[1], b[1])
            out[2] = max(out[2], b[2])
            out[3] = max(out[3], b[3])
    return out


def bbox_ring(b: Sequence[float]) -> Ring:
    return [[b[0], b[1]], [b[2], b[1]], [b[2], b[3]], [b[0], b[3]]]


def ring_area(ring: Sequence[Sequence[float]]) -> float:
    """Absolute polygon area (shoelace)."""
    total = 0.0
    n = len(ring)
    for i in range(n):
        x0, y0 = ring[i]
        x1, y1 = ring[(i + 1) % n]
        total += x0 * y1 - x1 * y0
    return abs(total) / 2


def ring_centroid(ring: Sequence[Sequence[float]]) -> Point:
    """Area centroid; falls back to the vertex mean for degenerate rings."""
    a = 0.0
    cx = cy = 0.0
    n = len(ring)
    for i in range(n):
        x0, y0 = ring[i]
        x1, y1 = ring[(i + 1) % n]
        cross = x0 * y1 - x1 * y0
        a += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    if abs(a) < 1e-12:
        return sum(p[0] for p in ring) / n, sum(p[1] for p in ring) / n
    return cx / (3 * a), cy / (3 * a)


def point_in_ring(x: float, y: float, ring: Sequence[Sequence[float]]) -> bool:
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def polyline_length(points: Sequence[Sequence[float]]) -> float:
    return sum(math.dist(points[i], points[i + 1]) for i in range(len(points) - 1))


def chain_rings(polylines: list[list[Point]], tolerance: float = 0.005) -> tuple[list[list[Point]], list[list[Point]]]:
    """Join open polylines end to end. Returns ``(closed_rings, unclosed_chains)``."""
    pending = [list(p) for p in polylines if len(p) >= 2]
    closed: list[list[Point]] = []
    open_chains: list[list[Point]] = []

    def close(a: Point, b: Point) -> bool:
        return abs(a[0] - b[0]) <= tolerance and abs(a[1] - b[1]) <= tolerance

    while pending:
        chain = pending.pop()
        progressed = True
        while progressed and not close(chain[0], chain[-1]):
            progressed = False
            for i, other in enumerate(pending):
                if close(chain[-1], other[0]):
                    chain.extend(other[1:])
                elif close(chain[-1], other[-1]):
                    chain.extend(reversed(other[:-1]))
                elif close(chain[0], other[-1]):
                    chain = other[:-1] + chain
                elif close(chain[0], other[0]):
                    chain = list(reversed(other[1:])) + chain
                else:
                    continue
                pending.pop(i)
                progressed = True
                break
        if len(chain) >= 4 and close(chain[0], chain[-1]):
            closed.append(chain[:-1])
        else:
            open_chains.append(chain)
    return closed, open_chains
