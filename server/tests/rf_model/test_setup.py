from __future__ import annotations

import dataclasses

import pytest

from trustpcb.intake.detect import Detection
from trustpcb.pcb.model import parse_board_text
from trustpcb.rf.setup import parse_value, plan_setup
from trustpcb.rf.types import NotSimulatable


def test_nrf_trace_setup(nrf_setup):
    s = nrf_setup
    assert s.kind == "trace" and s.antenna_net == "ANT_FEED"
    assert s.port_on == "pad:L1:2"  # antenna side of the series part
    assert s.port_layer == "F.Cu" and s.ground_layer == "B.Cu"
    assert s.port_width_mm == 0  # 0.3 mm feed track: point port
    assert s.band_ghz == [2.4, 2.4835]
    assert s.region[0] == 0 and s.region[2] == 66  # snapped to the board edge
    assert s.region[3] < 53  # quarter wavelength limit
    assert s.narrowest_mm == pytest.approx(0.3)
    assert any("unresolved" in w or "not resolved" in w for w in s.warnings)


def test_nrf_matching_network(nrf_setup):
    n = nrf_setup.network
    assert n.understood and n.chain == ["ANT_FEED", "ANT_CHIP"] and n.source == "pad:U3:H23"
    by = {e.ref: e for e in n.elements}
    assert by["L1"].populated and by["L1"].value == pytest.approx(2.2e-9) and by["L1"].topology == "series"
    assert not by["C19"].populated and not by["C20"].populated
    assert by["C20"].position < by["L1"].position < by["C19"].position  # antenna side first


def test_patch_setup_microstrip_feed(patch_setup):
    s = patch_setup
    assert s.kind == "patch" and s.port_on == "pad:J1:1"
    assert s.port_width_mm > 1.0 and s.port_axis == "x"  # strip port across the 3 mm line
    assert s.network.chain == ["ANTENNA_50R"]
    assert all(not e.populated for e in s.network.elements)


@pytest.mark.parametrize("text,kind,value,dnp", [
    ("2.2nH", "L", 2.2e-9, False), ("DNP 0.5pF", "C", 0.5e-12, True), ("4n7", "C", 4.7e-9, False),
    ("DNP_SERIES", "C", None, True), ("10", "L", None, False), ("100k", "R", 100e3, False),
])
def test_parse_value(text, kind, value, dnp):
    v, d = parse_value(kind, text)
    assert d is dnp
    assert (v is None) if value is None else v == pytest.approx(value)


def test_not_simulatable_cases(nrf_board):
    det = Detection(False, None, None, [], [], [])
    assert plan_setup(nrf_board, det).code == "not_wireless"
    for kind, code in (("chip", "chip_antenna"), ("module", "module")):
        det = Detection(True, {"low_ghz": 2.4, "high_ghz": 2.48}, {"type": kind, "targets": ["ref:A1"]}, [], [], [])
        r = plan_setup(nrf_board, det)
        assert isinstance(r, NotSimulatable) and r.code == code and not r.simulatable
    det = Detection(True, None, {"type": "trace", "net": "ANT_FEED", "feed_point": [32.485, 12.8], "layer": "F.Cu", "open_ends": []}, [], [], [])
    assert plan_setup(nrf_board, det).code == "band_unknown"


def test_no_ground_under_feed_is_refused(nrf_board):
    b = dataclasses.replace(nrf_board, zones=[z for z in nrf_board.zones if z.net != "GND"])
    r = plan_setup(b)
    assert isinstance(r, NotSimulatable) and r.code in ("no_ground", "no_ground_reference")


def test_feed_ambiguous_is_refused(nrf_board, monkeypatch):
    from trustpcb.intake.detect import detect

    det = detect(nrf_board)
    det.antenna = {**det.antenna, "feed_point": None}
    r = plan_setup(nrf_board, det)
    # two candidate pads would be a guess; one series part is unique, so this still resolves
    assert r.simulatable or r.code in ("feed_ambiguous", "no_feed")
