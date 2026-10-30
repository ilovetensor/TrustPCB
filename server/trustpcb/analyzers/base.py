"""Analyzer context, candidate and mark helpers, and ``run_all``.

Analyzers measure; they never judge. Each module exposes ``analyze(board, ctx) -> list[dict]``
returning candidates in the shape of ``docs/specs/contracts.md``. A module may also expose
``overlays(board, ctx) -> dict`` (data for the UI) which ``run_all`` stores under the module name.

``run_all`` runs every analyzer in a fixed order. One analyzer failing never stops the others;
the status of each is reported. Marks are resolved against the board (``resolved``) and
candidates are registered on the board so ``candidate:<id>`` targets work.
"""

from __future__ import annotations

import importlib
import math
import time
import traceback
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from ..pcb import geom
from ..pcb.model import Board, Geometry, UnknownTarget

#: analyzers in the order of spec section 9; also the order of the output
ANALYZER_NAMES = (
    "zones",
    "noise_proximity",
    "track_jitter",
    "track_detour",
    "track_angle",
    "edge_track",
    "decoupling",
    "antenna_keepout",
    "rf_path",
    "return_path",
    "impedance",
    "via_count",
)

#: spec defaults (section 9); every one can be replaced through ``AnalyzerContext.thresholds``
DEFAULTS: dict[str, Any] = {
    # noise_proximity: below 10 mm
    "noise_distance_mm": 10.0,
    "noise_max_per_source": 3,
    # track_jitter: 3+ bends within 5x track width
    "jitter_min_bends": 3,
    "jitter_window_width_mult": 5.0,
    "jitter_min_bend_deg": 5.0,
    # track_detour: over 1.6x and 10 mm extra
    "detour_ratio": 1.6,
    "detour_extra_mm": 10.0,
    # track_angle: under 90 degrees
    "angle_max_deg": 89.5,
    # edge_track: below 0.5 mm; 5+ mm parallel run
    "edge_clearance_mm": 0.5,
    "edge_parallel_mm": 5.0,
    "edge_parallel_band_mm": 1.5,
    "edge_parallel_angle_deg": 15.0,
    # decoupling: over 3 mm or through a via
    "decoupling_max_mm": 3.0,
    "decoupling_via_max": 0,
    # antenna_keepout
    "antenna_clearance_mm": 2.0,
    # return_path
    "return_path_step_mm": 0.25,
    "return_path_min_gap_mm": 0.3,
    "return_path_merge_mm": 1.0,
    # impedance: more than 10 percent from target
    "impedance_tolerance_pct": 10.0,
    "impedance_min_section_mm": 1.0,
    # via_count: more than 2
    "via_max": 2,
    # zones
    "zone_cluster_mm": 8.0,
    "zone_pad_mm": 0.5,
    "zone_overlap_min_mm2": 1.0,
}


@dataclass
class AnalyzerContext:
    part_roles: dict[str, str] = field(default_factory=dict)
    fast_nets: list[str] = field(default_factory=list)
    target_ohms: float = 50.0
    version_dir: str | None = None
    #: overrides for DEFAULTS
    thresholds: dict[str, Any] = field(default_factory=dict)
    #: extra net lists the brief may know about
    rf_nets: list[str] = field(default_factory=list)
    power_nets: list[str] = field(default_factory=list)
    important_nets: list[str] = field(default_factory=list)
    #: per-net target impedance in ohms, and the differential target used for pair nets
    net_targets: dict[str, float] = field(default_factory=dict)
    diff_target_ohms: float = 90.0
    cache: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def t(self, key: str) -> Any:
        if key in self.thresholds:
            return self.thresholds[key]
        return DEFAULTS[key]

    def to_dict(self) -> dict[str, Any]:
        return {
            "part_roles": dict(self.part_roles),
            "fast_nets": list(self.fast_nets),
            "target_ohms": self.target_ohms,
            "version_dir": str(self.version_dir) if self.version_dir else None,
            "thresholds": {**DEFAULTS, **self.thresholds},
            "rf_nets": list(self.rf_nets),
            "power_nets": list(self.power_nets),
            "important_nets": list(self.important_nets),
            "net_targets": dict(self.net_targets),
            "diff_target_ohms": self.diff_target_ohms,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> AnalyzerContext:
        data = dict(data or {})
        keys = {"part_roles", "fast_nets", "target_ohms", "version_dir", "thresholds", "rf_nets", "power_nets", "important_nets", "net_targets", "diff_target_ohms"}
        return cls(**{k: v for k, v in data.items() if k in keys})


# --------------------------------------------------------------------------- helpers


def meas(value: float | int, unit: str, digits: int = 3) -> dict[str, Any]:
    return {"value": round(float(value), digits) if isinstance(value, float) else value, "unit": unit}


def mark(type_: str, targets: Iterable[str], label: str | None = None, value: dict[str, Any] | None = None, role: str | None = None) -> dict[str, Any]:
    m: dict[str, Any] = {"type": type_, "targets": list(dict.fromkeys(targets))}
    if label:
        m["label"] = label
    if value is not None:
        m["value"] = value
    if role:
        m["role"] = role
    return m


def point_target(p: Iterable[float]) -> str:
    x, y = list(p)[:2]
    return f"point:{round(x, 4):g},{round(y, 4):g}"


def bbox_target(box: Iterable[float]) -> str:
    return "bbox:" + ",".join(f"{round(v, 4):g}" for v in box)


class Emitter:
    """Numbers candidates ``candidate:<rule>:<n>`` in the order they are added."""

    def __init__(self, rule: str):
        self.rule = rule
        self.items: list[dict[str, Any]] = []

    def add(
        self,
        title: str,
        measurements: dict[str, dict[str, Any]],
        targets: Iterable[str],
        marks: list[dict[str, Any]],
        severity: str = "info",
        position: Iterable[float] | None = None,
        details: list[str] | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        cand: dict[str, Any] = {
            "id": f"candidate:{self.rule}:{len(self.items) + 1}",
            "rule": self.rule,
            "title": title,
            "source": "analyzer",
            "measurements": measurements,
            "targets": list(dict.fromkeys(targets)),
            "marks": marks,
            "severity_hint": severity,
        }
        if position is not None:
            cand["position"] = [round(float(v), 4) for v in list(position)[:2]]
        if details:
            cand["details"] = details
        cand.update(extra)
        self.items.append(cand)
        return cand


def resolve_all(board: Board, targets: list[str]) -> list[dict[str, Any]]:
    out = []
    for t in targets:
        try:
            out.append(board.resolve(t).to_dict())
        except UnknownTarget:
            continue
    return out


def finalize(board: Board, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Register each candidate on the board and give marks their ``resolved`` geometry."""
    for cand in candidates:
        board.register_candidate(cand["id"], cand["targets"])
        for m in cand["marks"]:
            if "resolved" not in m:
                m["resolved"] = resolve_all(board, m["targets"])
    return candidates


def polygon_geometry(ring_or_rings: list, target: str, layer: str | None = None) -> dict[str, Any]:
    """A ``resolved`` entry for a computed polygon (a hull) that no board id names."""
    rings = ring_or_rings if ring_or_rings and isinstance(ring_or_rings[0][0], (list, tuple)) else [ring_or_rings]
    box = geom.union_bbox(geom.bbox_of(r) for r in rings) or [0, 0, 0, 0]
    return Geometry("polygon", box, layer, rings=[[list(p) for p in r] for r in rings], center=[(box[0] + box[2]) / 2, (box[1] + box[3]) / 2], target=target).to_dict()


def dist_pt_seg(p: tuple[float, float], a: Iterable[float], b: Iterable[float]) -> tuple[float, tuple[float, float]]:
    """Distance from ``p`` to segment ``ab`` and the nearest point on it."""
    ax, ay = list(a)[:2]
    bx, by = list(b)[:2]
    dx, dy = bx - ax, by - ay
    ll = dx * dx + dy * dy
    s = 0.0 if ll < 1e-18 else max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / ll))
    q = (ax + s * dx, ay + s * dy)
    return math.hypot(p[0] - q[0], p[1] - q[1]), q


def shortest_between(a, b):
    """Shapely nearest points between two geometries as ((x, y), (x, y), distance)."""
    from shapely.ops import nearest_points

    pa, pb = nearest_points(a, b)
    return (pa.x, pa.y), (pb.x, pb.y), pa.distance(pb)


# ---------------------------------------------------------------------------- runner

AnalyzeFn = Callable[[Board, AnalyzerContext], list[dict[str, Any]]]


def _load(name: str):
    return importlib.import_module(f"{__package__}.{name}")


def run_all(board: Board, ctx: AnalyzerContext | None = None, only: Iterable[str] | None = None) -> dict[str, Any]:
    """Run the analyzers and return ``{candidates, overlays, status, seconds}``.

    ``status[name]`` is ``{status: ok|failed, seconds, count, error?, slow?}``. An analyzer that
    raises contributes no candidates and does not stop the next one.
    """
    ctx = ctx or AnalyzerContext()
    wanted = list(only) if only is not None else list(ANALYZER_NAMES)
    started = time.perf_counter()
    candidates: list[dict[str, Any]] = []
    overlays: dict[str, Any] = {}
    status: dict[str, Any] = {}
    for name in ANALYZER_NAMES:
        if name not in wanted:
            continue
        t0 = time.perf_counter()
        entry: dict[str, Any] = {"status": "ok", "count": 0}
        try:
            module = _load(name)
            found = module.analyze(board, ctx)
            finalize(board, found)
            candidates.extend(found)
            entry["count"] = len(found)
            if hasattr(module, "overlays"):
                data = module.overlays(board, ctx)
                if data:
                    overlays[name] = data
        except Exception as exc:  # one analyzer must never take the others down
            entry = {"status": "failed", "count": 0, "error": f"{type(exc).__name__}: {exc}", "trace": traceback.format_exc(limit=4)}
        entry["seconds"] = round(time.perf_counter() - t0, 3)
        if entry["seconds"] > 2.0:
            entry["slow"] = True
        status[name] = entry
    return {"candidates": candidates, "overlays": overlays, "status": status, "seconds": round(time.perf_counter() - started, 3)}
