"""Schema checks for the RF worker's processed output.

Fast tests validate a saved job directory (the exported patch fixture, or the
directory in TRUSTPCB_RF_JOB). The slow test runs the real worker in Docker on
a coarse patch scenario and validates what it writes.
"""
from __future__ import annotations

import json
import math
import os
import pathlib
import shutil
import subprocess
import sys

import numpy as np
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]
FIXTURE = ROOT / "web" / "public" / "fixtures" / "patch-sim"
JOB = pathlib.Path(os.environ.get("TRUSTPCB_RF_JOB", FIXTURE))
IMAGE = os.environ.get("TRUSTPCB_EMS_IMAGE", "trustpcb-ems:dev")
LIMIT = 30 * 1024 * 1024

FILES = ["progress.jsonl", "s11.json", "zin.json", "pattern.json", "field.json", "field_xy.bin",
         "field_xz.bin", "pulse.json", "pulse.bin", "current.json", "current.bin", "result.json"]


def load(job, name):
    return json.loads((job / name).read_text())


def f32(job, name, shape):
    raw = np.fromfile(job / name, dtype="<f4")
    assert raw.size == int(np.prod(shape)), f"{name}: {raw.size} floats, sidecar says {shape}"
    assert np.isfinite(raw).all(), f"{name} has non-finite values"
    return raw.reshape(shape)


def check_job(job: pathlib.Path):
    for n in FILES:
        assert (job / n).exists(), f"missing {n}"
    assert not list(job.glob("**/*.h5")) and not (job / "_sim").exists(), "raw solver files left behind"
    total = sum(p.stat().st_size for p in job.rglob("*") if p.is_file() and p.name != "scenario.json")
    assert total < LIMIT, f"processed output {total} bytes"

    res = load(job, "result.json")
    for k in ["f_res", "s11_min_db", "bandwidth", "zin_at_res", "dmax_dbi", "trust", "cells",
              "timesteps", "solve_s", "nf2ff_s"]:
        assert k in res, f"result.json lacks {k}"
    tr = res["trust"]
    for k in ["energy_decay_db", "passive", "cells_per_wavelength", "status", "reasons"]:
        assert k in tr, f"trust lacks {k}"
    assert tr["status"] in ("ok", "fail") and isinstance(tr["reasons"], list)
    assert (tr["status"] == "ok") == (not tr["reasons"])

    s = load(job, "s11.json")
    n = len(s["freq_hz"])
    assert n > 10 and all(len(s[k]) == n for k in ["s11_db", "s11_re", "s11_im"])
    assert all(b > a for a, b in zip(s["freq_hz"], s["freq_hz"][1:]))
    mag = 20 * np.log10(np.hypot(s["s11_re"], s["s11_im"]))
    assert np.allclose(mag, s["s11_db"], atol=0.02)
    assert abs(min(s["s11_db"]) - res["s11_min_db"]) < 0.01
    assert s["freq_hz"][int(np.argmin(s["s11_db"]))] == pytest.approx(res["f_res"], rel=1e-9)

    z = load(job, "zin.json")
    assert len(z["z_re"]) == len(z["z_im"]) == n

    p = load(job, "pattern.json")
    d = np.array(p["directivity_dbi"])
    assert d.shape == (len(p["theta_deg"]), len(p["phi_deg"])) == (37, 73)
    assert d.max() == pytest.approx(p["dmax_dbi"], abs=0.1)
    assert p["dmax_dbi"] == pytest.approx(res["dmax_dbi"], abs=0.01)
    if "pattern_f_hz" in res:        # runs from the band-centre worker: the far field, peak and efficiency share one frequency
        assert p["f_hz"] == res["pattern_f_hz"] == p["efficiency_f_hz"]
        lo, hi = s["freq_hz"][0], s["freq_hz"][-1]
        assert p["f_hz"] == (res["f0_hz"] if lo <= res["f0_hz"] <= hi else res["f_res"])

    fj = load(job, "field.json")
    for key in ("xy", "xz"):
        e = fj[key]
        a = f32(job, e["file"], e["shape"])
        assert e["shape"][0] == 2 and e["shape"][1] >= 2 and max(e["shape"][1:]) <= 256
        assert e["scale"] > 0 and np.abs(a).max() > 0.5
        for ax in ("u_mm", "v_mm"):
            assert e[ax][1] > e[ax][0]

    pj = load(job, "pulse.json")
    a = f32(job, pj["file"], pj["shape"])
    assert 1 <= pj["shape"][0] <= 80 and max(pj["shape"][1:]) <= 256
    assert len(pj["times_s"]) == pj["shape"][0] and a.min() >= 0 and a.max() == pytest.approx(1.0, abs=0.01)

    cj = load(job, "current.json")
    a = f32(job, cj["file"], cj["shape"])
    assert len(cj["shape"]) == 2 and a.min() >= 0 and a.max() > 0.5

    lines = [json.loads(x) for x in (job / "progress.jsonl").read_text().splitlines()]
    assert lines and lines[-1]["phase"] == "done"
    ts = [x["timestep"] for x in lines if x.get("phase") == "solve"]
    assert ts and ts == sorted(ts)
    return res


def test_saved_job_schema():
    if not JOB.exists():
        pytest.skip(f"no saved job at {JOB}")
    check_job(JOB)


def test_patch_scenario_maker(tmp_path):
    subprocess.run([sys.executable, str(ROOT / "worker" / "make_patch_scenario.py"), str(tmp_path),
                    "--mesh-mm", "6"], check=True, capture_output=True)
    sc = json.loads((tmp_path / "scenario.json").read_text())
    assert sc["port"]["impedance_ohm"] == 50 and len(sc["metals"]) == 2
    for ax in "xyz":
        m = sc["mesh"][ax]
        assert all(b > a for a, b in zip(m, m[1:]))
        assert max(b - a for a, b in zip(m, m[1:])) <= 6.0 + 1e-9


@pytest.mark.slow
def test_worker_in_docker(tmp_path):
    if shutil.which("docker") is None:
        pytest.skip("docker not available")
    if subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True).returncode:
        pytest.skip(f"image {IMAGE} not built")
    job = tmp_path / "job"
    subprocess.run([sys.executable, str(ROOT / "worker" / "make_patch_scenario.py"), str(job),
                    "--mesh-mm", "3.6", "--threads", "4"], check=True, capture_output=True)
    r = subprocess.run(["docker", "run", "--rm", "--cpus", "4", "--memory", "6g", "-v", f"{job}:/job",
                        IMAGE, "python3", "/app/run_scenario.py", "/job"], capture_output=True, text=True,
                       timeout=900)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    res = check_job(job)
    assert 1.5e9 < res["f_res"] < 3.5e9
