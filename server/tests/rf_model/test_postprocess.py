from __future__ import annotations

import json
import shutil
from pathlib import Path

import numpy as np
import pytest

from trustpcb.rf import postprocess as P
from trustpcb.rf import scenario as S


def fake_result(trust_over=None, **over):
    res = {"f_res": 2.44e9, "s11_min_db": -12.0, "bandwidth": {}, "zin_at_res": {"re": 50, "im": 0}, "dmax_dbi": 2.0, "efficiency_est": 0.6,
           "trust": {"energy_decay_db": -41.0, "passive": True, "cells_per_wavelength": 22.0, "status": "ok", "reasons": []},
           "cells": 1, "timesteps": 1, "solve_s": 1.0, "nf2ff_s": 1.0}
    res.update(over)
    if trust_over:
        res["trust"].update(trust_over)
    return res


MESH = {"cells_per_wavelength_substrate": 22.0, "min_cells_across_feature": 2.1, "narrowest_feature_mm": 0.3, "narrowest_feature": "x"}


def test_trust_ok():
    t = P.trust_checks(fake_result(), {"mesh": MESH}, {"end_criteria": -40.0})
    assert t["status"] == "ok" and not t["reasons"] and len(t["checks"]) == 5


@pytest.mark.parametrize("over,check", [
    ({"energy_decay_db": -25.0}, "energy_decayed"),
    ({"passive": False}, "passive"),
    ({"cells_per_wavelength": 15.0}, "mesh_wavelength"),
    ({"reasons": ["port has copper on only one end (no ground reference)"]}, "port_on_copper_with_ground"),
])
def test_each_trust_check_can_fail(over, check):
    t = P.trust_checks(fake_result(trust_over=over), {"mesh": MESH}, {"end_criteria": -40.0})
    assert t["status"] == "fail" and any(r.startswith(check) for r in t["reasons"])


def test_mesh_across_fail():
    t = P.trust_checks(fake_result(), {"mesh": {**MESH, "min_cells_across_feature": 1.2}}, {"end_criteria": -40.0})
    assert t["status"] == "fail" and t["reasons"][0].startswith("mesh_across_feature")


def test_plausibility_flags():
    f = P.plausibility(fake_result(dmax_dbi=12.0, f_res=4.0e9, s11_min_db=-1.0, efficiency_est=1.4), (2.4e9, 2.48e9), {})
    assert len(f) == 4
    assert P.plausibility(fake_result(), (2.4e9, 2.48e9), {}) == []


def _run(worst, z, f1, dmax=2.0, dip=2.44e9):
    return {"sim": {"f_res": dip, "dmax_dbi": dmax, "first_series_resonance_hz": f1}, "s11": {"antenna_only": {"band_worst_db": worst}},
            "zin_at_band_centre": {"re": z.real, "im": z.imag}}


def test_compare_runs():
    a = _run(-1.4, 34 + 131j, 1.95e9, dip=3.06e9)
    r = P.compare_runs(a, _run(-1.1, 17 + 105j, 2.0e9, 2.1, dip=2.01e9))
    assert r["agree"] and r["deltas"]["deepest_dip_hz"] > 1e9  # the dip jumped modes, the comparison does not rely on it
    r = P.compare_runs(a, _run(-8.0, 34 + 131j, 1.95e9))
    assert not r["agree"] and "replaces" in r["verdict"]
    assert not P.compare_runs(a, _run(-1.4, 34 + 131j, 2.6e9))["agree"]


def test_finalize_writes_summary_with_as_built(tmp_path, nrf_board, nrf_setup):
    S.build_job(nrf_board, nrf_setup, S.scenario_for(nrf_board, nrf_setup, "screen"), tmp_path, debug_png=False)
    n = 101
    f = np.linspace(1.6e9, 3.3e9, n)
    g = np.full(n, 0.5 + 0.0j)
    (tmp_path / "s11.json").write_text(json.dumps({"freq_hz": f.tolist(), "s11_db": [-6.0] * n, "s11_re": g.real.tolist(), "s11_im": g.imag.tolist()}))
    (tmp_path / "result.json").write_text(json.dumps(fake_result(trust_over={"cells_per_wavelength": 21.79})))
    s = P.finalize(tmp_path)
    assert s["trust"]["status"] == "ok" and (tmp_path / "summary.json").exists() and (tmp_path / "s11_as_built.json").exists()
    assert s["s11"]["as_built"]["parts"][0]["kind"] == "L" and s["s11"]["antenna_only"]["band_worst_db"] == pytest.approx(-6.02, abs=0.05)
    assert "Simulated" in s["note"]


def test_finalize_error_result_fails_trust(tmp_path, nrf_board, nrf_setup):
    S.build_job(nrf_board, nrf_setup, S.scenario_for(nrf_board, nrf_setup, "screen"), tmp_path, debug_png=False)
    (tmp_path / "result.json").write_text(json.dumps({"error": "boom", "trust": {"status": "fail", "reasons": ["worker error: boom"]}}))
    s = P.finalize(tmp_path)
    assert s["trust"]["status"] == "fail" and s["error"] == "boom"


def test_board_plane_flips_y():
    out = P.board_plane({"v_axis": "y", "v_mm": [-67.0, 23.0]})
    assert out["v_mm_board"] == [-23.0, 67.0]
