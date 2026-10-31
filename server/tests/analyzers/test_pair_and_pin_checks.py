"""Pair skew, the per-pin decoupling check, antenna pour per layer, and the return path's plane net, clock nets, antipads and rule-area slots."""
from __future__ import annotations

import math

import pytest

from trustpcb.analyzers import AnalyzerContext, antenna_keepout, decoupling, decoupling_pin, pair_skew, return_path
from trustpcb.analyzers.base import ANALYZER_NAMES
from trustpcb.analyzers.pair_skew import find_pairs
from trustpcb.pcb.model import Layer

BOX = [[0, 0], [50, 0], [50, 40], [0, 40]]


def val(c, name):
    return c["measurements"][name]["value"]


def four_layer(board):
    board.layers = [Layer("F.Cu", "copper", 0), Layer("In1.Cu", "copper", 1), Layer("In2.Cu", "copper", 2), Layer("B.Cu", "copper", 31)]
    return board


# ---- pair_skew -------------------------------------------------------------------------------------------------------------


def test_pair_names():
    names = ["/USB_DP", "/USB_DM", "USB_D+", "USB_D-", "ADC_P", "ADC_N", "ADP", "LONE_P", "FPGA_P2"]
    pairs = {(p, n) for _, p, n in find_pairs(names)}
    assert pairs == {("/USB_DP", "/USB_DM"), ("USB_D+", "USB_D-"), ("ADC_P", "ADC_N")}


def test_pair_skew_reports_mm_and_ps(builder):
    b = builder()
    b.track("USB_DP", (10, 10), (30, 10))
    b.track("USB_DM", (10, 12), (28.5, 12))
    out = pair_skew.analyze(b.build(), AnalyzerContext())
    assert len(out) == 1
    c = out[0]
    assert val(c, "mismatch") == pytest.approx(1.5, abs=0.01)
    assert val(c, "skew_estimate") == pytest.approx(9.0, abs=0.1)
    assert c["pair"] == ["USB_DP", "USB_DM"] and c["longer"] == "USB_DP"
    assert [m["type"] for m in c["marks"]] == ["object", "pin"]


def test_pair_skew_quiet_when_matched_or_unrouted(builder):
    b = builder()
    b.track("USB_DP", (10, 10), (30, 10))
    b.track("USB_DM", (10, 12), (30, 12))
    b.track("X_P", (10, 20), (30, 20))  # no X_N tracks
    assert pair_skew.analyze(b.build(), AnalyzerContext()) == []
    assert "pair_skew" in ANALYZER_NAMES


def test_pair_skew_on_the_nrf_sample(nrf_board):
    # the sample's USB pair is routed; whatever it measures is listed in the overlay and every candidate is over the limit
    ctx = AnalyzerContext()
    out = pair_skew.analyze(nrf_board, ctx)
    assert all(val(c, "mismatch") > 0.25 for c in out)
    assert pair_skew.analyze(nrf_board, AnalyzerContext()) == out


# ---- decoupling, per supply pin --------------------------------------------------------------------------------------------


def test_pin_with_no_capacitor_within_limit_is_a_candidate(builder):
    b = builder()
    b.part("U1", "MCU", 20, 20, [("1", "3V3", -1, 0), ("2", "3V3", 1, 0), ("3", "GND", 0, 1)], lib="Package_QFP:QFP-8", size=2)
    b.part("U9", "MCU2", 40, 30, [("1", "3V3", 0, 0), ("2", "GND", 1, 0), ("3", "GND", 2, 0), ("4", "SIG", 3, 0)], lib="Package_QFP:QFP-8")
    b.part("C1", "100nF", 17.5, 20, [("1", "3V3", 0, 0), ("2", "GND", 0, 1.2)])  # 1.5 mm from pin 1
    out = decoupling_pin.analyze(b.build(), AnalyzerContext())
    pins = out
    mine = {(c["ic"], c["ic_pin"]): c for c in pins}
    assert ("U1", "2") in mine and ("U1", "1") not in mine
    c = mine[("U1", "2")]
    assert c["nearest_capacitor"] == "C1" and val(c, "nearest_distance") == pytest.approx(3.5, abs=0.05)
    assert "has no capacitor within 3 mm" in c["title"]


def test_pin_check_quiet_when_every_pin_has_a_capacitor(builder):
    b = builder()
    b.part("U1", "MCU", 20, 20, [("1", "3V3", -1, 0), ("2", "3V3", 1, 0), ("3", "GND", 0, 1)], lib="Package_QFP:QFP-8", size=2)
    b.part("C1", "100nF", 18.5, 22, [("1", "3V3", 0, 0), ("2", "GND", 0, 1.2)])
    b.part("C2", "100nF", 21.5, 22, [("1", "3V3", 0, 0), ("2", "GND", 0, 1.2)])
    assert decoupling_pin.analyze(b.build(), AnalyzerContext()) == []


def test_pin_check_on_the_nrf_sample(nrf_board):
    cap_rows = decoupling.analyze(nrf_board, AnalyzerContext())
    assert not any(c.get("pin_check") for c in cap_rows) and {c["capacitor"] for c in cap_rows} >= {"C5", "C10", "C9"}  # per-capacitor candidates are unchanged
    pin_rows = decoupling_pin.analyze(nrf_board, AnalyzerContext())
    dec1 = next(c for c in pin_rows if c["ic"] == "U3" and c["ic_pin_name"] == "DEC1")
    assert dec1["rule"] == "decoupling_pin" and dec1["nearest_capacitor"] == "C5" and val(dec1, "nearest_distance") == pytest.approx(9.0, abs=0.1)


# ---- antenna keep-out ------------------------------------------------------------------------------------------------------


def _antenna_board(builder, inner_ground: bool):
    b = builder()
    b.part("AE1", "Antenna", 20, 20, [("1", "ANT_FEED", -2, 0), ("2", "GND", 2, 0)], lib="RF:Chip_Antenna", size=3)
    b.track("ANT_FEED", (10, 20), (18, 20))
    b.part("L2", "2.4nH", 14, 20, [("1", "ANT_FEED", -0.7, 0), ("2", "MATCH", 0.7, 0)])  # matching part: on the antenna's own network
    if inner_ground:
        b.zone("GND", "In1.Cu", [[15, 15], [25, 15], [25, 25], [15, 25]])
    b.zone("+3V3", "In2.Cu", [[0, 0], [50, 0], [50, 40], [0, 40]])
    for i in range(4):  # a via fence beside the antenna
        b.via("GND", 24, 17 + i * 2)
    b.via("", 17, 23)
    board = four_layer(b.build())
    for v in board.vias:
        if v.net == "":
            v.net = None
    return board


def test_inner_ground_under_antenna_is_reported_per_layer(builder):
    out = antenna_keepout.analyze(_antenna_board(builder, True), AnalyzerContext())
    assert len(out) == 1
    c = out[0]
    by_layer = {(r["layer"], r["net"]): r for r in c["copper_pour_by_layer"]}
    assert by_layer[("In1.Cu", "GND")]["ground"] is True and by_layer[("In1.Cu", "GND")]["area_mm2"] > 10
    assert by_layer[("In2.Cu", "+3V3")]["ground"] is False
    assert c["ground_under_antenna_by_layer"]["In1.Cu"] > 5 and val(c, "ground_under_antenna") > 5
    assert "In1.Cu" in c["ground_distance_by_layer"] and "ground_distance_surface" in c["measurements"]
    assert c["severity_hint"] == "high"


def test_via_fence_and_own_network_are_not_counted_and_floating_via_is_flagged(builder):
    c = antenna_keepout.analyze(_antenna_board(builder, False), AnalyzerContext())[0]
    assert len(c["via_fence"]) == 4 and val(c, "via_fence_vias") == 4
    assert val(c, "vias") == 1 and c["vias_without_net"] and val(c, "vias_without_net") == 1
    assert "MATCH" in c["own_network"] and "ref:L2" not in c["targets"]
    assert "ground_under_antenna" not in c["measurements"]


# ---- return path -----------------------------------------------------------------------------------------------------------


def test_reference_plane_on_another_net_is_named(builder):
    b = builder()
    b.zone("+3V3", "B.Cu", BOX)
    b.track("USB_D+", (10, 20), (30, 20))
    c = return_path.analyze(b.build(), AnalyzerContext())[0]
    assert c["reference_not_ground"] == {"B.Cu": ["+3V3"]}
    assert "B.Cu is a +3V3 plane, not ground" in c["title"]
    assert c["reference_planes"]["B.Cu"] == {"ground": False, "other_plane_nets": ["+3V3"]}
    assert val(c, "over_other_plane_length") > 15


def test_clock_part_nets_are_fast(builder):
    b = builder()
    b.zone("GND", "B.Cu", BOX, holes=[(20, 0, 22, 40)])
    b.part("U5", "12MHz", 8, 20, [("1", "NET_Q", 0, 0), ("2", "GND", 1, 1), ("3", "3V3", -1, 1)], lib="Oscillator:Oscillator_SMD_2520")
    b.track("NET_Q", (8, 20), (30, 20))
    out = return_path.analyze(b.build(), AnalyzerContext())
    assert [c["net"] for c in out] == ["NET_Q"] and out[0]["fast_basis"] == "on a pad of U5"
    ctx = AnalyzerContext()
    ctx.part_roles = {"U9": "clock"}
    b = builder()
    b.zone("GND", "B.Cu", BOX, holes=[(20, 0, 22, 40)])
    b.part("U9", "X", 8, 20, [("1", "FOO", 0, 0)], lib="X:Y")
    b.track("FOO", (8, 20), (30, 20))
    assert [c["net"] for c in return_path.analyze(b.build(), ctx)] == ["FOO"]


def test_antipad_void_at_a_track_end_is_not_a_gap(builder):
    b = builder()
    b.zone("GND", "B.Cu", BOX, holes=[(9.2, 19.2, 10.8, 40)])  # the via's antipad: a 1.6 mm wide void (cut to the edge so the fill keeps it)
    b.via("USB_D+", 10, 20)
    b.track("USB_D+", (10, 20), (30, 20))
    assert return_path.analyze(b.build(), AnalyzerContext()) == []
    b = builder()  # the same void with no via at the end is a gap
    b.zone("GND", "B.Cu", BOX, holes=[(9.2, 19.2, 10.8, 40)])
    b.track("USB_D+", (10, 20), (30, 20))
    out = return_path.analyze(b.build(), AnalyzerContext())
    assert len(out) == 1 and math.isclose(val(out[0], "gap_length"), 0.8, abs_tol=0.3)


def test_track_over_a_rule_area_slot_is_a_candidate(builder):
    b = builder()
    b.zone("GND", "B.Cu", BOX, holes=[(20, 0, 20.8, 40)])
    b.keepout([[20, 0], [20.8, 0], [20.8, 40], [20, 40]], name="slot", layers=("B.Cu",))
    b.track("SIG", (10, 20), (30, 20))
    out = [c for c in return_path.analyze(b.build(), AnalyzerContext()) if c.get("kind") == "rule_area_slot"]
    assert len(out) == 1
    assert val(out[0], "slot_width") == pytest.approx(0.8, abs=0.01) and val(out[0], "crossing_length") == pytest.approx(0.8, abs=0.05)
    assert out[0]["severity_hint"] == "low" and out[0]["net"] == "SIG"
