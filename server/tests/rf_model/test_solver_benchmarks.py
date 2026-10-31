"""The solver, port, mesh and boundary on a textbook antenna, with no board in between. Slow (about 10 minutes); needs the trustpcb-ems:dev image.

A probe-fed rectangular patch on FR-4 is sized with the Hammerstad cavity formulas for 2.45 GHz and simulated through the same worker and the
product's mesh grading. Measured on this model: the cavity (peak of the input resistance) sits 4.3 % below the formula and the S11 dip 3.7 % below,
the ordinary spread of a closed-form estimate against a full-wave solution. A bug that moves a printed antenna by 10 percent or more (a wrong
dielectric, a port that adds inductance, a boundary too close) shows up here. The same patch on a plain 1.2 mm mesh without the 0.3 mm cells at
the copper edges came out 7.6 % low, so the check also guards the mesh rules.
"""
from __future__ import annotations

import json
import math
import os
import shutil
import subprocess

import numpy as np
import pytest

from trustpcb.rf.mesh import _grade
from trustpcb.rf.queue import WORKER_DIR, docker_run_command
from trustpcb.rf.types import C0

pytestmark = pytest.mark.slow

F0 = 2.45e9


def _have_image():
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "image", "inspect", "trustpcb-ems:dev"], capture_output=True).returncode == 0


def hammerstad(f: float, h_mm: float, er: float) -> tuple[float, float]:
    """Width and length (mm) of a patch resonant at f: Hammerstad effective permittivity and fringing extension."""
    w = C0 / (2 * f) * math.sqrt(2 / (er + 1)) * 1e3
    eeff = (er + 1) / 2 + (er - 1) / 2 / math.sqrt(1 + 12 * h_mm / w)
    dl = 0.412 * h_mm * (eeff + 0.3) * (w / h_mm + 0.264) / ((eeff - 0.258) * (w / h_mm + 0.8))
    return w, C0 / (2 * f * math.sqrt(eeff)) * 1e3 - 2 * dl


def _rect(x0, y0, x1, y1):
    return [[[x0, y0], [x1, y0], [x1, y1]], [[x0, y0], [x1, y1], [x0, y1]]]


def _scenario(h=1.6, er=4.4, gnd=30.0, y0=8.0):
    w, length = hammerstad(F0, h, er)
    lam0 = C0 / F0 * 1e3
    pad = 0.20 * lam0
    w2, l2 = w / 2, length / 2
    yp = round(-l2 + y0, 4)   # the port sits exactly on a mesh line: a port between lines is not excited

    def axis(edges):
        """The product's mesh grading: cells of 0.3 mm at copper edges growing to 1.2 mm over the board and 4 mm in the air margin."""
        fixed = [-gnd, gnd, -gnd - pad, gnd + pad, *edges]
        return _grade(sorted(set(fixed)), [(e, 0.3) for e in edges], lambda v: 1.2 if abs(v) <= gnd else min(lam0 / 20, 4.0), set(), 1.3)

    x = axis([-w2, w2, 0.0])
    y = axis([-l2, l2, yp])
    z = _grade(sorted({0.0, h, -pad, h + pad}), [(0.0, 0.45), (h, 0.45)], lambda v: 0.45 if 0 <= v <= h else min(lam0 / 20, 4.0), set(), 1.3)
    return {
        "name": "bench_patch", "quality": "screen", "f0_hz": F0, "band_hz": [1.6e9, 3.4e9], "fc_hz": 0.8e9, "unit_mm": 1,
        "mesh": {"x": x, "y": y, "z": z}, "materials": [],
        "metals": [{"name": "gnd", "z_mm": 0.0, "triangles": _rect(-gnd, -gnd, gnd, gnd)}, {"name": "patch", "z_mm": h, "triangles": _rect(-w2, -l2, w2, l2)}],
        "vias": [], "substrates": [{"name": "fr4", "zmin": 0.0, "zmax": h, "epsilon_r": er, "loss_tangent": 0.02, "box": [-gnd, -gnd, gnd, gnd]}],
        "port": {"start": [0, yp, 0], "end": [0, yp, h], "impedance_ohm": 50.0, "axis": "z"}, "boundary": "MUR",
        "dumps": {"field_xy": {"z_mm": h + 2.0}, "field_xz": {"y_mm": yp}, "pulse": {"z_mm": h + 2.0, "max_frames": 4}, "current": {"metal": "patch"}, "max_px": 32},
        "pattern_center_mm": [0, 0, h], "threads": 4, "end_criteria": -40.0, "max_timesteps": 150000, "n_freq": 601,
    }


@pytest.mark.skipif(not _have_image(), reason="docker image trustpcb-ems:dev not built")
def test_probe_fed_patch_resonates_where_the_cavity_formula_puts_it(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    (job / "scenario.json").write_text(json.dumps(_scenario()))
    cmd = docker_run_command("docker", f"trustpcb-bench-patch-{os.getpid()}", "trustpcb-ems:dev", job, cpus=4, memory="6g", worker_dir=WORKER_DIR)
    assert subprocess.run(cmd, capture_output=True, text=True, timeout=1500).returncode == 0
    zin = json.loads((job / "zin.json").read_text())
    s11 = json.loads((job / "s11.json").read_text())
    f = np.array(zin["freq_hz"])
    z = np.array(zin["z_re"]) + 1j * np.array(zin["z_im"])
    f_cavity = f[int(np.argmax(z.real))]
    f_dip = f[int(np.argmin(s11["s11_db"]))]
    # 4.3 % and 3.7 % low when measured; the window is -8 % to +3 %
    assert 0.92 * F0 < f_cavity < 1.03 * F0, f_cavity
    assert 0.92 * F0 < f_dip < 1.03 * F0, f_dip
    assert min(s11["s11_db"]) < -10
