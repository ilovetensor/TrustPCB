"""Facts the analyzers must state exactly, on the real nrf52840-node sample.

The expected values come from an independent fact-check of a review of this board, each re-measured here
(from track and pad coordinates, the project file and the DRC output) before it was written down. Where the
check's own figure did not reproduce, the test holds the measured one and says so.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from pathlib import Path

import pytest

from trustpcb.analyzers import AnalyzerContext, drc_summary, run_all
from trustpcb.analyzers import impedance
from trustpcb.analyzers.base import ANALYZER_NAMES
from trustpcb.analyzers.classify import check_role, classify_parts, is_dnp, part_roles_report, symbol_role

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def nrf_run(nrf_board):
    return run_all(nrf_board, AnalyzerContext())


def cands(run, rule):
    return [c for c in run["candidates"] if c["rule"] == rule]


def val(c, name):
    return c["measurements"][name]["value"]


# ------------------------------------------------------------------------------ 1. DRC


@pytest.fixture(scope="module")
def drc_candidates(nrf_board):
    from trustpcb.pcb.baseline import candidates_from_report

    drc = json.loads((FIXTURES / "nrf52840_node_drc.json").read_text())
    out = candidates_from_report(nrf_board, drc, None, default_rules=False)
    totals = drc_summary.annotate(out)
    return out, totals


def test_drc_counts_are_records_and_distinct_things(drc_candidates):
    out, t = drc_candidates
    s = t["drc_shorting_items"]
    # 21 records, but 14 positions (where the first item sits) and 14 net pairs
    assert (s["records"], s["distinct_positions"], s["distinct_net_pairs"]) == (21, 14, 14)
    # the fact-check says 16 net pairs: that is 16 distinct descriptions with the nets in KiCad's order
    # ("nets DECUSB and 3V3" and "nets 3V3 and DECUSB" are one pair); pairs are counted unordered here
    assert ["3V3", "DECUSB"] in s["net_pairs"] and ["DECUSB", "3V3"] not in s["net_pairs"]
    assert t["drc_tracks_crossing"]["records"] == 38 and t["drc_tracks_crossing"]["distinct_positions"] == 18
    # 19 hole-clearance records: 18 distinct, on 11 vias and one plated pad (J2 pad 2)
    h = t["drc_hole_clearance"]
    assert (h["records"], h["distinct_records"], h["duplicate_records"], h["distinct_vias"], h["distinct_pads"]) == (19, 18, 1, 11, 1)
    assert any(p.startswith("pad:J2:") for c in out if c["rule"] == "drc_hole_clearance" for p in c["pad_targets"])
    # 12 clearance records: one via pair is listed twice, so 11
    c = t["drc_clearance"]
    assert (c["records"], c["distinct_records"], c["duplicate_records"]) == (12, 11, 1)


def test_drc_duplicates_are_marked_not_dropped(drc_candidates):
    out, _ = drc_candidates
    assert len([c for c in out if c["rule"] == "drc_clearance"]) == 12  # ids stored reviews cite stay valid
    dups = [c for c in out if c.get("duplicate_of")]
    assert len(dups) == 2
    for d in dups:
        first = next(c for c in out if c["id"] == d["duplicate_of"])
        assert first["rule"] == d["rule"] and sorted(first["targets"]) == sorted(d["targets"])
    again = json.loads(json.dumps(out))
    drc_summary.annotate(again)
    assert again == out  # idempotent
    assert all(c["rule_totals"]["records"] >= 1 for c in out if c["source"] == "kicad")


def test_drc_annotate_version_writes_files(tmp_path, drc_candidates):
    out, _ = drc_candidates
    art = tmp_path / "artifacts"
    art.mkdir()
    plain = [{k: v for k, v in c.items() if k not in ("distinct_key", "duplicate_of", "position_key", "net_pair", "via_targets", "pad_targets", "track_targets", "rule_totals")} for c in out]
    (art / "candidates.json").write_text(json.dumps(plain))
    (art / "baseline.json").write_text(json.dumps({"drc": {"violations": 111}}))
    totals = drc_summary.annotate_version(tmp_path)
    assert totals["drc_shorting_items"]["distinct_positions"] == 14
    assert json.loads((art / "drc_summary.json").read_text())["drc_hole_clearance"]["distinct_vias"] == 11
    assert json.loads((art / "baseline.json").read_text())["drc"]["distinct"]["drc_clearance"]["distinct_records"] == 11
    stored = json.loads((art / "candidates.json").read_text())
    assert len(stored) == len(plain) and any(c.get("duplicate_of") for c in stored)


# ----------------------------------------------------------------------- 2. return_path


def test_return_path_events_per_net(nrf_run):
    rp = cands(nrf_run, "return_path")
    assert len(rp) == 41
    nets = {c["net"] for c in rp}
    assert nets == {"ANT_CHIP", "SWDCLK", "SWDIO", "USB_D+_ESD", "USB_D-_ESD", "USB_D+_MCU", "USB_D-_MCU", "XTAL_A", "XTAL_B"}
    rule = rp[0]["rule_summary"]
    assert rule["gap_events"] == 41 and rule["nets_with_gaps"] == 9
    assert "not signals" in rule["meaning"]
    per_net = {p["net"]: p for p in nrf_run["overlays"]["return_path"]["per_net"]}
    assert per_net["SWDCLK"]["gap_events"] == 7 and per_net["SWDIO"]["gap_events"] == 5
    assert per_net["USB_D-_ESD"]["gap_events"] == 14 and per_net["USB_D+_ESD"]["gap_events"] == 8
    assert sum(p["gap_events"] for p in per_net.values()) == 41
    for c in rp:
        assert c["net_summary"] == per_net[c["net"]]


def test_return_path_merged_span_vs_unsupported_length(nrf_run):
    xtal = [c for c in cands(nrf_run, "return_path") if c["net"] == "XTAL_A"]
    first = max(xtal, key=lambda c: val(c, "gap_length"))
    # the audit: 6.0 mm includes 1.0 mm merge rule bridging; 4.5 mm is the real unsupported length
    assert val(first, "gap_length") == pytest.approx(6.0, abs=0.01)
    assert val(first, "unsupported_length") == pytest.approx(4.5, abs=0.01)
    assert val(first, "bridged_length") == pytest.approx(1.5, abs=0.01)
    assert val(first, "merge_rule") == 1.0
    for c in cands(nrf_run, "return_path"):
        assert val(c, "unsupported_length") + val(c, "bridged_length") == pytest.approx(val(c, "gap_length"), abs=0.35)
        assert val(c, "unsupported_length") <= val(c, "gap_length") + 0.3
    # the longest events the review quoted
    spans = {c["net"]: max(val(x, "gap_length") for x in cands(nrf_run, "return_path") if x["net"] == c["net"]) for c in cands(nrf_run, "return_path")}
    assert spans["SWDIO"] == pytest.approx(7.44, abs=0.01) and spans["SWDCLK"] == pytest.approx(5.74, abs=0.01)


def test_unfilled_zone_hides_gaps_it_does_not_inflate_them(builder):
    from trustpcb.analyzers import return_path
    from trustpcb.pcb.model import Zone

    ring = [[0, 0], [50, 0], [50, 40], [0, 40]]
    filled = builder()
    filled.zone("GND", "B.Cu", ring, holes=[(20, 0, 26, 40)])
    filled.track("USB_D+", (10, 20), (40, 20))
    gap = return_path.analyze(filled.build(), AnalyzerContext())
    assert len(gap) == 1 and val(gap[0], "gap_length") == pytest.approx(6.0, abs=0.3)
    assert gap[0]["reference_from_outline"] is False and "reference_note" not in gap[0]
    # the same pour, unfilled: the outline stands in for the copper, the hole is not known, the gap vanishes
    unfilled = builder()
    unfilled.zones.append(Zone("zone:z000", "GND", ["B.Cu"], {}, False, "", ring))
    unfilled.track("USB_D+", (10, 20), (40, 20))
    assert return_path.analyze(unfilled.build(), AnalyzerContext()) == []
    # and where the outline itself leaves the track uncovered, the candidate says the outline was used
    partial = builder()
    partial.zones.append(Zone("zone:z000", "GND", ["B.Cu"], {}, False, "", [[0, 0], [20, 0], [20, 40], [0, 40]]))
    partial.track("USB_D+", (10, 20), (40, 20))
    out = return_path.analyze(partial.build(), AnalyzerContext())
    assert len(out) == 1 and out[0]["reference_from_outline"] is True
    assert "hide gaps" in out[0]["reference_note"] and "never inflates" in out[0]["reference_note"]
    assert out[0]["rule_summary"]["reference_from_outline_layers"] == ["B.Cu"]


# -------------------------------------------------------------------------- 3. decoupling


def test_decoupling_dec_class_min_max_and_sorted_list(nrf_run, nrf_board):
    dec = cands(nrf_run, "decoupling")
    summary = next(c for c in dec if c["pin_class"] == "DEC")["class_summary"]
    assert summary["count"] == 7
    assert [x["distance_mm"] for x in summary["capacitors"]] == [5.72, 9.04, 9.05, 9.86, 10.76, 12.92, 16.29]
    assert summary["min_mm"] == 5.72 and summary["max_mm"] == 16.29  # not "10 to 16 mm"
    assert {x["capacitor"] for x in summary["capacitors"]} == {"C5", "C6", "C7", "C8", "C9", "C10", "C11"}
    # independent: pad centre to pad centre from the board
    pads = {p.id: p for p in nrf_board.pads}
    for x in summary["capacitors"]:
        cap = next(p for p in nrf_board.pads_of(x["capacitor"]) if p.net == x["net"])
        pin = next(p for p in nrf_board.pads_of(x["ic"]) if p.number == x["ic_pin"])
        assert math.dist((cap.x, cap.y), (pin.x, pin.y)) == pytest.approx(x["distance_mm"], abs=0.006)
    assert nrf_run["overlays"]["decoupling"]["pin_classes"]["DEC"]["max_mm"] == 16.29


def test_decoupling_leaves_esd_and_dnp_parts_out(nrf_run):
    dec = cands(nrf_run, "decoupling")
    assert not any(c["ic"] == "U1" for c in dec)  # U1 is a USBLC6-2SC6 ESD array
    assert len(dec) == 15  # C1 and C2 stay, now measured to the LDO input instead of the ESD array
    excluded = {e["ref"]: e["reason"] for e in nrf_run["overlays"]["decoupling"]["excluded"]}
    assert "protection" in excluded["U1"] and "not fitted" in excluded["C19"]
    # C1 and C2 (VBUS_F) are now measured to the next supply pin, the LDO input
    assert {c["capacitor"]: c["ic"] for c in dec if c["net"] == "VBUS_F"} == {"C1": "U2", "C2": "U2"}


def test_decoupling_protection_part_excluded_on_a_hand_built_board(builder):
    from trustpcb.analyzers import decoupling

    b = builder()
    b.part("U1", "TPD4E05U06", 20, 20, [("1", "3V3", 0, 0), ("2", "GND", 1, 0), ("3", "D1", 2, 0), ("4", "D2", 3, 0)], lib="SOT-23-6")
    b.part("C1", "100nF", 30, 20, [("1", "3V3", 0, 0), ("2", "GND", 1, 0)], lib="C_0402")
    assert decoupling.analyze(b.build(), AnalyzerContext()) == []
    # the same cap beside a part the symbol does not call protection is measured
    b2 = builder()
    b2.part("U1", "XR9000", 20, 20, [("1", "3V3", 0, 0), ("2", "GND", 1, 0), ("3", "D1", 2, 0), ("4", "D2", 3, 0)], lib="SOT-23-6")
    b2.part("C1", "100nF", 30, 20, [("1", "3V3", 0, 0), ("2", "GND", 1, 0)], lib="C_0402")
    assert len(decoupling.analyze(b2.build(), AnalyzerContext())) == 1


# --------------------------------------------------------------------------- 4. impedance


def test_impedance_width_for_target_and_stackup_assumptions(nrf_run, nrf_board):
    ant = [c for c in cands(nrf_run, "impedance") if c["net"] == "ANT_CHIP"]
    assert len(ant) == 2
    for c in ant:
        w = val(c, "width_for_target")
        assert w == pytest.approx(2.835, abs=0.002)  # the check quoted about 2.8 to 2.9 mm
        z, _ = impedance.microstrip_z0(w, 1.53, 0.035, 4.5)
        assert z == pytest.approx(50.0, abs=0.05)
        a = c["stackup_assumptions"]
        assert a["epsilon_r"] == 4.5 and a["dielectric_height_mm"] == 1.53 and a["copper_thickness_mm"] == 0.035
        assert a["stackup_assumed"] is True and a["reference_layers"] == ["B.Cu"]
        assert "solid ground plane on B.Cu" in a["reference_plane_assumed"]


def test_impedance_reads_the_project_rf_net_class(nrf_run):
    c = next(c for c in cands(nrf_run, "impedance") if c["net"] == "ANT_CHIP")
    assert c["net_class"]["name"] == "RF" and c["net_class"]["track_width_mm"] == 3.0548
    assert val(c, "class_track_width") == 3.0548
    assert 45 < val(c, "class_width_z0_estimate") < 50  # the class width is about 48 ohm on this stackup, near its 50 ohm aim


def test_impedance_flags_missing_reference_plane_under_ant_chip(nrf_run, nrf_board):
    from shapely.geometry import LineString

    c = next(c for c in cands(nrf_run, "impedance") if c["net"] == "ANT_CHIP")
    assert c["reference_plane_missing"] is True
    ref = c["net_reference"]
    assert ref["reference_layers"] == ["B.Cu"] and ref["length_mm"] == pytest.approx(9.10, abs=0.01)
    # the check said 3.9 of 9.1 mm; sampled every 0.25 mm that is 3.86, and the exact line-in-polygon length is 3.76
    assert ref["unsupported_mm"] == pytest.approx(3.9, abs=0.1)
    gnd = [z for z in nrf_board.zones if z.net == "GND" and "B.Cu" in z.filled][0]
    from shapely.geometry import Polygon
    from shapely.ops import unary_union

    ground = unary_union([Polygon(r).buffer(0) for r in gnd.filled["B.Cu"]])
    exact = sum(LineString(t.points()).difference(ground).length for t in nrf_board.tracks if t.net == "ANT_CHIP")
    assert ref["unsupported_mm"] == pytest.approx(exact, abs=0.2)


# ------------------------------------------------------------------------ 5. connector_access


def test_connector_access_counts_the_mounting_hole(nrf_run):
    (c,) = cands(nrf_run, "connector_access")
    assert val(c, "blockers") == 4 and val(c, "mounting_holes_in_path") == 1 and val(c, "objects_in_path") == 5
    refs = {b["ref"]: b for b in c["in_path"]}
    assert {"U2", "C4", "C3", "SW1"} <= set(refs) and refs["H2"]["kind"] == "mounting_hole"
    assert "ref:H2" in c["targets"]
    assert refs["H2"]["from_mouth_mm"] == pytest.approx(21.4, abs=0.1)  # hole edge; its centre (61, 47) is 24.85 mm from the mouth
    corridor = c["corridor"]
    assert corridor["width_mm"] == pytest.approx(10.64, abs=0.01) and corridor["plug_envelope_known"] is False
    assert "courtyard width" in corridor["basis"] and "not a plug" in corridor["basis"]
    assert val(c, "corridor_width") == corridor["width_mm"]


# ------------------------------------------------------------------------------ 6. rf_path


def test_rf_path_separates_feed_from_radiating_length(nrf_run, nrf_board):
    (c,) = cands(nrf_run, "rf_path")
    assert val(c, "path_length") == pytest.approx(40.14, abs=0.01)
    assert val(c, "radiating_length") == pytest.approx(26.36, abs=0.01)
    assert val(c, "feed_length") == pytest.approx(13.78, abs=0.01)
    assert val(c, "feed_length") + val(c, "radiating_length") == pytest.approx(val(c, "path_length"), abs=0.01)
    by_net = {b["net"]: b for b in c["net_breakdown"]}
    assert by_net["ANT_FEED"]["length_mm"] == pytest.approx(31.04, abs=0.01)
    assert by_net["ANT_FEED"]["feed_mm"] == pytest.approx(4.68, abs=0.01)
    assert by_net["ANT_FEED"]["radiating_mm"] == pytest.approx(26.36, abs=0.01)
    assert by_net["ANT_CHIP"]["radiating_mm"] == 0.0
    # independent: the 1.0 mm tracks of ANT_FEED are the meander
    meander = sum(t.length for t in nrf_board.tracks if t.net == "ANT_FEED" and t.width == 1.0)
    assert meander == pytest.approx(26.36, abs=0.01)


def test_rf_path_counts_dnp_pads_as_stubs(nrf_run, nrf_board):
    (c,) = cands(nrf_run, "rf_path")
    assert val(c, "stubs") == 2 and val(c, "dnp_stubs") == 2 and val(c, "open_end_stubs") == 0
    assert {d["ref"] for d in c["dnp_stub_pads"]} == {"C19", "C20"}
    assert all(is_dnp(nrf_board.footprint(r)) for r in ("C19", "C20")) and not nrf_board.footprint("C19").dnp  # only the value says so


# ------------------------------------------------------------------------ 7. via_annular_ring


def test_via_annular_ring_flags_75_of_102(nrf_board, nrf_run):
    assert "via_annular_ring" in ANALYZER_NAMES
    assert len(nrf_board.vias) == 102 and sum(1 for v in nrf_board.vias if (v.diameter, v.drill) == (0.35, 0.2)) == 75
    (c,) = cands(nrf_run, "via_annular_ring")
    assert val(c, "vias") == 75 and val(c, "vias_total") == 102
    assert val(c, "annular_ring") == pytest.approx(0.075) and val(c, "pad_diameter") == 0.35 and val(c, "drill") == 0.2
    assert val(c, "threshold") == 0.127 and val(c, "project_min_annular") == 0.07  # DRC is silent: 0.075 >= 0.07
    assert len(c["targets"]) == 75 and "oshpark" in c["threshold_source"].lower()


def test_via_annular_ring_threshold_is_configurable(nrf_board):
    from trustpcb.analyzers import via_annular_ring

    assert via_annular_ring.analyze(nrf_board, AnalyzerContext(thresholds={"via_min_annular_mm": 0.07})) == []
    out = via_annular_ring.analyze(nrf_board, AnalyzerContext(thresholds={"via_min_annular_mm": 0.2}))
    assert sorted(val(c, "vias") for c in out) == [27, 75]


# ----------------------------------------------------------------------------- 8. part roles


def test_symbol_value_overrules_a_wrong_role(nrf_board):
    assert check_role("U1", "regulator", nrf_board)["ok"] is False
    assert check_role("U1", "protection", nrf_board)["ok"] is True
    assert check_role("U2", "regulator", nrf_board)["ok"] is True
    assert check_role("U2", "sensor", nrf_board)["ok"] is False
    assert check_role("U4", "sensor", nrf_board)["ok"] is True
    assert check_role("D1", "protection", nrf_board)["ok"] is False and check_role("D1", "status_led", nrf_board)["ok"] is True
    assert check_role("U3", "radio", nrf_board)["ok"] is True
    assert check_role("C1", "decoupling", nrf_board)["ok"] is None  # a 1uF value does not decide a role
    assert check_role("U9", "x", nrf_board)["ok"] is None
    msg = check_role("U1", "regulator", nrf_board)["message"]
    assert "USBLC6-2SC6" in msg and "esd protection" in msg
    assert symbol_role(nrf_board.footprint("U2"))["family"] == "ldo_regulator"


def test_brief_roles_do_not_beat_the_symbol(nrf_board):
    ctx = AnalyzerContext(part_roles={"U1": "regulator", "U2": "sensor", "D1": "protection", "U4": "sensor"})
    roles = classify_parts(nrf_board, ctx)
    assert (roles["U1"], roles["U2"], roles["D1"], roles["U4"]) == ("protection", "power", "digital", "sensor")
    assert set(ctx.cache["role_conflicts"]) == {"U1", "U2", "D1"}
    rep = part_roles_report(nrf_board, ctx)
    assert rep["U1"]["conflict"] is True and rep["U1"]["source"] == "symbol" and rep["U1"]["brief_role"] == "regulator"
    assert rep["U4"]["conflict"] is False and rep["U4"]["source"] == "brief"


def test_run_for_version_reports_roles_and_drc_summary(tmp_path, nrf_board, drc_candidates):
    from trustpcb.analyzers import run_for_version

    art = tmp_path / "artifacts"
    art.mkdir()
    (art / "board.json").write_text(json.dumps(nrf_board.to_dict()))
    (art / "candidates.json").write_text(json.dumps(drc_candidates[0]))
    res = run_for_version(tmp_path, AnalyzerContext(part_roles={"U1": "regulator"}))
    assert res["part_roles"]["U1"]["conflict"] is True
    assert res["drc_summary"]["drc_shorting_items"]["distinct_positions"] == 14


# ---------------------------------------------------------------------- 9. net class widths


def test_net_class_width_candidates(nrf_run):
    assert "netclass_width" in ANALYZER_NAMES
    by_net = {c["net"]: c for c in cands(nrf_run, "netclass_width")}
    assert set(by_net) == {"ANT_CHIP", "ANT_FEED", "VBUS_RAW"}
    chip = by_net["ANT_CHIP"]
    assert val(chip, "class_track_width") == 3.0548 and chip["net_class"]["name"] == "RF"
    assert [w["width_mm"] for w in chip["narrow_by_width"]] == [0.15, 0.3]
    assert [w["width_mm"] for w in by_net["ANT_FEED"]["narrow_by_width"]] == [0.3]  # the 1.0 mm meander is the antenna, left out
    vbus = by_net["VBUS_RAW"]
    assert vbus["net_class"]["name"] == "POWER" and val(vbus, "class_track_width") == 0.5
    assert vbus["narrow_by_width"][0]["width_mm"] == 0.25


def test_net_class_width_needs_a_project_file(builder):
    from trustpcb.analyzers import netclass_width

    b = builder()
    b.track("SIG", (1, 1), (9, 1))
    assert netclass_width.analyze(b.build(), AnalyzerContext()) == []
