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
