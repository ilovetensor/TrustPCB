from __future__ import annotations

import math
from pathlib import Path

import pytest

from trustpcb.analyzers import ANALYZER_NAMES, AnalyzerContext, run_all
from trustpcb.analyzers import connector_access
from trustpcb.analyzers.base import DEFAULTS, finalize
from trustpcb.pcb import geom
from trustpcb.review.budgets import LAYOUT_RULES

NRF_PCB = Path(__file__).resolve().parents[3] / "samples" / "nrf52840-node" / "nrf52840_rf_iot_node.kicad_pcb"
W, H = 50.0, 40.0
FACING = {0: "bottom", 90: "right", 180: "top", 270: "left"}


def val(c, name):
    return c["measurements"][name]["value"]


def place_at_mouth(add_connector, b, ref, mouth, rot, side="top", **kw):
    """Put a connector so the middle of its mouth lands on ``mouth`` (the mouth is 4 mm from the origin)."""
    flip = -1.0 if side == "bottom" else 1.0
    dx, dy = geom.rotate(0.0, 4.0 * flip, rot)
    add_connector(b, ref, mouth[0] - dx, mouth[1] - dy, rot, side, **kw)


def edge_point(rot):
    return {0: (W / 2, H), 90: (W, H / 2), 180: (W / 2, 0.0), 270: (0.0, H / 2)}[rot]


def run(board, **thresholds):
    return connector_access.analyze(board, AnalyzerContext(thresholds=thresholds))


def test_sample_flags_j1(nrf_board):
    out = connector_access.analyze(nrf_board, AnalyzerContext())
    assert [c["ref"] for c in out] == ["J1"]
    c = out[0]
    assert c["rule"] == "connector_access" and c["source"] == "analyzer" and c["id"] == "candidate:connector_access:1"
    # the mouth is on the right-hand side of the body, away from the signal pads at x = 27.96
    assert c["facing"] == "right" and c["heading_deg"] == 0.0
    assert c["mouth"] == pytest.approx([36.15, 47.0], abs=0.01)
    assert val(c, "mouth_to_edge") == pytest.approx(29.85, abs=0.01)  # up to the right-hand edge at x = 66
    assert c["edge_point"] == pytest.approx([66.0, 47.0], abs=0.01)
    assert c["nearest_edge"]["side"] == "bottom" and c["nearest_edge"]["gap_mm"] == pytest.approx(0.68, abs=0.01)
    assert c["nearest_edge"]["turn_deg"] == pytest.approx(90.0, abs=0.5)
    in_path = {b["ref"]: b for b in c["in_path"]}
    assert {"C4", "SW1"} <= set(in_path)
    assert in_path["C4"]["from_mouth_mm"] == pytest.approx(8.7, abs=0.05)
    assert [b["from_mouth_mm"] for b in c["in_path"]] == sorted(b["from_mouth_mm"] for b in c["in_path"])
    assert "J2" not in in_path and "J1" not in in_path
    assert "board interior" in c["title"] and "29.9 mm" in c["title"]
    assert [m["type"] for m in c["marks"]] == ["object", "link", "object"]
    assert c["marks"][1]["targets"] == ["point:36.15,47", "point:66,47"]
    assert c["targets"][0] == "ref:J1" and {"ref:C4", "ref:SW1"} <= set(c["targets"])
    finalize(nrf_board, out)
    assert all(m["resolved"] for m in c["marks"])


def test_sample_overlay_lists_the_connector(nrf_board):
    data = connector_access.overlays(nrf_board, AnalyzerContext())
    rows = {r["ref"]: r for r in data["connectors"]}
    assert set(rows) == {"J1"} and rows["J1"]["status"] == "blocked" and rows["J1"]["facing"] == "right"
    assert data["note"] == ""


def test_sample_file_is_unchanged_by_the_run(nrf_board):
    before = NRF_PCB.read_bytes()
    connector_access.analyze(nrf_board, AnalyzerContext())
    assert NRF_PCB.read_bytes() == before


@pytest.mark.parametrize("rot", [0, 90, 180, 270])
def test_flush_at_each_edge_passes(builder, rot, add_connector):
    b = builder(W, H)
    place_at_mouth(add_connector, b, "J1", edge_point(rot), rot)
    board = b.build()
    assert run(board) == []
    row = connector_access.overlays(board, AnalyzerContext())["connectors"][0]
    assert row["status"] == "ok" and row["facing"] == FACING[rot]


@pytest.mark.parametrize("rot", [0, 90, 180, 270])
def test_mouth_over_the_edge_passes(builder, rot, add_connector):
    b = builder(W, H)
    ex, ey = edge_point(rot)
    ux, uy = geom.rotate(0.0, 1.0, rot)
    place_at_mouth(add_connector, b, "J1", (ex + 0.4 * ux, ey + 0.4 * uy), rot)
    assert run(b.build()) == []


def test_setback_limit(builder, add_connector):
    b = builder(W, H)
    place_at_mouth(add_connector, b, "J1", (W - 0.8, H / 2), 90)
    assert run(b.build()) == []  # 0.8 mm of board in front is within the 1 mm limit
    b = builder(W, H)
    place_at_mouth(add_connector, b, "J1", (W - 3.0, H / 2), 90)
    out = run(b.build())
    assert len(out) == 1 and out[0]["kind"] == "setback" and val(out[0], "mouth_to_edge") == pytest.approx(3.0, abs=0.01)
    assert "set back 3.0 mm" in out[0]["title"] and "right edge" in out[0]["title"]
    assert out[0]["severity_hint"] == "medium"
    # the limit is a threshold like the others
    assert run(b.build(), connector_setback_mm=3.5) == []
    assert DEFAULTS["connector_setback_mm"] == 1.0


@pytest.mark.parametrize("rot", [0, 90, 180, 270])
def test_each_rotation_gives_the_right_direction_and_distance(builder, rot, add_connector):
    b = builder(W, H)
    add_connector(b, "J1", 25.0, 20.0, rot)
    out = run(b.build())
    assert len(out) == 1
    c = out[0]
    ux, uy = geom.rotate(0.0, 1.0, rot)
    assert c["facing"] == FACING[rot]
    assert c["mouth"] == pytest.approx([25.0 + 4 * ux, 20.0 + 4 * uy], abs=0.01)
    expected = (W - c["mouth"][0]) if rot == 90 else c["mouth"][0] if rot == 270 else (H - c["mouth"][1]) if rot == 0 else c["mouth"][1]
    assert val(c, "mouth_to_edge") == pytest.approx(expected, abs=0.01)
    assert c["kind"] == "interior" and "board interior" in c["title"]


def test_arbitrary_angle(builder, add_connector):
    b = builder(W, H)
    add_connector(b, "J1", 25.0, 12.0, 30.0)
    (c,) = run(b.build())
    ux, uy = math.sin(math.radians(30)), math.cos(math.radians(30))
    mx, my = 25.0 + 4 * ux, 12.0 + 4 * uy
    assert c["mouth"] == pytest.approx([mx, my], abs=0.01)
    assert val(c, "mouth_to_edge") == pytest.approx((H - my) / uy, abs=0.01)
    assert c["heading_deg"] == pytest.approx(math.degrees(math.atan2(uy, ux)), abs=0.1)
    assert c["facing"] == "bottom"


def test_back_side_connector_uses_mirrored_geometry(builder, add_connector):
    # mirrored in y, the mouth of an unrotated back-side part is on the world -y side
    b = builder(W, H)
    add_connector(b, "J1", 25.0, 20.0, 0.0, side="bottom")
    (c,) = run(b.build())
    assert c["facing"] == "top" and c["mouth"] == pytest.approx([25.0, 16.0], abs=0.01)
    assert val(c, "mouth_to_edge") == pytest.approx(16.0, abs=0.01)
    # flush at the top edge it passes; rotated a quarter turn the same geometry points elsewhere
    b = builder(W, H)
    place_at_mouth(add_connector, b, "J1", (W / 2, 0.0), 0.0, side="bottom")
    assert run(b.build()) == []
    b = builder(W, H)
    add_connector(b, "J1", 25.0, 20.0, 90.0, side="bottom")
    (c,) = run(b.build())
    assert c["facing"] == "left" and val(c, "mouth_to_edge") == pytest.approx(25.0 - 4.0, abs=0.01)


def test_parts_in_the_plug_corridor_are_listed(builder, add_connector):
    b = builder(W, H)
    add_connector(b, "J1", 10.0, 20.0, 90.0)  # mouth at x = 14, courtyard spans y 15..25
    b.part("C1", "100n", 24.0, 20.0, [("1", "SIG", -0.5, 0.0), ("2", "GND", 0.5, 0.0)], size=1.0)  # in the corridor
    b.part("C2", "100n", 30.0, 28.0, [("1", "SIG", -0.5, 0.0)], size=1.0)  # beside it
    b.part("TP1", "TP", 20.0, 20.0, [("1", "SIG", 0.0, 0.0)], lib="TestPoint:TestPoint_Pad_D1.0mm")  # flat parts are not in the way
    (c,) = run(b.build())
    assert [x["ref"] for x in c["in_path"]] == ["C1"]
    assert c["in_path"][0]["from_mouth_mm"] == pytest.approx(9.0, abs=0.01)
    assert val(c, "blockers") == 1 and val(c, "corridor_width") == pytest.approx(10.0, abs=0.01)
    assert c["targets"] == ["ref:J1", "ref:C1"]


def test_pin_header_and_other_non_cable_parts_are_ignored(builder, add_connector):
    b = builder(W, H)
    add_connector(b, "J1", 25.0, 20.0, 0.0, lib="Connector_PinHeader_2.54mm:PinHeader_1x06_P2.54mm_Horizontal", value="HDR")
    add_connector(b, "J2", 10.0, 10.0, 0.0, lib="Connector_FFC-FPC:Hirose_FH12-10S-0.5SH_1x10-1MP", value="FPC")
    add_connector(b, "J3", 40.0, 10.0, 0.0, lib="Connector_PinSocket_2.54mm:PinSocket_1x06_P2.54mm_Horizontal", value="USB-C breakout")
    add_connector(b, "J4", 40.0, 30.0, 0.0, lib="Connector_USB:USB_A_Molex_67643_Vertical", value="USB")
    add_connector(b, "TP1", 10.0, 30.0, 0.0, lib="TestPoint:TestPoint_Pad_D1.0mm", value="TP")
    board = b.build()
    assert run(board) == []
    assert connector_access.overlays(board, AnalyzerContext()) == {}


def test_undeterminable_direction_emits_nothing_and_says_so(builder, add_connector):
    b = builder(W, H)
    add_connector(b, "J1", 25.0, 20.0, 0.0, pads="centred")
    add_connector(b, "J2", 10.0, 10.0, 90.0, courtyard=False)
    board = b.build()
    assert run(board) == []
    data = connector_access.overlays(board, AnalyzerContext())
    assert {r["ref"]: r["status"] for r in data["connectors"]} == {"J1": "undetermined", "J2": "undetermined"}
    assert all(r["reason"] for r in data["connectors"])
    assert "J1" in data["note"] and "J2" in data["note"] and "no claim" in data["note"]


def test_cutout_edge_counts_as_an_edge(builder, add_connector):
    # a notch cut into the board in front of the mouth: the plug reaches it
    b = builder(W, H)
    add_connector(b, "J1", 20.0, 20.0, 90.0)  # mouth at x = 24
    board = b.build()
    board.outline.append([[24.5, 15.0], [30.0, 15.0], [30.0, 25.0], [24.5, 25.0]])
    assert run(board) == []  # 0.5 mm to the cut-out wall
    board.outline[-1] = [[28.0, 15.0], [30.0, 15.0], [30.0, 25.0], [28.0, 25.0]]
    (c,) = run(board)
    assert val(c, "mouth_to_edge") == pytest.approx(4.0, abs=0.01)


def test_registered_with_runner_and_layout_gate(builder, add_connector):
    assert "connector_access" in ANALYZER_NAMES and "connector_access" in LAYOUT_RULES
    b = builder(W, H)
    add_connector(b, "J1", 25.0, 20.0, 90.0)
    r = run_all(b.build(), AnalyzerContext())
    assert r["status"]["connector_access"]["status"] == "ok" and r["status"]["connector_access"]["count"] == 1
    assert r["overlays"]["connector_access"]["connectors"][0]["ref"] == "J1"
