"""A printed antenna whose copper is drawn in a library footprint (the TI inverted-F), end to end on a small hand-written board:
detection, the feed and ground reference, the assumed stackup, the model copper and the what-ifs that cannot apply."""
from __future__ import annotations

import json

import pytest

from trustpcb.intake.detect import detect
from trustpcb.pcb.model import parse_board_text
from trustpcb.rf import geometry as geo
from trustpcb.rf import scenario as S
from trustpcb.rf import whatif
from trustpcb.rf.setup import plan_setup, stackup_check, typical_stackup

STACK = """(setup (stackup
  (layer "F.Cu" (type "copper") (thickness 0.035))
  (layer "dielectric 1" (type "prepreg") (thickness {d1}) (material "FR4") (epsilon_r 4.4) (loss_tangent 0.02))
  (layer "In1.Cu" (type "copper") (thickness 0.0175))
  (layer "dielectric 2" (type "core") (thickness {d2}) (material "FR4") (epsilon_r 4.4) (loss_tangent 0.02))
  (layer "In2.Cu" (type "copper") (thickness 0.0175))
  (layer "dielectric 3" (type "prepreg") (thickness {d1}) (material "FR4") (epsilon_r 4.4) (loss_tangent 0.02))
  (layer "B.Cu" (type "copper") (thickness 0.035))))"""

IFA = """(footprint "footprints:Texas_SWRA117D_2.4GHz_Left" (layer "F.Cu") (uuid "aaaaaaaa-0000-0000-0000-000000000001") (at 0 0)
  (property "Reference" "AE1" (at 0 0 0) (layer "F.SilkS")) (property "Value" "Antenna_PCB" (at 0 1 0) (layer "F.Fab"))
  (fp_poly (pts (xy 1 5) (xy 9 5) (xy 9 6.2) (xy 8.5 6.2) (xy 8.5 8) (xy 9 8) (xy 9 6.2) (xy 7 6.2) (xy 7 8) (xy 6.5 8) (xy 6.5 6.2) (xy 1 6.2))
    (stroke (width 0) (type solid)) (fill yes) (layer "F.Cu"))
  (pad "1" connect rect (at 6.75 8.3) (size 0.5 0.8) (layers "F.Cu") (net "/Ant"))
  (pad "2" connect rect (at 8.75 8.3) (size 0.8 0.8) (layers "F.Cu") (net "GND"))
  (pad "2" thru_hole rect (at 8.75 8.9) (size 0.9 0.5) (drill 0.3) (layers "*.Cu") (net "GND")))"""

PARTS = """(footprint "R:R_0402" (layer "F.Cu") (uuid "bbbbbbbb-0000-0000-0000-000000000001") (at 13 8.3)
  (property "Reference" "R1" (at 0 0 0)) (property "Value" "0R" (at 0 1 0))
  (pad "1" smd rect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net "/Ant")) (pad "2" smd rect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net "/RF")))
(footprint "Package_DFN_QFN:QFN-48" (layer "F.Cu") (uuid "bbbbbbbb-0000-0000-0000-000000000002") (at 25 8.3)
  (property "Reference" "U1" (at 0 0 0)) (property "Value" "nRF52832" (at 0 1 0))
  (pad "1" smd rect (at -1 0) (size 0.4 0.4) (layers "F.Cu") (net "/RF")) (pad "2" smd rect (at 0 0) (size 0.4 0.4) (layers "F.Cu") (net "GND")))
(segment (start 6.75 8.3) (end 12.5 8.3) (width 0.4) (layer "F.Cu") (net "/Ant") (uuid "cccccccc-0000-0000-0000-000000000001"))
(segment (start 13.5 8.3) (end 24 8.3) (width 0.4) (layer "F.Cu") (net "/RF") (uuid "cccccccc-0000-0000-0000-000000000002"))"""

GROUND = """(zone (net "GND") (net_name "GND") (layer "In1.Cu") (uuid "dddddddd-0000-0000-0000-000000000001") (polygon (pts (xy 8 0) (xy 60 0) (xy 60 30) (xy 8 30)))
  (filled_polygon (layer "In1.Cu") (pts (xy 8 0) (xy 60 0) (xy 60 30) (xy 8 30))))"""


def board_text(extra="", d1=0.2, d2=1.095):
    return f"""(kicad_pcb (version 20240108) (generator pcbnew)
 (layers (0 "F.Cu" signal) (1 "In1.Cu" signal) (2 "In2.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
 {STACK.format(d1=d1, d2=d2)}
 (gr_rect (start 0 0) (end 60 30) (stroke (width 0.1) (type solid)) (fill none) (layer "Edge.Cuts"))
 {IFA}
 {PARTS}
 {GROUND}
 {extra})"""


@pytest.fixture(scope="module")
def board():
    return parse_board_text(board_text(), "v-ifa")


@pytest.fixture(scope="module")
def setup(board):
    s = plan_setup(board)
    assert s.simulatable, getattr(s, "reason", "")
    return s


def test_library_antenna_footprint_is_a_printed_antenna_not_a_chip(board):
    d = detect(board)
    a = d.antenna
    assert d.wireless and a["type"] == "trace" and a["source"] == "footprint" and a["footprint"] == "AE1"
    assert a["net"] == "/Ant" and a["feed_pad"] == "pad:AE1:1" and "pad:AE1:2" in a["ground_pads"]
    assert a["confidence"] == "high" and a["area_mm2"] > 5 and a["confidence_reasons"]
    assert any(e.kind == "antenna_shape" and "AE1" in e.text for e in d.evidence)
    assert d.band["low_ghz"] == 2.4                                    # from the part family and the footprint name


def test_a_chip_antenna_footprint_stays_a_chip_antenna():
    chip = """(footprint "RF_Antenna:Johanson_2450AT43A100" (layer "F.Cu") (uuid "aaaaaaaa-0000-0000-0000-000000000009") (at 5 5)
      (property "Reference" "AE1" (at 0 0 0)) (property "Value" "Antenna" (at 0 1 0))
      (pad "1" smd rect (at -1 0) (size 0.8 0.9) (layers "F.Cu") (net "/RF")) (pad "2" smd rect (at 1 0) (size 0.8 0.9) (layers "F.Cu") (net "GND")))"""
    b = parse_board_text(board_text().replace(IFA, chip), "v-chip")
    d = detect(b)
    assert d.antenna["type"] == "chip" and plan_setup(b, d).code == "chip_antenna"


def test_ground_tracks_in_a_keepout_are_not_an_antenna():
    ko = """(zone (net 0) (net_name "") (layers "F.Cu" "In1.Cu") (uuid "eeeeeeee-0000-0000-0000-000000000001") (name "antenna")
      (keepout (tracks not_allowed) (vias not_allowed) (pads allowed) (copperpour not_allowed) (footprints allowed))
      (polygon (pts (xy 0 20) (xy 10 20) (xy 10 29) (xy 0 29))))
     (segment (start 1 21) (end 9 21) (width 0.3) (layer "F.Cu") (net "GND") (uuid "cccccccc-0000-0000-0000-000000000011"))
     (segment (start 9 21) (end 9 25) (width 0.3) (layer "F.Cu") (net "GND") (uuid "cccccccc-0000-0000-0000-000000000012"))
     (segment (start 9 25) (end 1 25) (width 0.3) (layer "F.Cu") (net "GND") (uuid "cccccccc-0000-0000-0000-000000000013"))"""
    b = parse_board_text(board_text(ko).replace(IFA, ""), "v-trap")
    d = detect(b)
    assert not d.antenna or d.antenna.get("net") != "GND"
    assert plan_setup(b, d).simulatable is False


def test_port_sits_on_the_feed_line_where_ground_starts(setup):
    # the feed pad (6.75, 8.3) has no ground under it: the In1 plane begins at x = 8
    assert setup.port_mode == "feed_line" and setup.antenna_footprint == "AE1"
    assert setup.ground_layer == "In1.Cu" and setup.port_layer == "F.Cu"
    assert 8.4 < setup.port_xy[0] < 10.0 and setup.port_xy[1] == pytest.approx(8.3)
    assert setup.feed_line_mm == pytest.approx(setup.port_xy[0] - 6.75, abs=0.11)
    assert any("feed line" in a and "microstrip" in a for a in setup.assumptions)
    assert [e.ref for e in setup.network.elements] == ["R1"] and setup.network.source == "pad:U1:1"   # the antenna's own pads are not the source


def test_the_antenna_polygon_stub_and_plated_hole_are_in_the_model(board, setup):
    items = geo.collect_items(board, setup)
    shapes = [i for i in items if i.kind == "gfx" and i.antenna]
    assert shapes and shapes[0].layer == "F.Cu" and shapes[0].min_width and shapes[0].min_width <= 1.5
    model = geo.build_model(board, setup)
    assert any(v["id"] == "pad:AE1:2#2" and len(v["layers"]) == 4 for v in model.vias)
    from shapely.geometry import Point
    f = model.layers["F.Cu"]
    assert f.contains(Point(5, 5.6))                                   # the radiator
    assert f.contains(Point(8.75, 7.5))                                # the shorted stub reaches the ground pad, merged into one shape
    assert f.contains(Point(8.75, 8.3))
    scn = S.scenario_for(board, setup, "screen")
    jd = S.build_job(board, setup, scn, debug_png=False)
    sc = json.loads((jd / "scenario.json").read_text())
    assert sc["port"]["start"][2] < sc["port"]["end"][2] and sc["port"]["axis"] == "z"
    assert sc["vias"], "the plated hole of the shorted stub joins the layers"
    meta = json.loads((jd / "meta.json").read_text())
    assert meta["mesh"]["rule_ok"] and meta["mesh"]["max_growth_ratio"] < 1.8


def test_what_ifs_that_cannot_apply_are_refused_with_a_reason(board, setup):
    with pytest.raises(whatif.WhatIfError) as e:
        whatif.validate_ops([whatif.AdjustAntennaLength(1.0)], board, setup)
    assert e.value.code == "footprint_antenna" and "AE1" in e.value.message
    with pytest.raises(whatif.WhatIfError) as e:
        whatif.validate_ops([whatif.RemoveNearbyPart("AE1")], board, setup)
    assert e.value.code == "antenna_part"
    whatif.validate_ops([whatif.SetMaterial(4.2)], board, setup)       # general ones still work
    jd = S.build_job(board, setup, S.scenario_for(board, setup, "screen", [whatif.SetMaterial(4.2)]), debug_png=False)
    assert json.loads((jd / "meta.json").read_text())["material"]["epsilon_r"] == 4.2


def test_stackup_default_is_flagged_and_an_assumed_one_is_labelled():
    odd = parse_board_text(board_text(d1=1.51, d2=1.51), "v-odd")
    assert stackup_check(odd.stackup) and "KiCad" in " ".join(stackup_check(odd.stackup))
    s = plan_setup(odd)
    assert s.simulatable and s.stackup_origin == "suspect_default" and s.stackup_note
    assert any("does not look like a real board" in w for w in s.warnings)
    a = plan_setup(odd, assumed_stackup={"total_thickness_mm": 1.6})
    assert a.stackup_origin == "assumed_by_user" and a.stackup["total_mm"] == pytest.approx(1.565, abs=0.01)
    assert any(x.startswith("ASSUMED STACKUP") for x in a.assumptions) and not a.stackup_note
    assert S.geometry_key(odd, s) != S.geometry_key(odd, a)            # the stackup is part of the cache key
    st = typical_stackup(odd, 1.6)
    d = [l["thickness_mm"] for l in st["layers"] if l["kind"] == "dielectric"]
    assert d[0] == d[-1] == 0.2 and sum(d) + 0.07 + 0.035 == pytest.approx(1.6, abs=1e-3)
    with pytest.raises(ValueError):
        typical_stackup(odd, 9.0)
    assert plan_setup(odd, assumed_stackup={"total_thickness_mm": "x"}).code == "bad_assumed_stackup"


def test_no_ground_anywhere_is_still_refused():
    b = parse_board_text(board_text().replace(GROUND, ""), "v-noground")
    s = plan_setup(b)
    assert s.simulatable is False and s.code in ("no_ground", "no_ground_reference")


def test_custom_pad_keeps_every_part_and_its_arcs():
    pad = """(footprint "X:cp" (layer "F.Cu") (uuid "aaaaaaaa-0000-0000-0000-000000000007") (at 10 10)
      (property "Reference" "TP1" (at 0 0 0)) (property "Value" "x" (at 0 1 0))
      (pad "1" smd custom (at 0 0) (size 1 1) (layers "F.Cu") (net "N1") (options (clearance outline) (anchor circle))
        (primitives (gr_poly (pts (xy 3 0) (xy 6 0) (xy 6 1) (xy 3 1)) (width 0) (fill yes))
                    (gr_poly (pts (xy 0 3) (xy 1 3) (xy 1 6) (xy 0 6)) (width 0) (fill yes)))))"""
    b = parse_board_text(board_text().replace(PARTS, pad), "v-custom")
    p = b.pads_of("TP1")[0]
    assert p.polys and len(p.polys) == 3                               # anchor and two separate parts
    assert geo.pad_geometry(p).area > 6.0
    assert "polys" not in p.to_dict()
