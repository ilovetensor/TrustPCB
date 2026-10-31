"""Scenario (the cache key) and job building.

``Scenario`` names what is simulated: the board version, the antenna, the band, the quality and
the what-if changes. Its content hash is the cache key. ``build_job`` turns board + setup +
scenario into a job directory holding ``scenario.json`` for the solver worker and ``meta.json``
with everything post-processing needs (mesh report, setup, y convention). It writes no KiCad file.

Board y is negated in the scenario (the worker flips nothing), so the structure is right-handed.
Field sidecars come back in scenario millimetres; ``meta.json`` says how to map them to board y.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from shapely.geometry import Point

from ..pcb.model import Board
from . import geometry as geo
from . import mesh as meshmod
from . import whatif
from .types import NotSimulatable, RfSetup

MODEL_VERSION = "rf-model-2"

RUN = {
    # end_criteria is energy decay in dB; n_freq points across the sweep
    "screen": {"end_criteria": -40.0, "max_timesteps": 150_000, "n_freq": 301},
    "confirm": {"end_criteria": -45.0, "max_timesteps": 300_000, "n_freq": 401},
}
MAX_THREADS = 6


@dataclass(frozen=True)
class Scenario:
    version_id: str
    antenna_id: str
    band: tuple[float, float]  # GHz
    quality: str = "screen"  # screen | confirm
    whatif_ops: tuple[Any, ...] = ()
    material: dict[str, float | None] | None = None
    region: tuple[float, float, float, float] | None = None  # override of the automatic region

    def __post_init__(self):
        if self.quality not in ("screen", "confirm"):
            raise ValueError("quality must be screen or confirm")

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": MODEL_VERSION,
            "version_id": self.version_id,
            "antenna_id": self.antenna_id,
            "band_ghz": [round(self.band[0], 6), round(self.band[1], 6)],
            "quality": self.quality,
            "whatif_ops": [whatif.op_to_dict(o) for o in self.whatif_ops],
            "material": self.material,
            "region": list(self.region) if self.region else None,
        }

    def hash(self) -> str:
        blob = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()[:20]

    def with_quality(self, quality: str) -> "Scenario":
        return Scenario(self.version_id, self.antenna_id, self.band, quality, self.whatif_ops, self.material, self.region)

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Scenario":
        ops = tuple(whatif.op_from_dict(o) for o in d.get("whatif_ops") or [])
        return Scenario(d["version_id"], d["antenna_id"], tuple(d["band_ghz"]), d.get("quality", "screen"), ops,
                        d.get("material") or whatif.material_of(ops), tuple(d["region"]) if d.get("region") else None)


def scenario_for(board: Board, setup: RfSetup, quality: str = "screen", ops=(), band=None, region=None) -> Scenario:
    ops = tuple(ops)
    return Scenario(board.version_id, setup.antenna_id, tuple(band or setup.band_ghz), quality, ops, whatif.material_of(ops),
                    tuple(region) if region else None)


# --------------------------------------------------------------------------- build


def _metal_name(layer: str) -> str:
    return layer.replace(".", "_")


def build_job(board: Board, setup: RfSetup, scenario: Scenario, job_dir: str | Path | None = None, *, threads: int = MAX_THREADS,
              debug_png: bool = True) -> Path:
    """Write scenario.json and meta.json into ``job_dir`` (a new temp directory when omitted)."""
    import tempfile

    jd = Path(job_dir) if job_dir else Path(tempfile.mkdtemp(prefix="trustpcb-job-"))
    jd.mkdir(parents=True, exist_ok=True)
    ops = list(scenario.whatif_ops)
    whatif.validate_ops(ops, board, setup)
    material = scenario.material or whatif.material_of(ops)
    run = RUN[scenario.quality]

    model = geo.build_model(board, setup, ops=ops, region=list(scenario.region) if scenario.region else None)
    eps_override = material["epsilon_r"] if material else None
    mesh = meshmod.build_mesh(model, setup, scenario.quality, eps_r=eps_override)

    zmap = setup.stackup["layer_z"]
    metals = []
    tri_counts = {}
    for layer, g in model.layers.items():
        tris = geo.triangulate(g)
        if not tris:
            continue
        tri_counts[layer] = len(tris)
        metals.append({"name": _metal_name(layer), "z_mm": zmap[layer],
                       "triangles": [[[x, -y] for x, y in t] for t in tris]})
    if setup.antenna_layer not in tri_counts:
        raise ValueError("no antenna copper left in the model")

    reg = model.region
    ob = model.outline.bounds
    sx0, sy0, sx1, sy1 = max(reg[0], ob[0]), max(reg[1], ob[1]), min(reg[2], ob[2]), min(reg[3], ob[3])
    substrates = []
    for s in setup.stackup["slabs"]:
        er = float(material["epsilon_r"]) if material else s["epsilon_r"]
        td = s["loss_tangent"]
        if material and material.get("loss_tangent") is not None:
            td = float(material["loss_tangent"])
        substrates.append({"name": s["name"].replace(" ", "_"), "zmin": s["zmin"], "zmax": s["zmax"], "epsilon_r": er,
                           "loss_tangent": td, "box": [sx0, -sy1, sx1, -sy0]})

    # vias snapped to mesh nodes; kept only when copper of both end layers lies under the node
    vias, dropped = [], 0
    xg, yg = np.asarray(mesh.x), np.asarray(mesh.y)
    seen = set()
    for v in model.vias:
        nx_ = float(xg[np.argmin(np.abs(xg - v["x"]))])
        ny_ = float(yg[np.argmin(np.abs(yg - (-v["y"])))])
        layers = [l for l in v["layers"] if l in zmap]
        if len(layers) < 2:
            continue
        zz = sorted(zmap[l] for l in layers)
        ok = all(model.layers[l].buffer(0.05).contains(Point(nx_, -ny_)) for l in (min(layers, key=zmap.get), max(layers, key=zmap.get))
                 if l in model.layers)
        if not ok:
            dropped += 1
            continue
        key = (round(nx_, 4), round(ny_, 4), round(zz[0], 4), round(zz[-1], 4))
        if key in seen:
            continue
        seen.add(key)
        vias.append({"x": round(nx_, 5), "y": round(ny_, 5), "diameter_mm": round(max(v["diameter"], 0.2), 4), "z0_mm": zz[0], "z1_mm": zz[-1]})

    px, py = setup.port_xy
    zg, zp = zmap[setup.ground_layer], zmap[setup.port_layer]
    pw, paxis = meshmod.effective_port_width(setup)
    if pw > 0:
        if paxis == "x":
            start, end = [px - pw / 2, -py, zg], [px + pw / 2, -py, zp]
        else:
            start, end = [px, -py - pw / 2, zg], [px, -py + pw / 2, zp]
    else:
        start, end = [px, -py, zg], [px, -py, zp]

    ztop = max(zmap.values())
    ab = setup.antenna_bbox
    cx, cy = (ab[0] + ab[2]) / 2, -(ab[1] + ab[3]) / 2
    sweep = setup.sweep_hz
    sc = {
        "name": f"trustpcb_{scenario.hash()}",
        "quality": scenario.quality,
        "f0_hz": setup.f0_hz,
        "band_hz": [sweep[0], sweep[1]],
        "fc_hz": setup.fc_hz,
        "unit_mm": 1,
        "mesh": {"x": mesh.x, "y": mesh.y, "z": mesh.z},
        "materials": [],
        "metals": metals,
        "vias": vias,
        "substrates": substrates,
        "port": {"start": start, "end": end, "impedance_ohm": setup.port_impedance_ohm},
        "boundary": "MUR",
        "dumps": {
            "field_xy": {"z_mm": ztop + 2.0},
            "field_xz": {"y_mm": -py},
            "pulse": {"z_mm": ztop + 2.0, "max_frames": 80},
            "current": {"metal": _metal_name(setup.antenna_layer)},
            "max_px": 256,
        },
        "pattern_center_mm": [cx, cy, ztop],
        "threads": max(1, min(int(threads), MAX_THREADS)),
        "end_criteria": run["end_criteria"],
        "max_timesteps": run["max_timesteps"],
        "n_freq": run["n_freq"],
    }
    (jd / "scenario.json").write_text(json.dumps(sc, separators=(",", ":")))
    meta = {
        "model": MODEL_VERSION,
        "scenario": scenario.to_dict(),
        "scenario_hash": scenario.hash(),
        "setup": setup.to_dict(),
        "mesh": mesh.report,
        "y_convention": {"scenario_y": "-board_y", "note": "sidecar v_mm and row order of y-planes are scenario y; board_y = -scenario_y"},
        "region": model.region,
        "fine_zone": model.fine_zone,
        "model_stats": {**model.stats, "triangles": tri_counts, "vias_in_scenario": len(vias), "vias_dropped_snap": dropped},
        "whatif_notes": model.notes,
        "material": material,
        "port": {"start": start, "end": end, "width_mm": pw, "requested_width_mm": setup.port_width_mm},
    }
    (jd / "meta.json").write_text(json.dumps(meta, indent=1, default=float))
    if debug_png:
        from .debug import render_debug_png

        render_debug_png(model, setup, mesh, jd / "model.png")
    return jd


def load_meta(job_dir: str | Path) -> dict[str, Any]:
    return json.loads((Path(job_dir) / "meta.json").read_text())
