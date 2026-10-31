from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import numpy as np
import pytest

from trustpcb.rf import scenario as S
from trustpcb.rf import whatif as W

ROOT = Path(__file__).resolve().parents[3]


def _worker():
    spec = importlib.util.spec_from_file_location("run_scenario", ROOT / "worker" / "run_scenario.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_hash_is_stable_and_sensitive(nrf_board, nrf_setup):
    a = S.scenario_for(nrf_board, nrf_setup, "screen")
    assert a.hash() == S.scenario_for(nrf_board, nrf_setup, "screen").hash()
    assert a.hash() != S.scenario_for(nrf_board, nrf_setup, "confirm").hash()
    b = S.scenario_for(nrf_board, nrf_setup, "screen", [W.AdjustAntennaLength(-1.0)])
    assert b.hash() != a.hash()
    assert S.Scenario.from_dict(b.to_dict()).hash() == b.hash()


def test_the_cache_key_follows_the_geometry_and_not_the_version_or_the_matching_parts(nrf_board, nrf_setup, nrf_v2_board, nrf_v2_setup):
    from dataclasses import replace

    a = S.scenario_for(nrf_board, nrf_setup, "confirm")
    b = S.scenario_for(nrf_v2_board, nrf_v2_setup, "confirm")           # another version id, other matching parts, the same copper
    assert nrf_board.version_id != nrf_v2_board.version_id and a.hash() == b.hash() and a.geometry_key == b.geometry_key
    assert a.legacy_hash() != b.legacy_hash()                            # the old key held the version id
    assert replace(a, version_id="anything").hash() == a.hash()
    assert S.Scenario.from_dict(a.to_dict()).hash() == a.hash()
    # a change of copper changes the key
    moved = S.scenario_for(nrf_board, nrf_setup, "confirm", [W.AdjustAntennaLength(-1.0)])
    assert moved.hash() != a.hash()
    nrf_board.tracks[0].width += 0.1
    try:
        assert S.geometry_key(nrf_board, nrf_setup) != a.geometry_key
    finally:
        nrf_board.tracks[0].width -= 0.1
    assert S.geometry_key(nrf_board, nrf_setup) == a.geometry_key


@pytest.fixture(scope="module")
def nrf_job(tmp_path_factory, nrf_board, nrf_setup):
    d = tmp_path_factory.mktemp("job")
    S.build_job(nrf_board, nrf_setup, S.scenario_for(nrf_board, nrf_setup, "screen"), d)
    return d


def test_job_files_and_y_flip(nrf_job, nrf_setup):
    sc = json.loads((nrf_job / "scenario.json").read_text())
    meta = S.load_meta(nrf_job)
    assert (nrf_job / "model.png").exists()
    ys = [p[1] for m in sc["metals"] for t in m["triangles"] for p in t]
    assert max(ys) <= 0.01 and min(ys) < -40  # board y is negated
    assert sc["port"]["start"][1] == pytest.approx(-nrf_setup.port_xy[1])
    assert sc["port"]["start"][2] == 0 and sc["port"]["end"][2] == pytest.approx(1.565)
    assert sc["threads"] <= 6
    assert meta["y_convention"]["scenario_y"] == "-board_y"


def test_the_sweep_has_801_points_so_dips_are_not_on_a_coarse_grid(nrf_job, nrf_setup):
    sc = json.loads((nrf_job / "scenario.json").read_text())
    assert sc["n_freq"] == 801 and S.RUN["screen"]["n_freq"] == 801 and S.RUN["confirm"]["n_freq"] == 801
    lo, hi = nrf_setup.sweep_hz
    assert (hi - lo) / (sc["n_freq"] - 1) < 2.5e6               # 301 points left a 5.7 MHz grid on the same sweep
    assert S.MODEL_VERSION == "rf-model-4"                        # the old cache keys (rf-model-3 and earlier) are not reused: the far field moved to the band centre


def test_port_on_real_copper_both_ends(nrf_job):
    w = _worker()
    sc = json.loads((nrf_job / "scenario.json").read_text())
    assert w.point_on_metal(sc, *sc["port"]["start"]) and w.point_on_metal(sc, *sc["port"]["end"])


def test_patch_strip_port_on_copper(tmp_path, patch_board, patch_setup):
    S.build_job(patch_board, patch_setup, S.scenario_for(patch_board, patch_setup, "screen"), tmp_path, debug_png=False)
    sc = json.loads((tmp_path / "scenario.json").read_text())
    w = _worker()
    a, b = np.array(sc["port"]["start"]), np.array(sc["port"]["end"])
    assert int(np.argmax(np.abs(b - a))) == 2 and sc["port"]["axis"] == "z"
    assert w.port_axis(sc["port"], a, b) == 2                                 # explicit axis
    assert w.port_axis({}, [0, 0, 0], [0.2, 0, 1.5]) == 2                     # older files: longest side
    assert w.port_axis({"axis": "x"}, [0, 0, 0], [0.2, 0, 1.5]) == 0
    with pytest.raises(ValueError):
        w.port_axis({"axis": "q"}, a, b)
    assert w.point_on_metal(sc, *a) and w.point_on_metal(sc, *b)


def test_mesh_rules(nrf_job):
    sc = json.loads((nrf_job / "scenario.json").read_text())
    rep = S.load_meta(nrf_job)["mesh"]
    for ax in "xyz":
        d = np.diff(sc["mesh"][ax])
        assert (d > 0).all()
    assert rep["rule_ok"] and rep["cells_per_wavelength_substrate"] >= 20 and rep["min_cells_across_feature"] >= 2
    assert rep["max_growth_ratio"] < 1.6
    assert rep["cells"] < 2_000_000
    # the worker's own cells-per-wavelength rule agrees
    assert _worker().cells_per_wavelength(sc, sc["band_hz"][1]) >= 20


def test_confirm_is_finer_than_screen(nrf_board, nrf_setup, tmp_path):
    S.build_job(nrf_board, nrf_setup, S.scenario_for(nrf_board, nrf_setup, "confirm"), tmp_path, debug_png=False)
    conf = S.load_meta(tmp_path)["mesh"]
    scr_cells = 650_000
    assert conf["cells"] > 1.5 * scr_cells and conf["rule_ok"]


def test_material_changes_hash_and_substrate(nrf_board, nrf_setup, tmp_path):
    sc = S.scenario_for(nrf_board, nrf_setup, "screen", [W.SetMaterial(4.0)])
    assert sc.material == {"epsilon_r": 4.0, "loss_tangent": None}
    S.build_job(nrf_board, nrf_setup, sc, tmp_path, debug_png=False)
    j = json.loads((tmp_path / "scenario.json").read_text())
    assert j["substrates"][0]["epsilon_r"] == 4.0


def test_no_kicad_file_written(nrf_board, nrf_setup, tmp_path):
    S.build_job(nrf_board, nrf_setup, S.scenario_for(nrf_board, nrf_setup, "screen", [W.AdjustAntennaLength(-2.0)]), tmp_path, debug_png=False)
    assert not [p for p in tmp_path.rglob("*") if p.suffix.startswith(".kicad")]


@pytest.mark.parametrize("ops", [
    [W.AdjustAntennaLength(-6.0)],
    [W.AdjustAntennaLength(-3.0)],
    [W.ClearCopperRegion(bbox=(36, 17, 44, 22))],
    [W.SetMaterial(4.0)],
])
def test_whatif_models_still_meet_the_mesh_rule(nrf_board, nrf_setup, tmp_path, ops):
    S.build_job(nrf_board, nrf_setup, S.scenario_for(nrf_board, nrf_setup, "screen", ops), tmp_path, debug_png=False)
    rep = S.load_meta(tmp_path)["mesh"]
    assert rep["rule_ok"], rep["rule_reasons"]
