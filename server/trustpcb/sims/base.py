"""Shared contract for board simulations: inputs, results, overlays and the run cache.

A simulator turns a parsed board plus labelled inputs into a ``SimResult``. Every number is simulated
or estimated and says so; every input is an ``Assumption`` with a source. A run's output folder holds
``result.json`` and one ``.bin`` file per overlay under ``<data>/sims/<hash>/``. The hash covers the
simulator kind, the board geometry, the inputs, the what-ifs and ``SIM_VERSION``, so identical runs
are served from the folder. What-ifs stay in simulation memory: nothing here edits a design.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

#: bump when a simulator changes its numbers or its output, so older cached runs are not reused
SIM_VERSION = "1"

RESULT_FILE = "result.json"


@dataclass
class Assumption:
    name: str
    value: float | str
    unit: str
    source: str  # 'datasheet' | 'user' | 'default' | 'board'
    cite: str | None = None  # datasheet url and page, or the reason


@dataclass
class Overlay:
    """A raster over the board, drawn by the web app. ``file`` is float32 little-endian [ny, nx]; NaN where nothing exists."""

    id: str
    label: str
    unit: str
    layer: str  # e.g. 'F.Cu', 'B.Cu', 'board'
    x0: float
    y0: float
    dx: float
    dy: float
    nx: int
    ny: int
    vmin: float
    vmax: float
    file: str
    scale: str = "sequential"  # 'sequential' | 'diverging'


@dataclass
class SimResult:
    kind: str
    status: str  # 'ok' | 'not_possible'
    reason: str | None = None
    metrics: dict[str, dict] = field(default_factory=dict)  # {name: {value, unit, label}}
    trust: dict = field(default_factory=lambda: {"status": "failed", "checks": []})  # {status: 'ok'|'failed', checks: [...]}
    overlays: list[Overlay] = field(default_factory=list)
    assumptions: list[Assumption] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    solve_s: float = 0.0
    #: simulator-specific detail (per-net tables and so on); plain JSON
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SimResult:
        return cls(
            kind=d["kind"],
            status=d["status"],
            reason=d.get("reason"),
            metrics=d.get("metrics", {}),
            trust=d.get("trust", {"status": "failed", "checks": []}),
            overlays=[Overlay(**o) for o in d.get("overlays", [])],
            assumptions=[Assumption(**a) for a in d.get("assumptions", [])],
            notes=list(d.get("notes", [])),
            solve_s=float(d.get("solve_s", 0.0)),
            detail=d.get("detail", {}),
        )

    @property
    def trusted(self) -> bool:
        """A result that fails its checks cannot support a finding."""
        return self.status == "ok" and self.trust.get("status") == "ok"


class Simulator:
    kind: str = ""  # 'power' | 'thermal' | 'signal'
    label: str = ""
    #: what-if operations this simulator accepts; any other op is refused
    whatif_ops: tuple[str, ...] = ()

    def available(self, board, ctx) -> tuple[bool, str]:
        raise NotImplementedError

    def inputs_needed(self, board, ctx) -> list[dict]:
        """What the simulator needs, with defaults and where each default comes from."""
        raise NotImplementedError

    def run(self, board, ctx, inputs: dict, whatif: list[dict], out_dir: Path) -> SimResult:
        raise NotImplementedError

    def check_whatif(self, whatif: list[dict]) -> str | None:
        """A reason to refuse these what-ifs, or None."""
        for op in whatif or []:
            name = op.get("op") if isinstance(op, dict) else None
            if name not in self.whatif_ops:
                return f"what-if {name!r} is not accepted by the {self.kind} simulator (accepts: {', '.join(self.whatif_ops) or 'none'})"
        return None


# ------------------------------------------------------------------ cache

def board_digest(board) -> str:
    """A short hash of the geometry that simulations read, so a changed board never reuses a run."""
    h = hashlib.sha256()

    def add(*parts: Any) -> None:
        h.update(json.dumps(parts, sort_keys=True, default=str, separators=(",", ":")).encode())

    add("v", getattr(board, "version_id", ""))
    add("stack", getattr(board, "stackup", {}), getattr(board, "thickness", None))
    for t in getattr(board, "tracks", []):
        add("t", t.net, t.layer, t.width, t.start, t.end, t.mid)
    for v in getattr(board, "vias", []):
        add("via", v.net, v.x, v.y, v.diameter, v.drill, v.layers)
    for p in getattr(board, "pads", []):
        add("p", p.id, p.net, round(p.x, 4), round(p.y, 4), p.w, p.h, p.drill, p.layers)
    for z in getattr(board, "zones", []):
        add("z", z.net, z.layers, {k: len(v) for k, v in z.filled.items()}, [sum(len(r) for r in v) for v in z.filled.values()])
        for rings in z.filled.values():
            for r in rings:
                add("zr", r[:4], len(r))
    return h.hexdigest()[:16]


def run_hash(kind: str, board, inputs: dict, whatif: list[dict]) -> str:
    payload = json.dumps(
        {"kind": kind, "board": board_digest(board), "inputs": inputs or {}, "whatif": whatif or [], "sim_version": SIM_VERSION},
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:20]


def run_dir(data_dir: str | Path, kind: str, board, inputs: dict, whatif: list[dict]) -> Path:
    return Path(data_dir) / "sims" / run_hash(kind, board, inputs, whatif)


def load_cached(out_dir: Path) -> SimResult | None:
    f = out_dir / RESULT_FILE
    try:
        res = SimResult.from_dict(json.loads(f.read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if any(not (out_dir / o.file).is_file() for o in res.overlays):
        return None
    return res


def save_result(out_dir: Path, result: SimResult) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out_dir / (RESULT_FILE + ".tmp")
    tmp.write_text(json.dumps(result.to_dict(), indent=1, default=_json_default), encoding="utf-8")
    tmp.replace(out_dir / RESULT_FILE)


def _json_default(o: Any) -> Any:
    try:
        import numpy as np

        if isinstance(o, np.generic):
            return o.item()
        if isinstance(o, np.ndarray):
            return o.tolist()
    except ImportError:  # pragma: no cover
        pass
    raise TypeError(f"not JSON serialisable: {type(o).__name__}")


def run_cached(sim: Simulator, board, ctx, inputs: dict | None, whatif: list[dict] | None, data_dir: str | Path) -> tuple[SimResult, Path, bool]:
    """Run ``sim`` or return the stored run. Returns ``(result, folder, from_cache)``.

    A refused what-if or an unavailable simulator gives a ``not_possible`` result and writes nothing.
    Blocking: callers on an event loop run this in a worker thread.
    """
    inputs = dict(inputs or {})
    whatif = list(whatif or [])
    refused = sim.check_whatif(whatif)
    if refused:
        return SimResult(sim.kind, "not_possible", refused), Path(data_dir) / "sims" / "refused", False
    ok, why = sim.available(board, ctx)
    if not ok:
        return SimResult(sim.kind, "not_possible", why), Path(data_dir) / "sims" / "unavailable", False
    out = run_dir(data_dir, sim.kind, board, inputs, whatif)
    cached = load_cached(out)
    if cached is not None:
        return cached, out, True
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    result = sim.run(board, ctx, inputs, whatif, out)
    if not result.solve_s:
        result.solve_s = round(time.perf_counter() - t0, 3)
    save_result(out, result)
    return result, out, False
