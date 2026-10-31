from __future__ import annotations

from pathlib import Path

from trustpcb.intake.detect import detect, find_project
from trustpcb.intake.stackup import apply_overrides, assumptions, confirmation
from trustpcb.pcb.model import parse_board_text

SAMPLES = Path(__file__).resolve().parents[3] / "samples"


def test_nrf_detected_as_2g4_trace_antenna(nrf_board):
    d = detect(nrf_board, SAMPLES / "nrf52840-node" / "nrf52840_rf_iot_node.kicad_sch")
    assert d.wireless and d.confidence == "high" and not d.ambiguous
    assert (d.band["low_ghz"], d.band["high_ghz"]) == (2.4, 2.4835)
    assert d.parts[0]["ref"] == "U3" and "nRF" in d.parts[0]["family"]
    a = d.antenna
    assert a["type"] == "trace" and a["shape"] == "meander" and a["net"] == "ANT_FEED"
    assert a["in_keepout"] and a["bends"] >= 10 and 25 < a["length_mm"] < 40
    assert a["open_ends"] and a["feed_point"] == [32.485, 12.8]
    kinds = {e.kind for e in d.evidence}
    assert {"part", "net", "keepout", "antenna_shape"} <= kinds
    # evidence points at real targets
    for e in d.evidence:
        for t in e.targets:
            nrf_board.resolve(t)


def test_patch_detected_with_band_guess(patch_board):
    d = detect(patch_board)
    assert d.wireless and d.antenna["type"] == "patch" and d.antenna["net"] == "ANTENNA_50R"
    assert 2.3 < d.antenna["estimated_f0_ghz"] < 2.6
    assert d.band["low_ghz"] == 2.4 and d.band["confidence"] == "low"


def test_non_wireless_board():
    text = """(kicad_pcb (version 20240108) (layers (0 "F.Cu" signal) (2 "B.Cu" signal))
 (segment (start 0 0) (end 10 0) (width 0.25) (layer "F.Cu") (net "VCC") (uuid "aaaaaaaa-0000-0000-0000-000000000001"))
 (footprint "R:R" (layer "F.Cu") (uuid "bbbbbbbb-0000-0000-0000-000000000001") (at 5 5)
   (property "Reference" "R1" (at 0 0 0)) (property "Value" "10k" (at 0 1 0))
   (pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu") (net "VCC"))))"""
    d = detect(parse_board_text(text))
    assert not d.wireless and d.band is None and d.antenna is None and d.confidence == "none"


def test_esp32_module_part_detected():
    text = """(kicad_pcb (version 20240108) (layers (0 "F.Cu" signal) (2 "B.Cu" signal))
 (footprint "RF_Module:ESP32-WROOM-32" (layer "F.Cu") (uuid "bbbbbbbb-0000-0000-0000-000000000001") (at 5 5)
   (property "Reference" "U1" (at 0 0 0)) (property "Value" "ESP32-WROOM-32" (at 0 1 0))
   (pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu"))))"""
    d = detect(parse_board_text(text))
    assert d.wireless and d.band["label"].startswith("2.4 GHz") and d.antenna["type"] == "module"


def test_find_project(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "x.kicad_pcb").write_text("(kicad_pcb)")
    (tmp_path / "sub" / "x.kicad_pro").write_text("{}")
    (tmp_path / "other.kicad_pcb").write_text("(kicad_pcb)")
    info = find_project(tmp_path)
    assert info["pcb"] == "sub/x.kicad_pcb" and info["has_project_file"] and not info["has_schematic"]
    assert info["other_boards"] == ["other.kicad_pcb"]


def test_stackup_confirmation_and_assumptions(nrf_board):
    c = confirmation(nrf_board)
    assert c["needs_confirmation"] and c["source"] == "default" and c["copper_layers"] == 2
    ids = {a["id"] for a in c["assumptions"]}
    assert "stackup.default" in ids and "stackup.dielectric 1.epsilon_r" in ids
    assert all(a["source"] == "default" for a in c["assumptions"])
    s = apply_overrides(nrf_board.stackup, {"epsilon_r": 3.48, "loss_tangent": 0.0037, "material": "Rogers RO4350B", "total_thickness_mm": 0.8})
    d = [x for x in s["layers"] if x["kind"] == "dielectric"][0]
    assert s["assumed"] is False and s["source"] == "user" and d["epsilon_r"] == 3.48
    assert abs(sum(x["thickness_mm"] for x in s["layers"]) - 0.8) < 1e-3
    assert nrf_board.stackup["assumed"] is True  # original untouched
    assert any(a["source"] == "user" for a in assumptions(nrf_board, s))


def test_usb_regulator_sample_parses_and_is_not_wireless():
    from trustpcb.rf.setup import plan_setup

    board = parse_board_text((SAMPLES / "usb-ldo-led" / "usb_ldo_led_node.kicad_pcb").read_text())
    assert len(board.footprints) == 15 and [round(v) for v in board.bbox] == [0, 0, 40, 30]
    # every schematic pin that is left open has its own unconnected-(...) net, which keeps KiCad's parity check quiet
    assert {p.net for p in board.pads if (p.net or "").startswith("unconnected-")} >= {"unconnected-(U1-NC-Pad4)", "unconnected-(J1-D+-PadA6)"}
    d = detect(board, SAMPLES / "usb-ldo-led" / "usb_ldo_led_node.kicad_sch")
    assert not d.wireless and d.antenna is None and d.band is None
    setup = plan_setup(board, d)
    assert setup.simulatable is False and setup.code == "not_wireless"
