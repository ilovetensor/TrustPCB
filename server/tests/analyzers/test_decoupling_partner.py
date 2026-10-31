"""Which part's supply pin a decoupling capacitor is measured against when several parts share one rail."""
from __future__ import annotations

from trustpcb.analyzers import AnalyzerContext, decoupling


def _two_parts(builder, cap_x):
    b = builder()
    # U1 (the capacitor's intended part) and U2 share the 3V3 rail; the capacitor sits beside U2 after a "move"
    b.part("U1", "MCU-X", 10, 10, [("1", "3V3", 0, 0), ("2", "GND", 0.5, 0), ("3", "GND", 1, 0), ("4", "SIG", 1.5, 0)], lib="QFN")
    b.part("U2", "FLASH-Y", 30, 10, [("1", "3V3", 0, 0), ("2", "GND", 0.5, 0), ("3", "GND", 1, 0), ("4", "SIG2", 1.5, 0)], lib="SOIC")
    b.part("C1", "100nF", cap_x, 11.5, [("1", "3V3", -0.5, 0), ("2", "GND", 0.5, 0)], lib="C_0402")
    return b.build()


def _rows(board, ctx):
    return decoupling.measure(board, ctx)["rows"]


def test_nearest_pad_is_not_called_the_partner_when_two_parts_share_the_rail(builder):
    board = _two_parts(builder, 29.0)             # beside U2
    r = _rows(board, AnalyzerContext())[0]
    assert r["pin"].ref == "U2"                   # still the nearest candidate ...
    assert r["partner"]["determined"] is False    # ... but it is not claimed to be the partner
    assert set(r["partner"]["parts"]) == {"U1", "U2"} and "which one" in r["partner"]["basis"]


def test_part_roles_name_the_load_when_the_other_part_supplies_the_rail(builder):
    board = _two_parts(builder, 29.0)
    ctx = AnalyzerContext(part_roles={"U1": "mcu", "U2": "regulator"})
    r = _rows(board, ctx)[0]
    assert r["pin"].ref == "U1" and r["partner"]["determined"] is True and "part roles" in r["partner"]["basis"]
    # the capacitor is far from U1 now, so that is what gets reported
    out = decoupling.analyze(board, ctx)
    assert out and out[0]["ic"] == "U1" and out[0]["partner_determined"] is True


def test_undetermined_partner_is_said_in_the_finding(builder):
    b = builder()
    b.part("U1", "MCU-X", 10, 10, [("1", "3V3", 0, 0), ("2", "GND", 0.5, 0), ("3", "GND", 1, 0), ("4", "SIG", 1.5, 0)], lib="QFN")
    b.part("U2", "FLASH-Y", 30, 10, [("1", "3V3", 0, 0), ("2", "GND", 0.5, 0), ("3", "GND", 1, 0), ("4", "SIG2", 1.5, 0)], lib="SOIC")
    b.part("C1", "100nF", 20, 30, [("1", "3V3", -0.5, 0), ("2", "GND", 0.5, 0)], lib="C_0402")   # far from both
    out = decoupling.analyze(b.build(), AnalyzerContext())
    assert out and out[0]["partner_determined"] is False
    assert any("cannot be determined" in d for d in out[0]["details"])


def test_a_single_part_on_the_net_is_the_partner(builder):
    b = builder()
    b.part("U1", "MCU-X", 20, 20, [("1", "3V3", 0, 0), ("2", "GND", 0.5, 0), ("3", "GND", 1, 0), ("4", "SIG", 1.5, 0)], lib="QFN")
    b.part("C1", "100nF", 20, 22, [("1", "3V3", 0, 0), ("2", "GND", 1, 0)], lib="C_0402")
    assert _rows(b.build(), AnalyzerContext())[0]["partner"]["determined"] is True


def test_schematic_placement_names_the_partner(builder):
    from trustpcb.analyzers.schematic import SchematicInfo

    board = _two_parts(builder, 29.0)
    sch = SchematicInfo(path="x.kicad_sch")
    sch.pins[("U1", "1")] = {"name": "VDD", "type": "power_in", "x": 50.0, "y": 50.0}
    sch.pins[("U2", "1")] = {"name": "VCC", "type": "power_in", "x": 200.0, "y": 50.0}
    sch.sym_at["C1"] = (60.0, 55.0)                      # drawn beside U1
    pins = [p for p in board.pads if p.net == "3V3" and p.ref in ("U1", "U2")]
    chosen, partner = decoupling.choose_partner(pins, board.footprint("C1"), board.pads_of("C1")[0], sch, AnalyzerContext())
    assert partner["determined"] and {p.ref for p in chosen} == {"U1"} and "schematic places C1 beside U1" in partner["basis"]
