from __future__ import annotations

import math

from trustpcb.analyzers import AnalyzerContext, run_all
from trustpcb.analyzers import (
    antenna_keepout,
    decoupling,
    edge_track,
    impedance,
    noise_proximity,
    return_path,
    rf_path,
    track_angle,
    track_detour,
    track_jitter,
    via_count,
    zones,
)
from trustpcb.analyzers.base import finalize


def val(c, name):
    return c["measurements"][name]["value"]


def test_track_angle_acute_corner(builder):
    b = builder()
    # interior angle between the two tracks is 45 degrees
    b.track("SIG", (10, 10), (20, 10))
    b.track("SIG", (20, 10), (10, 20))
    board = b.build()
    out = track_angle.analyze(board, AnalyzerContext())
    assert len(out) == 1
    assert math.isclose(val(out[0], "angle"), 45.0, abs_tol=0.2)
    assert out[0]["marks"][0]["type"] == "pin" and out[0]["marks"][0]["targets"][0].startswith("point:20,10")


def test_track_angle_right_angle_not_flagged(builder):
    b = builder()
    b.poly("SIG", [(10, 10), (20, 10), (20, 20)])
    assert track_angle.analyze(b.build(), AnalyzerContext()) == []


def test_jitter_zigzag_flagged_and_smooth_curve_not(builder):
    b = builder()
    # 0.25 wide: window 1.25 mm. A jog with four turns in under 1.2 mm
    b.poly("SIG", [(5, 5), (10, 5), (10.3, 5.3), (10.6, 5), (10.9, 5.3), (11.2, 5), (16, 5)])
    # a chamfered rounded corner turns one way only
    b.poly("CURVE", [(5, 20), (10, 20), (10.4, 20.2), (10.7, 20.6), (10.9, 21.0), (10.9, 26)])
    out = track_jitter.analyze(b.build(), AnalyzerContext())
    assert [c["net"] for c in out] == ["SIG"]
    c = out[0]
    assert val(c, "bends") >= 3 and val(c, "span") <= 1.25 and val(c, "window_limit") == 1.25
    assert c["marks"][0]["type"] == "path" and len(c["marks"][0]["targets"]) >= 3


def test_detour_ratio_and_marks(builder):
    b = builder(60, 60)
    # U shaped route: 10 mm direct, 10 + 20 + 10 = 40 mm routed
    b.poly("SIG", [(10, 10), (10, 30), (20, 30), (20, 10)])
    out = track_detour.analyze(b.build(), AnalyzerContext())
    assert len(out) == 1
    c = out[0]
    assert math.isclose(val(c, "routed_length"), 50.0, abs_tol=0.01)
    assert math.isclose(val(c, "direct_length"), 10.0, abs_tol=0.01)
    assert math.isclose(val(c, "ratio"), 5.0, abs_tol=0.01)
    kinds = [m["type"] for m in c["marks"]]
    assert kinds == ["path", "sketch", "link"]
    assert c["marks"][1]["targets"] == ["point:10,10", "point:20,10"]


def test_detour_below_threshold_not_flagged(builder):
    b = builder()
    b.poly("SIG", [(10, 10), (10, 14), (20, 14), (20, 10)])  # 18 mm for 10 mm: extra 8 mm
    assert track_detour.analyze(b.build(), AnalyzerContext()) == []


def test_chain_followed_through_vias_for_detour_and_via_count(builder):
    b = builder(60, 60)
    b.track("USB_D+", (10, 10), (10, 30), layer="F.Cu")
    b.via("USB_D+", 10, 30)
    b.track("USB_D+", (10, 30), (25, 30), layer="B.Cu")
    b.via("USB_D+", 25, 30)
    b.track("USB_D+", (25, 30), (25, 20), layer="F.Cu")
    b.via("USB_D+", 25, 20)
    b.track("USB_D+", (25, 20), (14, 20), layer="B.Cu")
    board = b.build()
    ctx = AnalyzerContext()
    vc = via_count.analyze(board, ctx)
    assert len(vc) == 1 and val(vc[0], "vias") == 3
    # one route through three vias: 20 + 15 + 10 + 11 = 56 mm between (10,10) and (14,20)
    dt = track_detour.analyze(board, ctx)
    assert len(dt) == 1 and math.isclose(val(dt[0], "routed_length"), 56.0, abs_tol=0.01)


def test_via_count_ignores_unimportant_nets(builder):
    b = builder()
    b.track("LED", (10, 10), (10, 20))
    for i in range(3):
        b.via("LED", 10, 20)
    assert via_count.analyze(b.build(), AnalyzerContext()) == []


def test_edge_track_clearance_and_parallel_run(builder):
    b = builder()
    # centre line 0.3 mm from the left edge, 0.2 wide: copper gap 0.2 mm
    b.track("SIG", (0.3, 5), (0.3, 20), width=0.2)
    # 8 mm run 1.0 mm from the bottom edge (y = 40)
    b.track("AUX", (10, 39), (18, 39), width=0.2)
    out = edge_track.analyze(b.build(), AnalyzerContext())
    by_net = {c["net"]: c for c in out}
    assert math.isclose(val(by_net["SIG"], "min_gap"), 0.2, abs_tol=0.01)
    assert math.isclose(val(by_net["SIG"], "parallel_run"), 15.0, abs_tol=0.01)
    assert by_net["AUX"]["kind"] == "parallel"
    assert math.isclose(val(by_net["AUX"], "parallel_run"), 8.0, abs_tol=0.01)
    link = [m for m in by_net["SIG"]["marks"] if m["type"] == "link"][0]
    assert link["value"]["unit"] == "mm" and link["targets"][1] == "point:0,5"


def test_edge_track_far_track_not_flagged(builder):
    b = builder()
    b.track("SIG", (10, 10), (30, 10))
    assert edge_track.analyze(b.build(), AnalyzerContext()) == []


def _decap_board(builder, dx):
    b = builder()
    pins = [(str(i + 1), "GND" if i else "3V3", -2 + i * 0.5, 0) for i in range(4)]
    b.part("U1", "MCU-X", 20, 20, pins, lib="QFN")
    b.part("C1", "100nF", 20 + dx, 25, [("1", "3V3", -0.5, 0), ("2", "GND", 0.5, 0)], lib="C_0402")
    return b


def test_decoupling_distance(builder):
    b = _decap_board(builder, 3.0)
    out = decoupling.analyze(b.build(), AnalyzerContext())
    assert len(out) == 1
    c = out[0]
    # cap power pad (22.5, 25) to U1 pin 1 (18, 20)
    assert math.isclose(val(c, "distance"), math.hypot(4.5, 5.0), abs_tol=0.01)
    assert c["marks"][0]["type"] == "link" and c["marks"][0]["targets"] == ["pad:C1:1", "pad:U1:1"]
    assert c["marks"][0]["value"]["unit"] == "mm"


def test_decoupling_close_cap_ok_and_via_flagged(builder):
    b = builder()
    b.part("U1", "MCU-X", 20, 20, [("1", "3V3", 0, 0), ("2", "GND", 0.5, 0), ("3", "GND", 1, 0), ("4", "SIG", 1.5, 0)], lib="QFN")
    b.part("C1", "100nF", 20, 22, [("1", "3V3", 0, 0), ("2", "GND", 1, 0)], lib="C_0402")
    board = b.build()
    assert decoupling.analyze(board, AnalyzerContext()) == []
    # now join them through a via pair on the back
    b.track("3V3", (20, 20), (20, 21), layer="F.Cu")
    b.via("3V3", 20, 21)
    b.track("3V3", (20, 21), (20, 22), layer="B.Cu")
    b.via("3V3", 20, 22)
    out = decoupling.analyze(b.build(), AnalyzerContext())
    assert len(out) == 1 and val(out[0], "vias") == 2


def test_antenna_keepout_intruders(builder):
    b = builder()
    b.keepout([[10, 0], [30, 0], [30, 10], [10, 10]])
    b.track("ANT_FEED", (15, 2), (25, 2), width=1.0)  # the antenna itself
    b.track("SIG", (12, 5), (28, 5))
    b.via("GND", 20, 8)
    b.track("SIG2", (12, 20), (28, 20))  # outside
    out = antenna_keepout.analyze(b.build(), AnalyzerContext())
    assert len(out) == 1
    c = out[0]
    assert val(c, "tracks") == 1 and val(c, "vias") == 1
    assert math.isclose(val(c, "track_length_inside"), 16.0, abs_tol=0.01)
    assert [m["type"] for m in c["marks"]] == ["region", "object"]
    assert c["marks"][0]["targets"] == ["keepout:k000"]
    assert "track:t000" not in c["marks"][1]["targets"]  # the antenna's own net


def test_return_path_gap_crossing(builder):
    b = builder()
    b.zone("GND", "B.Cu", [[0, 0], [50, 0], [50, 40], [0, 40]], holes=[(20, 0, 22, 40)])
    b.track("USB_D+", (10, 20), (30, 20))
    out = return_path.analyze(b.build(), AnalyzerContext())
    assert len(out) == 1
    c = out[0]
    assert math.isclose(val(c, "gap_length"), 2.0, abs_tol=0.3)
    assert c["marks"][0]["type"] == "path" and c["marks"][1]["type"] == "pin"
    x = float(c["marks"][1]["targets"][0].split(":")[1].split(",")[0])
    assert 20 <= x <= 22


def test_return_path_solid_ground_clean(builder):
    b = builder()
    b.zone("GND", "B.Cu", [[0, 0], [50, 0], [50, 40], [0, 40]])
    b.track("USB_D+", (10, 20), (30, 20))
    b.track("SIG", (10, 22), (30, 22))  # not a fast net: ignored
    assert return_path.analyze(b.build(), AnalyzerContext()) == []


def test_microstrip_closed_form():
    # 3.05 mm over 1.53 mm of er 4.5: the patch sample's feed, about 50 ohm
    z, ee = impedance.microstrip_z0(3.0548, 1.53, 0.035, 4.5)
    assert 46 < z < 52 and 3.2 < ee < 3.7
    # w/h = 1 on er 4.5 is near 70 ohm
    z1, _ = impedance.microstrip_z0(1.0, 1.0, 0.0, 4.5)
    assert 66 < z1 < 72
    # narrower is higher
    assert impedance.microstrip_z0(0.25, 1.53, 0.035, 4.5)[0] > 120
    # centred stripline: 0.25 mm in 0.5 mm total, er 4.5
    assert 35 < impedance.stripline_z0(0.25, 0.5, 0.035, 4.5) < 60


def test_impedance_candidates_and_overlay(builder):
    b = builder()
    b.track("ANT_FEED", (10, 10), (30, 10), width=3.05)  # about 50 ohm: not a candidate
    b.track("RF_OUT", (10, 20), (30, 20), width=0.25)  # far from 50
    b.track("LED", (10, 30), (30, 30), width=0.25)  # not controlled
    board = b.build()
    ctx = AnalyzerContext()
    out = impedance.analyze(board, ctx)
    assert [c["net"] for c in out] == ["RF_OUT"]
    c = out[0]
    assert c["estimate"] is True and val(c, "deviation") > 100 and val(c, "target") == 50
    ov = impedance.overlays(board, ctx)
    assert ov["estimate"] is True and "estimate" in ov["label"].lower()
    items = {i["track"]: i for i in ov["tracks"]}
    assert len(items) == 3
    assert items["track:t000"]["color"] == "#2e9e5b" and items["track:t000"]["controlled"] is True
    assert items["track:t002"]["controlled"] is False


def test_impedance_diff_net_target_halved(builder):
    b = builder()
    b.track("USB_D+", (10, 10), (30, 10), width=0.25)
    out = impedance.analyze(b.build(), AnalyzerContext())
    assert val(out[0], "target") == 45.0
    out = impedance.analyze(b.build(), AnalyzerContext(net_targets={"USB_D+": 100.0}))
    assert val(out[0], "target") == 100.0


def test_rf_path_width_change_layer_jump_and_stub(builder):
    b = builder()
    b.part("U1", "RFIC", 10, 10, [("1", "RF_OUT", 0, 0), ("2", "GND", 1, 0), ("3", "GND", 2, 0)], lib="QFN")
    b.part("J1", "SMA", 40, 10, [("1", "RF_OUT", 0, 0)], lib="SMA")
    b.track("RF_OUT", (10, 10), (20, 10), width=0.3)
    b.track("RF_OUT", (20, 10), (40, 10), width=0.5)
    b.track("RF_OUT", (30, 10), (30, 16), width=0.5)  # open stub off the line
    b.via("RF_OUT", 25, 10)
    out = rf_path.analyze(b.build(), AnalyzerContext())
    assert len(out) == 1
    c = out[0]
    assert val(c, "width_changes") == 1 and val(c, "layer_jumps") == 1 and val(c, "stubs") == 1
    assert math.isclose(val(c, "path_length"), 10 + 20 + 6, abs_tol=0.01)
    types = [m["type"] for m in c["marks"]]
    assert types[0] == "path" and "pin" in types and "object" in types


def test_noise_proximity_with_roles_and_heuristics(builder):
    b = builder()
    b.part("U1", "TPS62160", 10, 10, [("1", "SW", 0, 0), ("2", "GND", 1, 0), ("3", "VIN", 2, 0)], lib="SOT-23-5")
    b.part("U2", "LM75C", 18, 10, [("1", "SDA", 0, 0), ("2", "GND", 1, 0), ("3", "3V3", 2, 0)], lib="VSSOP-8")
    b.part("U3", "LM358", 45, 35, [("1", "AIN", 0, 0), ("2", "GND", 1, 0), ("3", "3V3", 2, 0)], lib="SOIC-8")
    out = noise_proximity.analyze(b.build(), AnalyzerContext())
    assert len(out) == 1
    c = out[0]
    assert c["targets"] == ["ref:U1", "ref:U2"]
    assert math.isclose(val(c, "distance"), 8 - 2.0, abs_tol=0.01)
    assert c["marks"][0]["type"] == "link" and c["marks"][0]["value"]["unit"] == "mm"
    # roles from a brief override the heuristics
    ctx = AnalyzerContext(part_roles={"U1": "digital", "U3": "rf", "U2": "analog"})
    out = noise_proximity.analyze(b.build(), ctx)
    assert out == []  # U3 (rf) is 20+ mm from U2


def test_zones_hull_roles_registered_and_overlap(builder):
    b = builder()
    b.part("U1", "TPS62160", 10, 10, [("1", "SW", 0, 0), ("2", "GND", 1, 0), ("3", "VIN", 2, 0)])
    b.part("L1", "2.2uH", 14, 10, [("1", "SW", 0, 0), ("2", "VOUT", 1, 0)], lib="L_0603")
    b.part("U2", "LM75C", 30, 30, [("1", "SDA", 0, 0), ("2", "GND", 1, 0), ("3", "3V3", 2, 0)])
    board = b.build()
    out = zones.analyze(board, AnalyzerContext())
    titles = [c["title"] for c in out]
    assert "Zone switching" in titles and "Zone analog" in titles
    sw = next(c for c in out if c["title"] == "Zone switching")
    assert sw["marks"][0]["type"] == "region" and sw["marks"][0]["role"] == "switching"
    assert sw["marks"][0]["targets"] == ["zone_role:switching"]
    assert sw["marks"][0]["resolved"][0]["kind"] == "polygon"
    assert board.zone_roles["switching"] == ["ref:L1", "ref:U1"]
    # a brief puts both into one role: no overlap between different roles, one hull
    out = zones.analyze(builder_with_roles(b), AnalyzerContext(part_roles={"U1": "power", "L1": "power", "U2": "power"}))
    assert [c["title"] for c in out] == ["Zone power 1", "Zone power 2"]


def builder_with_roles(b):
    return b.build()


def test_marks_resolve_on_the_board(builder):
    b = builder()
    b.poly("SIG", [(10, 10), (20, 10), (10, 20)])
    board = b.build()
    out = track_angle.analyze(board, AnalyzerContext())
    finalize(board, out)
    for c in out:
        for m in c["marks"]:
            assert m["resolved"], m
        assert board.resolve(c["id"]).kind == "group"


def test_run_all_isolates_failures(builder, monkeypatch):
    b = builder()
    b.poly("SIG", [(10, 10), (20, 10), (10, 20)])

    def boom(board, ctx):
        raise RuntimeError("broken")

    monkeypatch.setattr(track_jitter, "analyze", boom)
    r = run_all(b.build(), AnalyzerContext())
    assert r["status"]["track_jitter"]["status"] == "failed" and "broken" in r["status"]["track_jitter"]["error"]
    assert r["status"]["track_angle"]["status"] == "ok" and r["status"]["track_angle"]["count"] == 1
    assert set(r["status"]) >= {"zones", "impedance", "via_count"}
    assert r["overlays"]["impedance"]["estimate"] is True


def test_thresholds_overridable(builder):
    b = builder()
    b.poly("SIG", [(10, 10), (20, 10), (20, 20)])
    ctx = AnalyzerContext(thresholds={"angle_max_deg": 120.0})
    assert len(track_angle.analyze(b.build(), ctx)) == 1
