"""Radiating sections: the part of a board that is meant to radiate and must not be judged as a transmission line.

A printed antenna is intentionally long, narrow, bent and free of ground. Rules that look for
detours, impedance steps or missing return copper would flag exactly the features that make the
antenna work, and a recommendation to put ground under it would detune it. This module finds
what to leave out:

  * the board's antenna keep-outs (copper keep-out zones named for an antenna, or touching antenna copper);
  * tracks whose midpoint lies inside one of those keep-outs on a layer it covers;
  * the detected radiating element (``intake.detect`` antenna data) when the board has no keep-out for it.

Only the transmission-line section between the radio and the start of the antenna remains for the
other rules. Results are cached on the analyzer context.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import shapely
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union

from ..intake.detect import detect
from ..pcb.model import Board
from .base import AnalyzerContext

_NAME_RE = re.compile(r"ant|rf|keep.?clear|keep.?out", re.I)
_COPPER_RULES = ("tracks", "copperpour", "pads", "vias")
FEED_EXEMPT_MM = 3.0  # no keep-out: copper this close to the feed point is still the feed line


@dataclass
class Radiating:
    keepouts: list[tuple[Polygon, list[str]]] = field(default_factory=list)  # (polygon, layers; empty = all)
    track_ids: frozenset[str] = frozenset()
    geometry: Any = None  # union of the keep-out polygons, prepared

    def __bool__(self) -> bool:
        return bool(self.track_ids) or self.geometry is not None

    def contains_xy(self, xs: np.ndarray, ys: np.ndarray, layer: str | None = None) -> np.ndarray:
        """True where a point lies inside an antenna keep-out that covers ``layer``."""
        out = np.zeros(len(xs), dtype=bool)
        for poly, layers in self.keepouts:
            if layer is None or not layers or layer in layers:
                out |= shapely.contains_xy(poly, xs, ys)
        return out


def _track_line(t) -> LineString:
    return LineString(t.points())


def radiating(board: Board, ctx: AnalyzerContext | None = None) -> Radiating:
    cache = ctx.cache if ctx is not None else None
    if cache is not None and cache.get("radiating_board") is board:
        return cache["radiating"]
    result = _find(board)
    if cache is not None:
        cache["radiating"], cache["radiating_board"] = result, board
    return result


def _find(board: Board) -> Radiating:
    try:
        antenna = (detect(board).antenna or {}) if board.keepouts or board.tracks else {}
    except Exception:  # detection is best effort; the rules then run as before
        antenna = {}
    ant_ids = set(antenna.get("track_ids") or [])
    tracks = {t.id: t for t in board.tracks}
    ant_lines = [_track_line(tracks[i]).buffer(tracks[i].width / 2) for i in ant_ids if i in tracks]
    ant_union = unary_union(ant_lines) if ant_lines else None
    keepouts: list[tuple[Polygon, list[str]]] = []
    for k in sorted(board.keepouts, key=lambda k: k.id):
        if not any(k.rules.get(r) == "not_allowed" for r in _COPPER_RULES):
            continue
        poly = Polygon(k.ring).buffer(0)
        if poly.is_empty:
            continue
        if _NAME_RE.search(k.name or "") or (ant_union is not None and poly.intersects(ant_union)):
            keepouts.append((poly, list(k.layers)))
    ids: set[str] = set()
    for t in board.tracks:
        mid = _track_line(t).interpolate(0.5, normalized=True)
        for poly, layers in keepouts:
            if (not layers or t.layer in layers) and poly.contains(mid):
                ids.add(t.id)
                break
    feed = antenna.get("feed_point")
    if ant_ids and feed is not None and not (ant_ids & ids):
        # the board has no keep-out around its antenna: use the detected element, minus the feed line
        # (without a known feed point the feed cannot be told from the element, so nothing is left out)
        for i in ant_ids:
            t = tracks.get(i)
            if t is not None and _track_line(t).distance(Point(feed)) > FEED_EXEMPT_MM:
                ids.add(i)
    geometry = unary_union([p for p, _ in keepouts]) if keepouts else None
    if geometry is not None:
        shapely.prepare(geometry)
    return Radiating(keepouts, frozenset(ids), geometry)
