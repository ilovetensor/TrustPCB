"""connector_access: cable connectors whose mouth does not open onto the outside of the board.

In scope are receptacles a cable plugs into from one side: USB, RJ45, barrel and DC jacks, audio
jacks, HDMI and DisplayPort. Headers, test points, FPC and board-to-board parts, vertical
(top-entry) parts and plugs are left out.

The mating direction comes from the footprint's own geometry in board coordinates, so rotation and
back-side mirroring are already applied: the signal pads sit at the rear of the body, so the mouth
is on the side of the courtyard away from the pad centroid, snapped to the footprint's own axes.
Shield, mounting and unnumbered pads are not signal pads. When the pads do not clearly sit at one
end, or the footprint has no courtyard, no direction is claimed and nothing is emitted; the
``overlays`` data lists such connectors with the reason.

A ray from the middle of the mouth along that direction is cast to the Edge.Cuts outline (cut-outs
included). The distance to the first edge is the board lying in front of the mouth. A mouth on or
past the edge, or within ``connector_setback_mm`` of it, passes. The candidate also lists parts
whose courtyard or pads fall in a corridor as wide as the courtyard, from the mouth to that edge,
and the board edge nearest to the connector.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

import shapely
from shapely.geometry import Point, Polygon

from ..pcb import geom
from ..pcb.model import Board, Footprint
from .base import AnalyzerContext, Emitter, mark, meas, point_target, shortest_between
from .classify import footprint_ring, part_kind

RULE = "connector_access"

_KINDS = (
    ("usb", r"connector_usb|usb[\s_-]?(c|a|b|micro|mini|type|3|4)|type[\s_-]?c|(micro|mini)[\s_-]?usb"),
    ("rj45", r"connector_rj|rj[\s_-]?(45|11|12|9)|8p8c|magjack|modular[\s_-]?jack"),
    ("barrel", r"connector_barreljack|barrel|dc[\s_-]?jack|power[\s_-]?jack|dc[\s_-]?power[\s_-]?jack|pj[\s_-]?0\d\d"),
    ("audio", r"connector_audio|audio[\s_-]?jack|phone[\s_-]?jack|(^|[^a-z])trs([^a-z]|$)"),
    ("video", r"connector_hdmi|connector_displayport|hdmi|displayport"),
)
_KIND_RES = tuple((name, re.compile(rx, re.I)) for name, rx in _KINDS)
_EXCLUDE_RE = re.compile(
    r"header|pin[\s_-]?socket|testpoint|test[\s_-]?point|fpc|ffc|board[\s_-]?to[\s_-]?board|btb|vertical|(^|[^a-z])vert([^a-z]|$)|upright|top[\s_-]?entry|plug",
    re.I,
)
_NOT_SIGNAL_RE = re.compile(r"^(|~.*|sh(ell|ield)?\d*|s\d+|sld\d*|mp\d*|mh\d*|shld\d*)$", re.I)
_FLAT_KINDS = ("testpoint", "fiducial", "mounting")

#: pads must sit at one end: the body centre must be this fraction of the body length away from the pad centroid
_MIN_OFFSET = 0.15
#: ... and that offset must beat the offset along the other axis by this factor
_AXIS_MARGIN = 1.5
#: most blockers named in a candidate
_MAX_BLOCKERS = 8
#: overlap (mm2) below which a part only touches the corridor
_MIN_OVERLAP = 0.01


@dataclass
class Access:
    fp: Footprint
    kind: str
    status: str  # ok | blocked | undetermined
    reason: str = ""
    u: tuple[float, float] | None = None
    mouth: tuple[float, float] | None = None
    width: float = 0.0
    distance: float = 0.0
    hit: tuple[float, float] | None = None
    blockers: list[dict[str, Any]] = field(default_factory=list)
    nearest: dict[str, Any] | None = None


def connector_kind(fp: Footprint) -> str | None:
    """The cable family of a receptacle in scope, or None."""
    if fp.dnp or part_kind(fp) != "connector":
        return None
    hay = f"{fp.lib_id} {fp.value}"
    if _EXCLUDE_RE.search(hay):
        return None
    for name, rx in _KIND_RES:
        if rx.search(hay):
            return name
    return None


def _facing(u: tuple[float, float]) -> str:
    """The side of the drawing a direction points to; KiCad's y grows downward."""
    if abs(u[0]) >= abs(u[1]):
        return "right" if u[0] > 0 else "left"
    return "bottom" if u[1] > 0 else "top"


def _heading(u: tuple[float, float]) -> float:
    """Degrees clockwise on screen from the right-hand direction."""
    return math.degrees(math.atan2(u[1], u[0])) % 360.0


def _board_shape(board: Board):
    shapes = sorted((Polygon(r).buffer(0) for r in board.outline if len(r) >= 3), key=lambda p: p.area, reverse=True)
    if not shapes:
        return None
    shape = shapes[0]
    for s in shapes[1:]:
        shape = shape.difference(s) if shape.contains(s) else shape.union(s)
    return shape


def mating_direction(board: Board, fp: Footprint) -> tuple[tuple[float, float] | None, str]:
    """Unit vector from the signal pads toward the mouth, snapped to the footprint's axes, and a reason when unknown."""
    if not fp.courtyard:
        return None, "the footprint has no courtyard to compare the pads with"
    pads = [p for p in board.pads_of(fp.ref) if p.type != "np_thru_hole" and not _NOT_SIGNAL_RE.match(p.number or "")]
    centres = {(round(p.x, 2), round(p.y, 2)) for p in pads}
    if len(centres) < 2:
        return None, "fewer than two signal pads"
    ex, ey = geom.rotate(1.0, 0.0, fp.rotation), geom.rotate(0.0, 1.0, fp.rotation)

    def local(p) -> tuple[float, float]:
        dx, dy = p[0] - fp.x, p[1] - fp.y
        return dx * ex[0] + dy * ex[1], dx * ey[0] + dy * ey[1]

    ring = [local(p) for p in footprint_ring(fp)]
    lo = [min(p[i] for p in ring) for i in (0, 1)]
    hi = [max(p[i] for p in ring) for i in (0, 1)]
    pad_c = [sum(local(c)[i] for c in centres) / len(centres) for i in (0, 1)]
    score = []
    for i in (0, 1):
        length = hi[i] - lo[i]
        score.append(abs((lo[i] + hi[i]) / 2 - pad_c[i]) / length if length > 1e-6 else 0.0)
    axis = 0 if score[0] >= score[1] else 1
    if score[axis] < _MIN_OFFSET or score[axis] < _AXIS_MARGIN * score[1 - axis]:
        return None, "the signal pads do not sit clearly at one end of the body"
    sign = 1.0 if (lo[axis] + hi[axis]) / 2 > pad_c[axis] else -1.0
    base = ex if axis == 0 else ey
    return (sign * base[0], sign * base[1]), ""


def _along(shape, origin, u) -> float:
    pts = shapely.get_coordinates(shape)
    return float(min((x - origin[0]) * u[0] + (y - origin[1]) * u[1] for x, y in pts))


def _corridor(m, u, half_width, length) -> Polygon:
    v = (-u[1], u[0])
    a = (m[0] + v[0] * half_width, m[1] + v[1] * half_width)
    b = (m[0] - v[0] * half_width, m[1] - v[1] * half_width)
    return Polygon([a, b, (b[0] + u[0] * length, b[1] + u[1] * length), (a[0] + u[0] * length, a[1] + u[1] * length)])


def _blockers(board: Board, fp: Footprint, corridor: Polygon, m, u) -> list[dict[str, Any]]:
    cx0, cy0, cx1, cy1 = corridor.bounds
    out = []
    for other in board.footprints:
        if other.ref == fp.ref or part_kind(other) in _FLAT_KINDS:
            continue
        x0, y0, x1, y1 = other.bbox
        if x1 < cx0 or x0 > cx1 or y1 < cy0 or y0 > cy1:
            continue
        court = Polygon(footprint_ring(other)).buffer(0)
        hit_court = court.intersection(corridor)
        pad_hits = [h for h in (Polygon(p.ring).buffer(0).intersection(corridor) for p in board.pads_of(other.ref)) if h.area > _MIN_OVERLAP / 10]
        if hit_court.area <= _MIN_OVERLAP and not pad_hits:
            continue
        shapes = ([hit_court] if hit_court.area > _MIN_OVERLAP else []) + pad_hits
        near = min(_along(s, m, u) for s in shapes)
        out.append(
            {
                "ref": other.ref,
                "id": other.id,
                "side": other.side,
                "in_path": "pads and courtyard" if pad_hits else "courtyard",
                "from_mouth_mm": round(max(near, 0.0), 2),
            }
        )
    out.sort(key=lambda b: (b["from_mouth_mm"], b["ref"]))
    return out


def _nearest_edge(shape, fp: Footprint) -> dict[str, Any] | None:
    body = Polygon(footprint_ring(fp)).buffer(0)
    pa, pb, d = shortest_between(body, shape.boundary)
    vx, vy = pb[0] - pa[0], pb[1] - pa[1]
    if math.hypot(vx, vy) < 1e-6:
        c = body.centroid
        vx, vy = pb[0] - c.x, pb[1] - c.y
    n = math.hypot(vx, vy)
    if n < 1e-9:
        return None
    return {"side": _facing((vx / n, vy / n)), "gap_mm": round(d, 3), "toward": (vx / n, vy / n), "from": pa, "to": pb}


def evaluate(board: Board, ctx: AnalyzerContext) -> list[Access]:
    shape = _board_shape(board)
    if shape is None:
        return []
    limit = float(ctx.t("connector_setback_mm"))
    boundary = shape.boundary
    reach = math.hypot(board.bbox[2] - board.bbox[0], board.bbox[3] - board.bbox[1]) + 1.0
    results = []
    for fp in sorted(board.footprints, key=lambda f: f.ref):
        kind = connector_kind(fp)
        if kind is None:
            continue
        u, why = mating_direction(board, fp)
        if u is None:
            results.append(Access(fp, kind, "undetermined", why))
            continue
        ring = footprint_ring(fp)
        v = (-u[1], u[0])
        along = [(p[0] - fp.x) * u[0] + (p[1] - fp.y) * u[1] for p in ring]
        lat = [(p[0] - fp.x) * v[0] + (p[1] - fp.y) * v[1] for p in ring]
        width = max(lat) - min(lat)
        lat_mid = (max(lat) + min(lat)) / 2
        m = (fp.x + max(along) * u[0] + lat_mid * v[0], fp.y + max(along) * u[1] + lat_mid * v[1])
        acc = Access(fp, kind, "ok", u=u, mouth=m, width=width)
        acc.nearest = _nearest_edge(shape, fp)
        # a mouth on or past the edge sees no board in front of it
        if not shape.contains(Point(m)):
            results.append(acc)
            continue
        ray = shapely.LineString([m, (m[0] + u[0] * reach, m[1] + u[1] * reach)])
        pts = shapely.get_coordinates(ray.intersection(boundary))
        if len(pts) == 0:
            acc.status, acc.reason = "undetermined", "the line from the mouth does not meet the board outline"
            results.append(acc)
            continue
        dists = [math.hypot(x - m[0], y - m[1]) for x, y in pts]
        j = min(range(len(dists)), key=dists.__getitem__)
        acc.distance, acc.hit = dists[j], (float(pts[j][0]), float(pts[j][1]))
        if acc.distance > limit:
            acc.status = "blocked"
            acc.blockers = _blockers(board, fp, _corridor(m, u, width / 2, acc.distance), m, u)
        results.append(acc)
    return results


def analyze(board: Board, ctx: AnalyzerContext) -> list[dict[str, Any]]:
    limit = float(ctx.t("connector_setback_mm"))
    interior = float(ctx.t("connector_interior_mm"))
    em = Emitter(RULE)
    for acc in evaluate(board, ctx):
        if acc.status != "blocked":
            continue
        fp, d = acc.fp, acc.distance
        facing = _facing(acc.u)
        toward_interior = d > interior
        title = (
            f"{fp.ref} opens toward the board interior (facing {facing}); {d:.1f} mm of board lies in front of the mouth"
            if toward_interior
            else f"{fp.ref} mouth is set back {d:.1f} mm from the {facing} edge it faces (limit {limit:g} mm)"
        )
        details = [
            f"The signal pads are at the rear, so the mouth faces {facing} (heading {_heading(acc.u):.0f} deg; 0 is right and 90 is down the drawing), "
            f"centred at ({acc.mouth[0]:.2f}, {acc.mouth[1]:.2f}).",
            f"Along that line the first board edge is {d:.2f} mm from the mouth ({facing} edge), against a limit of {limit:g} mm.",
        ]
        marks = [
            mark("object", [fp.id], label="mouth not at an edge"),
            mark("link", [point_target(acc.mouth), point_target(acc.hit)], label="board in front of mouth", value=meas(d, "mm", 2)),
        ]
        m: dict[str, Any] = {
            "mouth_to_edge": meas(d, "mm", 2),
            "limit": meas(limit, "mm", 2),
            "corridor_width": meas(acc.width, "mm", 2),
            "blockers": meas(len(acc.blockers), "count"),
        }
        extra: dict[str, Any] = {}
        near = acc.nearest
        if near:
            turn = math.degrees(math.acos(max(-1.0, min(1.0, acc.u[0] * near["toward"][0] + acc.u[1] * near["toward"][1]))))
            m["nearest_edge_gap"] = meas(near["gap_mm"], "mm", 2)
            m["turn_to_nearest_edge"] = meas(turn, "deg", 0)
            details.append(
                f"The nearest board edge to the connector is the {near['side']} edge, {near['gap_mm']:.2f} mm from its courtyard; "
                f"the mouth would face it after a turn of {turn:.0f} deg."
            )
            extra["nearest_edge"] = {"side": near["side"], "gap_mm": near["gap_mm"], "turn_deg": round(turn, 1)}
        targets = [fp.id]
        if acc.blockers:
            shown = acc.blockers[:_MAX_BLOCKERS]
            targets += [b["id"] for b in shown]
            marks.append(mark("object", [b["id"] for b in shown], label="in the plug path"))
            listed = ", ".join(f"{b['ref']} ({b['in_path']}, {b['from_mouth_mm']:.1f} mm from the mouth)" for b in shown)
            more = f" and {len(acc.blockers) - len(shown)} more" if len(acc.blockers) > len(shown) else ""
            details.append(f"In a {acc.width:.1f} mm wide corridor in front of the mouth: {listed}{more}.")
        em.add(
            title,
            m,
            targets,
            marks,
            "high" if toward_interior else "medium",
            position=acc.mouth,
            details=details,
            ref=fp.ref,
            connector=acc.kind,
            facing=facing,
            heading_deg=round(_heading(acc.u), 1),
            mouth=[round(acc.mouth[0], 3), round(acc.mouth[1], 3)],
            edge_point=[round(acc.hit[0], 3), round(acc.hit[1], 3)],
            in_path=[{k: b[k] for k in ("ref", "side", "in_path", "from_mouth_mm")} for b in acc.blockers],
            kind="interior" if toward_interior else "setback",
            **extra,
        )
    return em.items


def overlays(board: Board, ctx: AnalyzerContext) -> dict[str, Any]:
    rows = []
    for acc in evaluate(board, ctx):
        row: dict[str, Any] = {"ref": acc.fp.ref, "connector": acc.kind, "status": acc.status}
        if acc.u is not None:
            row["facing"] = _facing(acc.u)
            row["mouth_to_edge_mm"] = round(acc.distance, 2)
        if acc.reason:
            row["reason"] = acc.reason
        rows.append(row)
    undetermined = [r["ref"] for r in rows if r["status"] == "undetermined"]
    if not rows:
        return {}
    return {
        "label": "Cable connectors checked for an outward-facing mouth",
        "connectors": rows,
        "note": (f"Direction could not be determined for {', '.join(undetermined)}; no claim was made about them." if undetermined else ""),
    }
