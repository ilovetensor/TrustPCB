from __future__ import annotations

import math
import re
import subprocess
from pathlib import Path

import pytest

from trustpcb.pcb import geom, sexpr
from trustpcb.pcb.model import UnknownTarget, parse_board, parse_board_text

SAMPLES = Path(__file__).resolve().parents[3] / "samples"
NRF_PCB = SAMPLES / "nrf52840-node" / "nrf52840_rf_iot_node.kicad_pcb"

HEADER = """(kicad_pcb (version 20240108) (generator "pcbnew") (generator_version "9.0")
 (general (thickness 1.6))
 (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (5 "F.SilkS" user "F.Silkscreen") (31 "F.CrtYd" user) (25 "Edge.Cuts" user))
"""


def test_counts_match_file(nrf_board):
    root = sexpr.parse(NRF_PCB.read_text())
    b = nrf_board
    # Counted straight from the S-expression tree, not with grep (which also hits
    # "(footprints not_allowed)" and "(vias not_allowed)" inside the keep-out zone).
    assert len(b.footprints) == len(list(sexpr.findall(root, "footprint"))) == 50
    assert len(b.tracks) == len(list(sexpr.findall(root, "segment"))) == 234
    assert len(b.vias) == len(list(sexpr.findall(root, "via"))) == 102
    assert len(b.zones) + len(b.keepouts) == len(list(sexpr.findall(root, "zone"))) == 3
    assert (len(b.zones), len(b.keepouts)) == (2, 1)
    pads = sum(len(list(sexpr.findall(fp, "pad"))) for fp in sexpr.findall(root, "footprint"))
    assert len(b.pads) == pads == 211


def test_outline_arcs_and_bbox(nrf_board):
    assert len(nrf_board.outline) == 1
    assert nrf_board.bbox == pytest.approx([0, 0, 66, 53], abs=1e-6)
    # four rounded corners flattened into many vertices
    assert len(nrf_board.outline[0]) > 30
    assert not nrf_board.warnings


def test_ids_and_uuid_index(nrf_board):
    b = nrf_board
    assert b.footprint("R5").id == "ref:R5"
    assert len({t.id for t in b.tracks}) == len(b.tracks)
    assert all(re.fullmatch(r"track:[0-9a-f]{8}(~\d+)?", t.id) for t in b.tracks)
    t = b.tracks[0]
    assert b.uuid_index[t.uuid] == t.id
    assert b.uuid_index[b.footprint("U3").uuid] == "ref:U3"


def test_pad_position_rotation_applied(nrf_board):
    b = nrf_board
    p1, p2 = b.pads_of("R5")
    # R5 sits at (44.5, 15.5) rotated 90 degrees; local pads at x = -/+0.825
    assert (p1.x, p1.y) == pytest.approx((44.5, 16.325))
    assert (p2.x, p2.y) == pytest.approx((44.5, 14.675))
    xs = [q[0] for q in p1.ring]
    ys = [q[1] for q in p1.ring]
    assert (max(xs) - min(xs), max(ys) - min(ys)) == pytest.approx((0.95, 0.8), abs=1e-6)
    assert p1.net == "3V3" and p1.layers[0] == "F.Cu"


def test_duplicate_and_unnumbered_pads(nrf_board):
    ids = [p.id for p in nrf_board.pads]
    assert len(ids) == len(set(ids))
    assert "pad:J1:SH" in ids and "pad:J1:SH#2" in ids
    assert any(i.startswith("pad:H1:~") for i in ids)
    group = nrf_board.resolve("pad:J1:SH")
    assert group.kind == "polygon"  # the first one keeps the plain id


def test_nets_zones_keepouts(nrf_board):
    b = nrf_board
    names = b.net_names()
    assert "ANT_FEED" in names and "GND" in names
    z = {z.name: z for z in b.zones}
    assert z["GND_POUR_TOP"].filled["F.Cu"] and z["GND_POUR_BOTTOM"].filled["B.Cu"]
    k = b.keepouts[0]
    assert k.name == "ANTENNA_KEEPOUT" and k.rules["tracks"] == "not_allowed"
    assert geom.bbox_of(k.ring) == [19.5, 0.0, 44.5, 10.0]


def test_default_stackup_is_assumed(nrf_board):
    s = nrf_board.stackup
    assert s["source"] == "default" and s["assumed"] is True
    kinds = [x["kind"] for x in s["layers"]]
    assert kinds == ["copper", "dielectric", "copper"]
    assert sum(x["thickness_mm"] for x in s["layers"]) == pytest.approx(1.6, abs=1e-3)


def test_stackup_read_from_board():
    text = HEADER + """ (setup (stackup
   (layer "F.Cu" (type "copper") (thickness 0.035))
   (layer "dielectric 1" (type "core") (thickness 0.2) (material "Rogers") (epsilon_r 3.5) (loss_tangent 0.004))
   (layer "B.Cu" (type "copper") (thickness 0.035))))
)"""
    b = parse_board_text(text)
    assert b.stackup["source"] == "board" and not b.stackup["assumed"]
    d = [x for x in b.stackup["layers"] if x["kind"] == "dielectric"][0]
    assert (d["epsilon_r"], d["loss_tangent"], d["material"]) == (3.5, 0.004, "Rogers")


def test_kicad9_net_table_syntax():
    text = HEADER + """
 (net 0 "") (net 1 "GND") (net 2 "SIG")
 (segment (start 0 0) (end 5 0) (width 0.25) (layer "F.Cu") (net 2) (uuid "11111111-aaaa-bbbb-cccc-000000000001"))
 (via (at 5 0) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "22222222-aaaa-bbbb-cccc-000000000002"))
 (footprint "R:R" (layer "F.Cu") (uuid "33333333-aaaa-bbbb-cccc-000000000003") (at 10 10)
   (property "Reference" "R1" (at 0 0 0)) (property "Value" "1k" (at 0 1 0))
   (pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu" "F.Mask") (net 2 "SIG") (uuid "44444444-aaaa-bbbb-cccc-000000000004")))
 (zone (net 1) (net_name "GND") (layer "F.Cu") (uuid "55555555-aaaa-bbbb-cccc-000000000005")
   (polygon (pts (xy 0 0) (xy 10 0) (xy 10 10))))
)"""
    b = parse_board_text(text)
    assert b.tracks[0].net == "SIG" and b.vias[0].net == "GND"
    assert b.pads[0].net == "SIG" and b.zones[0].net == "GND"
    assert set(b.net_names()) == {"GND", "SIG"}
    assert b.resolve("net:SIG").kind == "group"


def test_kicad10_net_name_syntax_and_arc_track():
    text = HEADER + """
 (segment (start 0 0) (mid 1 1) (end 2 0) (width 0.2) (layer "F.Cu") (net "RF") (uuid "aaaaaaaa-0000-0000-0000-000000000001"))
 (arc (start 0 0) (mid 1 1) (end 2 0) (width 0.2) (layer "F.Cu") (net "RF") (uuid "bbbbbbbb-0000-0000-0000-000000000002"))
)"""
    b = parse_board_text(text)
    assert [t.net for t in b.tracks] == ["RF", "RF"]
    arc = b.tracks[1]
    assert arc.mid == [1.0, 1.0]
    assert arc.length == pytest.approx(math.pi, abs=1e-6)  # half circle of radius 1
    geo = b.resolve(arc.id)
    assert geo.kind == "polyline" and len(geo.points) > 4
    assert geo.points[0] == [0.0, 0.0] and geo.points[-1] == [2.0, 0.0]


def test_bottom_footprint_and_pad_shapes_polygons():
    text = HEADER + """
 (footprint "X:X" (layer "B.Cu") (uuid "66666666-aaaa-bbbb-cccc-000000000006") (at 20 20 30)
   (property "Reference" "U9" (at 0 0 0)) (property "Value" "x" (at 0 1 0))
   (fp_rect (start -2 -2) (end 2 2) (stroke (width 0.05) (type solid)) (fill no) (layer "B.CrtYd"))
   (pad "1" smd roundrect (at 1 0 30) (size 1 0.5) (layers "B.Cu") (roundrect_rratio 0.25))
   (pad "2" smd oval (at -1 0 30) (size 1 0.5) (layers "B.Cu"))
   (pad "3" thru_hole circle (at 0 1) (size 1 1) (drill 0.5) (layers "*.Cu"))
   (pad "4" smd custom (at 0 -1) (size 0.2 0.2) (layers "F.Cu") (options (clearance outline) (anchor circle))
      (primitives (gr_poly (pts (xy 0 0) (xy 1 0) (xy 1 1) (xy 0 1)) (width 0) (fill yes)))))
)"""
    b = parse_board_text(text)
    f = b.footprint("U9")
    assert f.side == "bottom" and f.courtyard is not None
    shapes = {p.number: p for p in b.pads}
    for p in shapes.values():
        assert len(p.ring) >= 4
        assert geom.ring_area(p.ring) > 0
    # all layers expansion for through-hole pads
    assert shapes["3"].layers == ["F.Cu", "B.Cu"] and shapes["3"].drill == 0.5
    # roundrect area is a little smaller than the plain rectangle, oval smaller still
    assert geom.ring_area(shapes["1"].ring) < 0.5 and geom.ring_area(shapes["2"].ring) < geom.ring_area(shapes["1"].ring)
    # custom pad is bigger than its 0.2 mm anchor: it includes the 1 mm square primitive
    assert geom.ring_area(shapes["4"].ring) > 0.9
    # rotation 30 degrees about the footprint origin: pad 1 local (1, 0)
    c, s = math.cos(math.radians(30)), math.sin(math.radians(30))
    assert (shapes["1"].x, shapes["1"].y) == pytest.approx((20 + c, 20 - s))


def test_resolve_all_target_kinds(nrf_board):
    b = nrf_board
    assert b.resolve("pad:U3:A8").kind == "polygon"
    assert b.resolve(b.tracks[0].id).kind == "polyline"
    v = b.resolve(b.vias[0].id)
    assert v.kind == "point" and len(v.points) == 1
    assert b.resolve("ref:U3").kind == "polygon"
    assert b.resolve(b.zones[0].id).kind == "polygon"
    assert b.resolve(b.keepouts[0].id).kind == "polygon"
    g = b.resolve("net:ANT_FEED")
    assert g.kind == "group" and g.parts and g.bbox[0] < g.bbox[2]
    assert b.resolve("point:1.5,2.5").points == [[1.5, 2.5]]
    assert b.resolve("bbox:0,0,4,5").bbox == [0, 0, 4, 5]
    b.register_candidate("candidate:x:1", ["ref:U3", "pad:U3:A8"])
    b.register_zone_role("power", ["ref:U2"])
    assert b.resolve("candidate:x:1").kind == "group"
    assert b.resolve("zone_role:power").kind == "group"
    d = b.resolve("pad:U3:A8").to_dict()
    assert d["target"] == "pad:U3:A8" and d["center"] == pytest.approx([28.75, 21.25])


@pytest.mark.parametrize("bad", ["", "nothing", "pad:U3:ZZ", "ref:NOPE", "net:NOPE", "track:ffffffff", "zone_role:x", "candidate:x:9", "point:a,b", "bbox:1,2"])
def test_unknown_targets_raise(nrf_board, bad):
    with pytest.raises(UnknownTarget):
        nrf_board.resolve(bad)


def test_to_dict_contract_fields(nrf_board):
    d = nrf_board.to_dict()
    for key in ("version_id", "title", "outline", "bbox", "layers", "stackup", "nets", "footprints", "pads", "tracks", "vias", "zones", "keepouts"):
        assert key in d
    assert d["version_id"] == "v-nrf"
    assert {"name", "kind", "index"} <= set(d["layers"][0])
    assert {"id", "ref", "value", "lib_id", "x", "y", "rotation", "side", "bbox", "courtyard", "pad_ids", "dnp"} <= set(d["footprints"][0])
    assert {"id", "ref", "number", "net", "x", "y", "w", "h", "rotation", "shape", "layers", "drill", "ring"} <= set(d["pads"][0])
    assert {"id", "net", "layer", "width", "start", "end", "mid", "length"} <= set(d["tracks"][0])
    assert {"id", "net", "x", "y", "diameter", "drill", "layers"} <= set(d["vias"][0])
    assert {"id", "net", "layers", "filled", "is_keepout"} <= set(d["zones"][0]) and d["zones"][0]["is_keepout"] is False
    assert {"id", "layers", "ring", "rules"} <= set(d["keepouts"][0])
    assert {"id", "name", "net_class"} <= set(d["nets"][0]) and d["nets"][0]["id"] == "net:" + d["nets"][0]["name"]


def test_not_a_board_rejected():
    with pytest.raises(ValueError):
        parse_board_text("(kicad_sch (version 1))")


def test_pads_match_kicad_own_svg_plot(tmp_path, nrf_copy):
    """Every F.Cu SMD pad outline equals what KiCad itself plots (within 0.02 mm)."""
    from trustpcb.pcb import kicad_cli

    if kicad_cli.find_kicad_cli() is None:
        pytest.skip("kicad-cli not installed")
    pcb = nrf_copy / "nrf52840_rf_iot_node.kicad_pcb"
    out = tmp_path / "fcu.svg"
    exe = kicad_cli.find_kicad_cli()
    subprocess.run([exe, "pcb", "export", "svg", "--layers", "F.Cu", "--mode-single", "--page-size-mode", "2", "--exclude-drawing-sheet", "-o", str(out), str(pcb)], check=True, capture_output=True, cwd=nrf_copy)
    svg = out.read_text()
    boxes = []
    for m in re.finditer(r'd="([^"]+)"', svg):
        nums = [float(x) for x in re.findall(r"-?\d+\.\d+", m.group(1))]
        if nums:
            boxes.append((min(nums[0::2]), min(nums[1::2]), max(nums[0::2]), max(nums[1::2])))
    for m in re.finditer(r'<circle cx="([\d.]+)" cy="([\d.]+)" r="([\d.]+)"', svg):
        cx, cy, r = map(float, m.groups())
        boxes.append((cx - r, cy - r, cx + r, cy + r))
    # oval pads are plotted as a round-capped thick line: <path d="M x0 y0 L x1 y1"> with a stroke width
    board = parse_board(pcb)
    checked = 0
    for p in board.pads:
        if "F.Cu" not in p.layers or p.type != "smd" or p.shape == "oval":
            continue
        xs = [q[0] for q in p.ring]
        ys = [q[1] for q in p.ring]
        want = (min(xs), min(ys), max(xs), max(ys))
        best = min(max(abs(a - b) for a, b in zip(want, k)) for k in boxes)
        assert best < 0.02, (p.id, best)
        checked += 1
    assert checked > 150


def test_net_classes_in_the_kicad6_layout_and_in_the_board_file():
    from trustpcb.pcb.model import parse_board_text

    text = """(kicad_pcb (version 20240108) (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
 (segment (start 0 0) (end 5 0) (width 0.2) (layer "F.Cu") (net "ANT") (uuid "aaaaaaaa-0000-0000-0000-000000000001"))
 (segment (start 0 1) (end 5 1) (width 0.2) (layer "F.Cu") (net "VCC") (uuid "aaaaaaaa-0000-0000-0000-000000000002"))
 (segment (start 0 2) (end 5 2) (width 0.2) (layer "F.Cu") (net "SIG") (uuid "aaaaaaaa-0000-0000-0000-000000000003")))"""
    six = {"net_settings": {"classes": [{"name": "Default", "track_width": 0.2, "nets": []},
                                        {"name": "RF", "track_width": 0.4, "nets": ["ANT"]},
                                        {"name": "Power", "track_width": 0.5, "nets": ["VCC"]}]}}
    b = parse_board_text(text, project=six)
    assert {n.name: n.net_class for n in b.nets} == {"ANT": "RF", "VCC": "Power", "SIG": "Default"}
    # KiCad 7 and later keep the assignments apart from the class list, and still win over a class's own list
    seven = {"net_settings": {"classes": [{"name": "RF", "nets": ["ANT"]}], "netclass_assignments": {"ANT": "Antenna"}, "netclass_patterns": [{"pattern": "V*", "netclass": "Power"}]}}
    assert {n.name: n.net_class for n in parse_board_text(text, project=seven).nets} == {"ANT": "Antenna", "VCC": "Power", "SIG": "Default"}
    # KiCad 5 and older: the classes are in the board file
    old = text.replace("(segment", '(net_class Power "" (clearance 0.2) (add_net VCC))\n (segment', 1)
    assert {n.name: n.net_class for n in parse_board_text(old).nets}["VCC"] == "Power"


def test_kicad5_modules_are_read_as_footprints():
    from trustpcb.pcb.model import parse_board_text

    text = """(kicad_pcb (version 20171130) (host pcbnew 5.1.0)
 (layers (0 F.Cu signal) (31 B.Cu signal))
 (net 0 "") (net 1 GND)
 (module Antenna:Test (layer F.Cu) (tedit 0) (tstamp 5A5A5A5A) (at 10 10 90)
   (fp_text reference AE1 (at 0 0) (layer F.SilkS)) (fp_text value Antenna_PCB (at 0 1) (layer F.Fab))
   (fp_poly (pts (xy 0 0) (xy 4 0) (xy 4 1) (xy 0 1)) (layer F.Cu) (width 0.1))
   (pad 1 smd rect (at 0 0 90) (size 1 1) (layers F.Cu) (net 1 GND))))"""
    b = parse_board_text(text)
    assert [f.ref for f in b.footprints] == ["AE1"] and b.pads_of("AE1")[0].net == "GND"
    poly = [g for g in b.graphics if g.layer == "F.Cu"]
    assert poly and poly[0].fill, "a KiCad 5 polygon has no fill entry and is filled"
