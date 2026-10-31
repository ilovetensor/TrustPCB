from __future__ import annotations

import numpy as np
import pytest

from trustpcb.rf import matching as M
from trustpcb.rf.types import MatchElement, MatchNetwork

F = np.linspace(1.6e9, 3.3e9, 171)
BAND = (2.4e9, 2.4835e9)


def antenna_gamma(r=20.0, x=-35.0):
    """A one-port with a fixed impedance r + jx over frequency."""
    return M.z_to_gamma(np.full(F.shape, complex(r, x)))


def pi_network(l_val=2.2e-9, fitted_l=True):
    els = [
        MatchElement("C20", "C", 0.5e-12, "DNP 0.5pF", False, "shunt", ("ANT", "GND"), "ANT", 0),
        MatchElement("L1", "L", l_val, "2.2nH", fitted_l, "series", ("ANT", "CHIP"), "ANT", 1),
        MatchElement("C19", "C", 0.5e-12, "DNP 0.5pF", False, "shunt", ("CHIP", "GND"), "CHIP", 2),
    ]
    return MatchNetwork(["ANT", "CHIP"], els, "pad:U3:1")


def test_recurrence_matches_scikit_rf():
    g = antenna_gamma()
    parts = [("shunt", "C", 1.2e-12), ("series", "L", 3.3e-9), ("shunt", "C", 0.8e-12)]
    assert np.allclose(M.cascade_gamma(g, F, parts), M.skrf_cascade(g, F, parts), atol=1e-6)


def test_as_built_uses_only_fitted_parts():
    out = M.as_built(pi_network(), F, antenna_gamma(), BAND)
    assert [p["kind"] for p in out["as_built"]["parts"]] == ["L"]
    expect = M.cascade_gamma(antenna_gamma(), F, [("series", "L", 2.2e-9)])
    assert np.allclose(out["as_built_gamma"], expect, atol=1e-6)
    assert out["antenna_only"]["s11_min_db"] != out["as_built"]["s11_min_db"]


def test_unfitted_network_equals_antenna():
    net = pi_network(fitted_l=False)
    out = M.as_built(net, F, antenna_gamma(), BAND)
    assert out["as_built"]["band_worst_db"] == out["antenna_only"]["band_worst_db"]


def test_unreadable_fitted_value_gives_no_as_built():
    net = pi_network()
    net.elements[1].value = None
    out = M.as_built(net, F, antenna_gamma(), BAND)
    assert out["as_built"] is None and "cannot be used" in out["network_note"]


def test_e24_values_are_standard():
    c = M.e24_values("C")
    assert any(v == pytest.approx(1.0e-12) for v in c) and any(v == pytest.approx(2.2e-12) for v in c) and all(M.C_RANGE[0] * 0.99 <= v <= M.C_RANGE[1] * 1.01 for v in c)
    assert M.nearest_e24("L", 2.25e-9) == pytest.approx(2.2e-9)


def test_tune_improves_a_mismatched_antenna():
    g = antenna_gamma(r=10.0, x=-60.0)  # poor match
    net = pi_network()
    r = M.tune_matching(net, F, g, BAND)
    assert r["ok"] and r["best"]["result"]["band_worst_db"] < r["as_built"]["band_worst_db"] - 3
    for row in r["best"]["parts"]:
        if row["value"] is not None:
            assert any(abs(np.log(row["value"] / v)) < 1e-6 for v in M.e24_values(row["kind"]))
    assert r["best"]["meets_target"]
    assert r["searched"] > 100_000


def test_tune_already_matched_changes_nothing_needed():
    g = antenna_gamma(r=50.0, x=0.0)
    r = M.tune_matching(pi_network(fitted_l=False), F, g, BAND)
    assert r["best"]["fitted_parts"] == 0 and r["best"]["meets_target"]


def test_tune_refuses_unknown_network():
    assert not M.tune_matching(None, F, antenna_gamma(), BAND)["ok"]
    net = pi_network()
    net.understood = False
    net.notes = ["branch"]
    assert not M.tune_matching(net, F, antenna_gamma(), BAND)["ok"]


def test_nrf_network_as_built(nrf_setup):
    out = M.as_built(nrf_setup.network, F, antenna_gamma(), BAND)
    assert [p["kind"] for p in out["as_built"]["parts"]] == ["L"]


# ---- explicit changes, response fields and the swap of an inductor for a capacitor ------------------------------------------


def test_apply_changes_swaps_an_inductor_for_a_series_capacitor():
    """An antenna 10 + j88 ohm at the band centre: a series capacitor of 1/(2 pi f 88) = 0.74 pF cancels the reactance."""
    g = M.z_to_gamma(np.full(F.shape, complex(10.0, 88.0)))
    net = pi_network()
    out = M.apply_changes(net, F, g, BAND, [{"ref": "L1", "kind": "C", "value": 0.75e-12}])
    assert out["ok"] and [r["changed"] for r in out["parts"]] == [False, True, False]
    assert out["parts"][1]["was"] == "2.2 nH (fitted)" and out["parts"][1]["now"] == "0.75 pF" and out["parts"][1]["kind"] == "C"
    expect = M.cascade_gamma(g, F, [("series", "C", 0.75e-12)])
    assert np.allclose(out["gamma"], expect, atol=1e-6)
    z = M.gamma_to_z(out["gamma"])[np.argmin(abs(F - 2.44e9))]
    assert abs(z.imag) < 3.0 and z.real == pytest.approx(10.0, abs=0.5)          # the reactance is gone; the 10 ohm resistance is what is left
    assert out["result"]["band_worst_db"] < M.as_built(net, F, g, BAND)["as_built"]["band_worst_db"] - 2.5
    assert out["result"]["band_worst_db"] == pytest.approx(M.band_stats(F, expect, BAND)["band_worst_db"])


def test_apply_changes_adds_a_shunt_part_takes_one_out_and_refuses_unknown_parts():
    g = antenna_gamma(r=20.0, x=-35.0)
    added = M.apply_changes(pi_network(), F, g, BAND, [{"ref": "C20", "kind": "C", "value": 1.0e-12}])
    assert added["ok"] and [p[:2] for p in [(r["ref"], r["changed"]) for r in added["parts"]]] == [("C20", True), ("L1", False), ("C19", False)]
    expect = M.cascade_gamma(g, F, [("shunt", "C", 1.0e-12), ("series", "L", 2.2e-9)])
    assert np.allclose(added["gamma"], expect, atol=1e-6)
    removed = M.apply_changes(pi_network(), F, g, BAND, [{"ref": "L1", "kind": None, "value": None}])
    assert removed["parts"][1]["now"] == "0 ohm link" and removed["parts"][1]["changed"] is True
    assert np.allclose(removed["gamma"], g, atol=1e-6)
    assert not M.apply_changes(pi_network(), F, g, BAND, [{"ref": "R99", "kind": "C", "value": 1e-12}])["ok"]
    assert not M.apply_changes(pi_network(), F, g, BAND, [{"ref": "L1", "kind": "C", "value": -1.0}])["ok"]
    assert not M.apply_changes(None, F, g, BAND, [])["ok"]


def test_tune_marks_the_parts_that_changed():
    r = M.tune_matching(pi_network(), F, antenna_gamma(r=10.0, x=-60.0), BAND)
    assert any(row["changed"] for row in r["best"]["parts"]) and all(isinstance(row["changed"], bool) for row in r["best"]["parts"])
    already = M.tune_matching(pi_network(fitted_l=False), F, antenna_gamma(r=50.0, x=0.0), BAND)
    assert not any(row["changed"] for row in already["best"]["parts"])           # nothing to change when it is matched already


def test_response_fields_state_dips_series_resonance_and_the_band_centre_impedance():
    z = np.where(F < 2.0e9, complex(30, -40), complex(30, 70))          # reactance crosses zero upward near 2.0 GHz? no: it jumps, so use a ramp below
    z = 30 + 1j * (F - 2.0e9) / 1e9 * 200.0                               # -80 ohm at 1.6 GHz up to +260 ohm at 3.3 GHz, zero at 2.0 GHz
    g = M.z_to_gamma(z)
    fields = M.response_fields(F, g, BAND)
    assert fields["first_series_resonance_hz"] == pytest.approx(2.0e9, rel=0.01)
    centre = fields["zin_band_centre"]
    assert centre["f_hz"] == pytest.approx(2.44e9, rel=0.01) and centre["re"] == pytest.approx(30.0, abs=0.1) and centre["im"] == pytest.approx(88.0, abs=3.0)
    assert all(d["s11_db"] <= -3.0 for d in fields["dips"])
    flat = M.response_fields(F, antenna_gamma(r=50.0, x=0.0), BAND)
    assert flat["first_series_resonance_hz"] is None and flat["zin_band_centre"]["im"] == pytest.approx(0.0, abs=0.01)


def test_match_changes_on_the_real_confirm_result():
    """The repository's confirm run of the nRF antenna: 0.82 pF in place of L1 reaches the README's single-change figure; the 10 + j88 ohm centre impedance is reported."""
    from pathlib import Path
    from trustpcb.rf import lab
    job = Path(__file__).resolve().parents[3] / "web" / "public" / "fixtures" / "nrf-sim"
    out = lab.match_changes(job, [{"ref": "L1", "kind": "C", "value": 0.82e-12}])
    assert out["ok"] and out["result"]["band_worst_db"] == pytest.approx(-3.49, abs=0.02)
    stock = lab.match_changes(job, [])
    assert stock["result"]["band_worst_db"] == pytest.approx(-0.503, abs=0.01)                  # as built: almost everything reflected
    assert stock["zin_band_centre"]["re"] == pytest.approx(10.4, abs=0.2) and stock["zin_band_centre"]["im"] == pytest.approx(121.8, abs=0.2)     # the antenna's 10 + j88 ohm plus the 2.2 nH of L1
    assert 1.85e9 < min(d["f_hz"] for d in stock["dips"]) < 1.9e9 and out["curve"]["freq_hz"] and len(out["curve"]["freq_hz"]) == len(out["curve"]["s11_db"])
    best = lab.tune_matching(job)
    assert best["best"]["result"]["band_worst_db"] == pytest.approx(-6.12, abs=0.02) and best["best"]["meets_target"] is False   # the honest best at confirm quality
