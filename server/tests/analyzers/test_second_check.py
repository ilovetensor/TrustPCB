"""Facts from the second independent fact-check of a review, held exactly.

Each expectation is a value the check measured on the real nrf52840-node and usb-ldo-led sample boards, or an exact sentence the check found wrong.
"""
from __future__ import annotations

import math
from pathlib import Path

import pytest

from trustpcb.analyzers import AnalyzerContext, decoupling, noise_proximity, run_all, zones
from trustpcb.analyzers import schematic as sch
from trustpcb.analyzers.classify import role_basis
from trustpcb.pcb.model import parse_board

SAMPLES = Path(__file__).resolve().parents[3] / "samples"
LDO_PCB = SAMPLES / "usb-ldo-led" / "usb_ldo_led_node.kicad_pcb"
needs_ldo = pytest.mark.skipif(not LDO_PCB.is_file(), reason="the usb-ldo-led sample is not in this checkout")


@pytest.fixture(scope="module")
def nrf_run(nrf_board):
    return run_all(nrf_board, AnalyzerContext())


@pytest.fixture(scope="module")
def ldo_board():
    return parse_board(LDO_PCB, "v-ldo")


def cands(run, rule):
    return [c for c in run["candidates"] if c["rule"] == rule]


# ---- 1. decoupling: the supply pin, by its name ----------------------------------------------------------------------------


def test_c1_and_c2_are_measured_to_the_regulator_input_not_to_its_enable_pin(nrf_run, nrf_board):
    """The check: 'C1 and C2 at U2 pin 3' names pin 3, which is EN. VIN is pin 1 (21.31 and 18.98 mm)."""
    dec = {c["capacitor"]: c for c in cands(nrf_run, "decoupling")}
    for ref, mm in (("C1", 21.31), ("C2", 18.98)):
        c = dec[ref]
        assert (c["ic"], c["ic_pin"], c["ic_pin_name"]) == ("U2", "1", "VIN")
        assert c["measurements"]["distance"]["value"] == pytest.approx(mm, abs=0.01)
        assert "U2 pin 1 VIN" in c["title"] and "pin 3" not in c["title"]
        assert any("U2 pin 3 (EN)" in d and "not a supply pin" in d for d in c["details"])
    assert nrf_board._pad["pad:U2:3"].net == "VBUS_F"          # EN is tied to the input net, which is why the nearest pad was the wrong one


def test_every_decoupling_candidate_names_its_pin_and_the_dec_distances_are_unchanged(nrf_run):
    dec = cands(nrf_run, "decoupling")
    assert len(dec) == 15 and all(c["ic_pin_name"] for c in dec)
    summary = next(c for c in dec if c["pin_class"] == "DEC")["class_summary"]
    assert [x["distance_mm"] for x in summary["capacitors"]] == [5.72, 9.04, 9.05, 9.86, 10.76, 12.92, 16.29]
    assert {x["ic_pin_name"] for x in summary["capacitors"]} == {"DEC1", "DEC2", "DEC3", "DEC4", "DEC5", "DEC6", "DECUSB"}


@needs_ldo
def test_the_no_radio_sample_measures_c1_to_vin_and_c2_c3_to_vout(ldo_board):
    """The same fault in the no-radio review: 'move C1 next to U1 pin 3' (EN). VIN is pin 1, 6.41 mm away."""
    out = {c["capacitor"]: c for c in decoupling.analyze(ldo_board, AnalyzerContext())}
    assert (out["C1"]["ic_pin"], out["C1"]["ic_pin_name"]) == ("1", "VIN") and out["C1"]["measurements"]["distance"]["value"] == pytest.approx(6.41, abs=0.01)
    assert (out["C2"]["ic_pin"], out["C2"]["ic_pin_name"]) == ("5", "VOUT") and (out["C3"]["ic_pin"], out["C3"]["ic_pin_name"]) == ("5", "VOUT")
    assert "pin 3" not in " ".join(c["title"] for c in out.values())


SCHEMATIC = """(kicad_sch (version 20250114) (generator "test")
  (lib_symbols
    (symbol "L:REG" (symbol "REG_1_1"
      (pin power_in line (at -7.62 2.54 0) (length 2.54) (name "VIN") (number "1"))
      (pin power_in line (at 0 -7.62 90) (length 2.54) (name "GND") (number "2"))
      (pin input line (at -7.62 0 0) (length 2.54) (name "EN") (number "3"))
      (pin no_connect line (at 5.08 0 180) (length 2.54) (name "NC") (number "4"))
      (pin power_out line (at 7.62 2.54 180) (length 2.54) (name "VOUT") (number "5"))))
  )
  (symbol (lib_id "L:REG") (at 100 100 0) (unit 1) (dnp no)
    (property "Reference" "U2" (at 0 0 0)) (property "Value" "REG-X" (at 0 0 0)))
  (symbol (lib_id "L:REG") (at 150 100 0) (unit 1) (dnp yes)
    (property "Reference" "U9" (at 0 0 0)) (property "Value" "REG-Y" (at 0 0 0)))
  (no_connect (at 92.38 97.46) (uuid "x"))
)
"""


def _regulator_board(builder, tmp_path, with_schematic=True):
    b = builder()
    # pin 3 (EN) sits nearer the capacitor than pin 1 (VIN); both are on the input net
    b.part("U2", "REG-X", 20, 20, [("1", "VBUS_F", -3, 0), ("2", "GND", 0, 2), ("3", "VBUS_F", -1, 0), ("5", "3V3", 3, 0)], lib="SOT-23-5")
    b.part("C1", "10uF", 30, 20, [("1", "VBUS_F", 0.5, 0), ("2", "GND", -0.5, 0)], lib="C_0402")
    board = b.build()
    board.source = str(tmp_path / "x.kicad_pcb")
    if with_schematic:
        (tmp_path / "x.kicad_sch").write_text(SCHEMATIC)
    return board


def test_a_hand_built_board_with_a_schematic_prefers_the_named_supply_pin(builder, tmp_path):
    board = _regulator_board(builder, tmp_path)
    (c,) = decoupling.analyze(board, AnalyzerContext())
    assert (c["ic_pin"], c["ic_pin_name"]) == ("1", "VIN") and "U2 pin 1 VIN" in c["title"]
    assert math.isclose(c["measurements"]["distance"]["value"], 13.5, abs_tol=0.01)


def test_without_a_schematic_the_nearest_pad_is_used_and_its_function_is_not_claimed(builder, tmp_path):
    board = _regulator_board(builder, tmp_path, with_schematic=False)
    (c,) = decoupling.analyze(board, AnalyzerContext())
    assert c["ic_pin"] == "3" and c["ic_pin_name"] is None
    assert "pin 3 (" in c["title"] and "EN" not in c["title"] and "VIN" not in c["title"].split("(")[0]
    assert any("No schematic pin name" in d and "not known" in d for d in c["details"])


# ---- the schematic reader ----------------------------------------------------------------------------------------------------


def test_schematic_pin_positions_match_the_markers(builder, tmp_path):
    info = sch.parse_schematic(SCHEMATIC)
    assert info.pins[("U2", "4")] == {"name": "NC", "type": "no_connect", "x": 105.08, "y": 100.0}
    assert info.dnp == {"U2": False, "U9": True}
    assert info.nc_pins == {("U2", "1")}                 # the marker at (92.38, 97.46) is on pin 1 (VIN) of U2
    assert info.is_supply_pin("U2", "1") and info.is_supply_pin("U2", "5") and info.is_supply_pin("U2", "3") is False and info.is_supply_pin("U2", "9") is None


def test_every_marker_in_the_samples_sits_on_a_pin():
    for name in ("nrf52840-node/nrf52840_rf_iot_node", "usb-ldo-led/usb_ldo_led_node"):
        path = SAMPLES / f"{name}.kicad_sch"
        if not path.is_file():
            continue
        info = sch.parse_schematic(path.read_text())
        on_pins = {(round(p["x"], 2), round(p["y"], 2)) for p in info.pins.values()}
        assert info.no_connect and all((round(x, 2), round(y, 2)) in on_pins for x, y in info.no_connect), name


def test_the_nrf_schematic_marks_both_c19_and_c20_do_not_populate(nrf_board):
    info = sch.schematic_info(nrf_board)
    assert info.found and {r for r, v in info.dnp.items() if v} == {"C19", "C20"}
    assert info.pin_name("U2", "3") == "EN" and info.pin_name("U2", "1") == "VIN"


@needs_ldo
def test_the_no_radio_schematic_already_has_the_no_connect_markers(ldo_board):
    """The check: 'mark them as no-connect' when the schematic already has six markers (J1 A6 B6 A7 B7 A8 B8) and U1 pin 4 is an NC pin."""
    info = sch.schematic_info(ldo_board)
    assert info.nc_numbers("J1") == ["A6", "A7", "A8", "B6", "B7", "B8"]
    assert info.pins[("U1", "4")]["type"] == "no_connect" and ("U1", "4") not in info.nc_pins


@needs_ldo
def test_parity_records_carry_what_the_schematic_already_has(ldo_board):
    records = [{"id": f"candidate:parity_net_conflict:{i}", "rule": "parity_net_conflict", "targets": [t]} for i, t in enumerate(["pad:J1:A6", "pad:U1:4", "pad:J1:A1"], 1)]
    assert sch.annotate_parity(records, ldo_board, AnalyzerContext()) == 2
    assert "already has a no_connect marker on J1 pin A6 (D+)" in records[0]["schematic_state"]["text"]
    assert "already declares U1 pin 4 (NC) as no-connect" in records[1]["schematic_state"]["text"]
    assert "schematic_state" not in records[2] and sch.annotate_parity(records, ldo_board, AnalyzerContext()) == 0         # a pad that is not marked says nothing; running again changes nothing


def test_a_dnp_parity_record_lists_every_do_not_populate_part(nrf_board):
    record = [{"id": "candidate:parity_footprint_symbol_mismatch:1", "rule": "parity_footprint_symbol_mismatch", "targets": ["ref:C19"], "details": ["Footprint C19"]}]
    sch.annotate_parity(record, nrf_board, AnalyzerContext())
    assert record[0]["schematic_state"]["text"] == "The schematic marks C19 and C20 do-not-populate."


# ---- 2. netclass_width carries the facts the review needs --------------------------------------------------------------------


def test_the_rf_class_candidate_carries_the_target_the_error_and_the_stackup(nrf_run):
    rf = [c for c in cands(nrf_run, "netclass_width") if c["net_class"]["name"] == "RF"]
    assert {c["net"] for c in rf} == {"ANT_CHIP", "ANT_FEED"}
    for c in rf:
        m = c["measurements"]
        assert m["class_track_width"]["value"] == pytest.approx(3.0548, abs=1e-4) and m["class_width_z0_estimate"]["value"] == pytest.approx(47.8, abs=0.1)
        assert m["width_for_target"]["value"] == pytest.approx(2.835, abs=0.002) and m["target_impedance"]["value"] == 50.0
        assert m["class_width_error_pct"]["value"] == pytest.approx(4.4, abs=0.2)
        assert c["stackup_assumptions"]["epsilon_r"] == 4.5 and c["stackup_assumptions"]["dielectric_height_mm"] == pytest.approx(1.53)
    power = [c for c in cands(nrf_run, "netclass_width") if c["net_class"]["name"] != "RF"]
    assert power and all("target_impedance" not in c["measurements"] for c in power)


# ---- 7. zones: a part and its own bus --------------------------------------------------------------------------------------


def test_r5_and_r6_are_the_sensors_own_pull_ups_and_the_overlap_says_so(nrf_run, nrf_board):
    basis = role_basis(nrf_board, AnalyzerContext())
    assert basis["R5"]["follows"] == "U4" and basis["R5"]["via_nets"] == ["I2C_SDA"] and basis["R6"]["follows"] == "U4" and basis["R6"]["via_nets"] == ["I2C_SCL"]
    overlap = next(c for c in cands(nrf_run, "zones") if c["title"] == "Zones analog and digital overlap")
    line = next(d for d in overlap["details"] if d.startswith("analog zone"))
    assert "R5" in line and "R6" in line and "follow U4" in line and "own connected parts" in line and "not separate analog parts" in line
    assert "R5" in overlap["zone_parts"]["analog"] and "R5" not in overlap["zone_parts"]["digital"]


def test_noise_proximity_ignores_a_part_and_the_bus_parts_wired_to_it(builder):
    """A passive wired to a sensor by a signal net is the sensor's own pull-up: even if the brief calls it a noise source there is no noise question between them."""
    b = builder()
    b.part("U4", "LM75C", 20, 20, [("1", "SDA", 0, 0), ("2", "SCL", 1, 0), ("8", "3V3", 2, 0)], lib="SOIC-8", size=2.0)
    b.part("R5", "10k", 23, 20, [("1", "SDA", 0, 0), ("2", "3V3", 1, 0)], lib="R_0402")
    ctx = AnalyzerContext(part_roles={"R5": "rf", "U4": "sensor"})
    assert noise_proximity.analyze(b.build(), ctx) == []
    other = builder()
    other.part("U4", "LM75C", 20, 20, [("1", "SDA", 0, 0), ("8", "3V3", 2, 0)], lib="SOIC-8", size=2.0)
    other.part("R5", "10k", 23, 20, [("1", "OTHER", 0, 0), ("2", "GND", 1, 0)], lib="R_0402")
    assert len(noise_proximity.analyze(other.build(), AnalyzerContext(part_roles={"R5": "rf", "U4": "sensor"}))) == 1       # an unrelated rf part beside it still counts


def test_a_part_the_brief_names_still_anchors_its_own_pull_ups(nrf_board):
    """With the brief's roles (U4 sensor) R5 and R6 used to fall to the regulator through the shared 3V3 and land in a power zone. They follow the sensor."""
    ctx = AnalyzerContext(part_roles={"U3": "radio", "U4": "sensor", "J1": "connector"})
    from trustpcb.analyzers.classify import classify_parts

    roles = classify_parts(nrf_board, ctx)
    assert roles["U4"] == roles["R5"] == roles["R6"] == "sensor"
    assert role_basis(nrf_board, ctx)["R5"] == {"follows": "U4", "via_nets": ["I2C_SDA"], "signal": True}


def test_zone_membership_lines_name_what_each_passive_follows(nrf_board):
    ctx = AnalyzerContext()
    cands_ = zones.analyze(nrf_board, ctx)
    assert any(d.startswith("rf zone:") and "follow U3" in d for c in cands_ for d in c.get("details") or [])
