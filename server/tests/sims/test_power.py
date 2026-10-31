"""Power delivery simulator: closed-form benchmarks, trust checks, what-ifs, caching and the two sample boards.

The benchmarks build a small KiCad board in text, run the real simulator on it and compare the result with
a hand formula. The resistivity is taken at 20 C (``temperature_c`` input) so that the formulas are the textbook ones.
"""

from __future__ import annotations

import math
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import pytest

SERVER = Path(__file__).resolve().parents[2]
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from trustpcb import sims  # noqa: E402
from trustpcb.analyzers.base import AnalyzerContext  # noqa: E402
from trustpcb.pcb.model import parse_board, parse_board_text  # noqa: E402
from trustpcb.sims import power, power_grid  # noqa: E402
from trustpcb.sims.base import run_cached  # noqa: E402

SAMPLES = SERVER.parent / "samples"
RHO = power_grid.RHO_CU_20            # ohm mm
T_CU = 0.035                          # mm, the default outer copper
RS = RHO / T_CU                       # ohm per square


# ------------------------------------------------------------------ a tiny board writer

def _pad(number, kind, shape, x, y, w, h, net, layers, drill=None):
    d = f"(drill {drill})" if drill else ""
    return f'(pad "{number}" {kind} {shape} (at {x} {y}) (size {w} {h}) {d} (layers {layers}) (net "{net}"))'


def board_text(parts, tracks=(), vias=(), zones=(), size=(60, 40)):
    out = ['(kicad_pcb (version 20260206) (generator "pcbnew") (generator_version "10.0") (general (thickness 1.6))',
           '(layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))']
    for ref, value, x, y, pads in parts:
        out.append(f'(footprint "Test:{ref}" (layer "F.Cu") (at {x} {y}) (property "Reference" "{ref}" (at 0 0) (layer "F.SilkS")) (property "Value" "{value}" (at 0 0) (layer "F.Fab")) ' + " ".join(pads) + ")")
    for (x0, y0), (x1, y1), w, layer, net in tracks:
        out.append(f'(segment (start {x0} {y0}) (end {x1} {y1}) (width {w}) (layer "{layer}") (net "{net}"))')
    for x, y, size_mm, drill, net in vias:
        out.append(f'(via (at {x} {y}) (size {size_mm}) (drill {drill}) (layers "F.Cu" "B.Cu") (net "{net}"))')
    for net, layer, pts in zones:
        out.append(f'(zone (net_name "{net}") (net "{net}") (layer "{layer}") (polygon (pts ' + " ".join(f"(xy {a} {b})" for a, b in pts) + ")))")
    w, h = size
    out.append(f'(gr_rect (start 0 0) (end {w} {h}) (layer "Edge.Cuts") (stroke (width 0.1) (type solid)) (fill no))')
    out.append(")")
    return "\n".join(out)


def two_pad_board(tracks, vias=(), src=(0.0, 0.0), load=(20.0, 0.0), pad=0.5, layers_src='"F.Cu"', layers_load='"F.Cu"', src_size=None, load_size=None, load_kind="smd", extra_parts=()):
    ss = src_size or (pad, pad)
    ls = load_size or (pad, pad)
    parts = [
        ("J1", "SRC", src[0], src[1], [_pad(1, "smd", "rect", 0, 0, ss[0], ss[1], "VIN", layers_src)]),
        ("U1", "LOAD", load[0], load[1], [_pad(1, load_kind, "rect", 0, 0, ls[0], ls[1], "VIN", layers_load, 0.3 if load_kind == "thru_hole" else None)]),
        *extra_parts,
    ]
    return parse_board_text(board_text(parts, tracks, vias), "bench")


INPUTS = {"nets": ["VIN"], "sources": {"VIN": ["pad:J1:1"]}, "currents": {"U1": 1.0}, "nominal_v": {"VIN": 5.0}, "temperature_c": 20.0, "include_return": False}


def run(board, inputs=None, whatif=None, tmp=None):
    ctx = AnalyzerContext()
    out = Path(tmp) if tmp else Path(tempfile.mkdtemp(prefix="power_test_"))
    return power.SIMULATOR.run(board, ctx, {**INPUTS, **(inputs or {})}, whatif or [], out)


def r_ohm(res, ref="U1", net="VIN"):
    load = next(l for l in res.detail["nets"][net]["loads"] if l["ref"] == ref)
    return load["r_path_mohm"] / 1000.0


def pct(a, b):
    return (a - b) / b * 100.0


# ------------------------------------------------------------------ closed-form benchmarks

@pytest.mark.parametrize("length,width", [(20.0, 0.4), (10.0, 0.25), (30.0, 1.0)])
def test_uniform_track(length, width, tmp_path):
    """R = rho L / (w t) between two square pads on the ends of a straight track."""
    b = two_pad_board([((0, 0), (length, 0), width, "F.Cu", "VIN")], load=(length, 0.0), pad=width)
    res = run(b, tmp=tmp_path)
    assert res.status == "ok"
    # the source pad is held at zero over its whole area and the load draws over its whole area: the path runs from the
    # source pad edge to the load pad edge, plus a third of the load pad (a current spread over a pad drops by L/3 on average)
    expected = RS * ((length - width) + width / 3.0) / width
    err = pct(r_ohm(res) , expected)
    assert abs(err) < 1.5, f"{err:.2f} %"


def test_uniform_track_oblique(tmp_path):
    """The same formula on tracks at an angle: the grid is not aligned with them, so the stated error is 3 %."""
    for deg in (20, 30, 45, 60):
        a = math.radians(deg)
        length, width = 20.0, 0.4
        b = two_pad_board([((0, 0), (length * math.cos(a), length * math.sin(a)), width, "F.Cu", "VIN")], load=(length * math.cos(a), length * math.sin(a)), pad=width)
        res = run(b, tmp=tmp_path)
        expected = RS * ((length - width) + width / 3.0) / width
        assert abs(pct(r_ohm(res), expected)) < 3.0, deg


def test_two_parallel_tracks(tmp_path):
    """Two tracks of different width between the same two tall pads: R1 R2 / (R1 + R2)."""
    length, w1, w2, gap = 12.0, 0.3, 0.6, 1.5
    y1, y2 = -gap / 2, gap / 2
    pad_w, pad_h = 0.5, gap + 1.0
    tracks = [((0, y1), (length, y1), w1, "F.Cu", "VIN"), ((0, y2), (length, y2), w2, "F.Cu", "VIN")]
    b = two_pad_board(tracks, load=(length, 0.0), src_size=(pad_w, pad_h), load_size=(pad_w, pad_h))
    res = run(b, tmp=tmp_path)
    leff = length - pad_w            # edge to edge of the two pads
    r1, r2 = RS * leff / w1, RS * leff / w2
    expected = r1 * r2 / (r1 + r2)
    err = pct(r_ohm(res), expected)
    assert abs(err) < 3.0, f"{err:.2f} %"


def test_via_resistance(tmp_path):
    """A pad on top and a pad below joined by one via: the barrel is rho L / (pi t_p (d + t_p))."""
    d, plating = 0.4, 0.025
    parts = [
        ("J1", "SRC", 5, 5, [_pad(1, "smd", "circle", 0, 0, 0.8, 0.8, "VIN", '"F.Cu"')]),
        ("U1", "LOAD", 5, 5, [_pad(1, "smd", "circle", 0, 0, 0.8, 0.8, "VIN", '"B.Cu"')]),
    ]
    b = parse_board_text(board_text(parts, vias=[(5, 5, 0.8, d, "VIN")]), "bench")
    res = run(b, tmp=tmp_path)
    length = 1.6 - T_CU                                     # centre to centre of the two copper layers
    expected = RHO * length / (math.pi * plating * (d + plating))
    err = pct(r_ohm(res), expected)
    assert abs(err) < 1.0, f"{err:.2f} % (barrel {expected * 1000:.3f} mOhm)"
    # and the via reports its own current against its limit
    v = res.detail["nets"]["VIN"]["vias"][0]
    assert v["amps"] == pytest.approx(1.0, rel=0.02)
    assert v["limit_a"] > 0


@pytest.mark.parametrize("leg", [10.0, 15.0])
def test_l_bend(leg, tmp_path):
    """An L-shaped track: legs counted to the corner square plus 0.559 squares for the bend (Hall's result), 2 % stated."""
    w = 0.5
    b = two_pad_board([((0, 0), (leg, 0), w, "F.Cu", "VIN"), ((leg, 0), (leg, leg), w, "F.Cu", "VIN")], load=(leg, leg), pad=w)
    res = run(b, tmp=tmp_path)
    squares = ((leg - w / 2 - w / 2) / w) * 2 + 0.559 + (w / 3) / w
    expected = RS * squares
    err = pct(r_ohm(res), expected)
    assert abs(err) < 2.0, f"{err:.2f} %"


def test_temperature_coefficient(tmp_path):
    b = two_pad_board([((0, 0), (20, 0), 0.5, "F.Cu", "VIN")])
    cold = r_ohm(run(b, tmp=tmp_path))
    hot = r_ohm(run(b, {"temperature_c": 100.0}, tmp=tmp_path))
    assert hot / cold == pytest.approx(1 + power_grid.ALPHA_CU * 80.0, rel=1e-3)


# ------------------------------------------------------------------ trust checks and reporting

def test_trust_checks_pass_and_current_is_balanced(tmp_path):
    b = two_pad_board([((0, 0), (20, 0), 0.4, "F.Cu", "VIN")])
    res = run(b, tmp=tmp_path)
    ids = {c["id"]: c for c in res.trust["checks"]}
    assert res.trust["status"] == "ok"
    assert ids["current_continuity"]["status"] == "ok" and ids["current_continuity"]["value"] < 1e-9
    assert ids["mesh_convergence"]["status"] == "ok"
    assert ids["connected_network"]["status"] == "ok"
    assert ids["solver_residual"]["value"] < 1e-9
    assert res.detail["nets"]["VIN"]["source_current_a"] == pytest.approx(1.0, rel=1e-9)


def test_load_not_connected_is_reported_not_zero(tmp_path):
    island = ("U2", "ISLAND", 40.0, 30.0, [_pad(1, "smd", "rect", 0, 0, 0.5, 0.5, "VIN", '"F.Cu"')])
    b = two_pad_board([((0, 0), (20, 0), 0.4, "F.Cu", "VIN")], extra_parts=[island])
    res = run(b, {"currents": {"U1": 1.0, "U2": 0.5}}, tmp=tmp_path)
    t = res.detail["nets"]["VIN"]
    lone = next(l for l in t["loads"] if l["ref"] == "U2")
    assert lone["connected"] is False
    assert lone["drop_mv"] is None and lone["r_path_mohm"] is None and lone["v_pin_min"] is None
    assert "not connected" in lone["problem"]
    check = next(c for c in t["checks"] if c["id"] == "connected_network")
    assert check["status"] == "flag" and "U2" in check["detail"]
    assert res.metrics["v_pin:U2@VIN"]["value"] is None
    # the connected load is still solved, and the flagged island does not break the balance of current
    assert next(c for c in t["checks"] if c["id"] == "current_continuity")["status"] == "ok"
    assert t["source_current_a"] == pytest.approx(1.0, rel=1e-6)


def test_current_continuity_failure_fails_trust(monkeypatch, tmp_path):
    """If the solver lost current, the run must not be trusted. Break the balance on purpose."""
    real = power_grid.solve

    def broken(*a, **k):
        sol = real(*a, **k)
        sol.source_amps = {n: v * 1.01 for n, v in sol.source_amps.items()}
        return sol

    monkeypatch.setattr(power_grid, "solve", broken)
    b = two_pad_board([((0, 0), (20, 0), 0.4, "F.Cu", "VIN")])
    res = run(b, tmp=tmp_path)
    assert res.trust["status"] == "failed"
    assert any(c["id"] == "current_continuity" and c["status"] == "failed" for c in res.trust["checks"])
    assert not res.trusted


def test_mesh_convergence_failure_is_detected(tmp_path):
    """A 0.1 mm diagonal track on a 0.15 mm grid is not resolved: two grids disagree, the check fails and so does the run."""
    a, length, w = math.radians(30), 10.0, 0.1
    end = (length * math.cos(a), length * math.sin(a))
    b = two_pad_board([((0, 0), end, w, "F.Cu", "VIN")], load=end, pad=w)
    bad = run(b, {"resolution_mm": 0.15}, tmp=tmp_path / "coarse")
    check = next(c for c in bad.trust["checks"] if c["id"] == "mesh_convergence")
    assert check["status"] == "failed" and check["value"] > check["limit"]
    assert bad.trust["status"] == "failed" and not bad.trusted
    # the automatic pitch (a fifth of the track width) converges on the same board
    good = run(b, tmp=tmp_path / "auto")
    assert next(c for c in good.trust["checks"] if c["id"] == "mesh_convergence")["status"] == "ok"
    assert good.trust["status"] == "ok"


def test_zone_without_fill_fails_trust(tmp_path):
    parts = [
        ("J1", "SRC", 0, 0, [_pad(1, "smd", "rect", 0, 0, 0.5, 0.5, "VIN", '"F.Cu"')]),
        ("U1", "LOAD", 10, 0, [_pad(1, "smd", "rect", 0, 0, 0.5, 0.5, "VIN", '"F.Cu"')]),
    ]
    b = parse_board_text(board_text(parts, [((0, 0), (10, 0), 0.4, "F.Cu", "VIN")], zones=[("VIN", "B.Cu", [(0, -2), (10, -2), (10, 2), (0, 2)])]), "bench")
    res = run(b, tmp=tmp_path)
    check = next(c for c in res.trust["checks"] if c["id"] == "zone_fill")
    assert check["status"] == "failed"
    assert res.trust["status"] == "failed"


def test_unknown_current_is_labelled_and_per_amp_given(tmp_path):
    parts = [
        ("J1", "SRC", 0, 0, [_pad(1, "smd", "rect", 0, 0, 0.5, 0.5, "VIN", '"F.Cu"')]),
        ("U1", "LOAD", 10, 0, [_pad(1, "smd", "rect", 0, 0, 0.5, 0.5, "VIN", '"F.Cu"')]),
    ]
    b = parse_board_text(board_text(parts, [((0, 0), (10, 0), 0.4, "F.Cu", "VIN")]), "bench")
    res = run(b, {"currents": {}}, tmp=tmp_path)
    load = res.detail["nets"]["VIN"]["loads"][0]
    assert load["current_known"] is False and load["current_source"] == "default"
    assert "placeholder" in load["current_basis"]
    assert load["drop_per_amp_mv"] == load["r_path_mohm"] and load["r_path_mohm"] > 0
    assert res.metrics["currents_assumed"]["value"] == 1
    cur = next(a for a in res.assumptions if a.name == "current U1 on VIN")
    assert cur.source == "default"
    # given by the user it becomes a user value
    res2 = run(b, {"currents": {"U1": {"amps": 0.2, "source": "datasheet", "cite": "ds p.7"}}}, tmp=tmp_path)
    assert next(a for a in res2.assumptions if a.name == "current U1 on VIN").source == "datasheet"
    assert res2.metrics["currents_assumed"]["value"] == 0


def test_no_source_is_not_possible_not_silent_zero():
    parts = [("U1", "LOAD", 10, 0, [_pad(1, "smd", "rect", 0, 0, 0.5, 0.5, "VIN", '"F.Cu"')]), ("U2", "LOAD", 12, 0, [_pad(1, "smd", "rect", 0, 0, 0.5, 0.5, "VIN", '"F.Cu"')])]
    b = parse_board_text(board_text(parts, [((10, 0), (12, 0), 0.4, "F.Cu", "VIN")]), "bench")
    res = power.SIMULATOR.run(b, AnalyzerContext(), {"nets": ["VIN"]}, [], Path(tempfile.mkdtemp(prefix="power_test_")))
    assert res.status == "not_possible" and "source" in res.reason


def test_four_layer_board_chains_the_barrel_through_inner_layers(tmp_path):
    """On four copper layers a through via has the same total resistance as the closed form over the whole stack height."""
    text = board_text(
        [("J1", "SRC", 5, 5, [_pad(1, "smd", "circle", 0, 0, 0.8, 0.8, "VIN", '"F.Cu"')]),
         ("U1", "LOAD", 5, 5, [_pad(1, "smd", "circle", 0, 0, 0.8, 0.8, "VIN", '"B.Cu"')])],
        vias=[(5, 5, 0.8, 0.4, "VIN")],
    ).replace('(layers (0 "F.Cu" signal) (2 "B.Cu" signal)', '(layers (0 "F.Cu" signal) (4 "In1.Cu" signal) (6 "In2.Cu" signal) (2 "B.Cu" signal)')
    b = parse_board_text(text, "bench4")
    assert len(b.copper_layers()) == 4
    res = run(b, tmp=tmp_path)
    stack = power.build_stack(b)
    height = stack.z[-1] - stack.z[0]
    expected = RHO * height / (math.pi * 0.025 * (0.4 + 0.025))
    assert res.status == "ok" and abs(pct(r_ohm(res), expected)) < 3.0, (r_ohm(res) * 1000, expected * 1000)
    assert res.detail["stack"]["layers"] == stack.names and len(stack.names) == 4


def test_hot_spot_at_a_neck(tmp_path):
    """A 1 mm track that narrows to 0.2 mm carries 5 A/mm in the neck at 1 A, above the 1 oz guide; the hot spot list says where."""
    tracks = [((0, 0), (8, 0), 1.0, "F.Cu", "VIN"), ((8, 0), (10, 0), 0.2, "F.Cu", "VIN"), ((10, 0), (18, 0), 1.0, "F.Cu", "VIN")]
    b = two_pad_board(tracks, load=(18.0, 0.0), pad=1.0)
    res = run(b, tmp=tmp_path)
    hot = res.detail["nets"]["VIN"]["hotspots"][0]
    assert hot["over_limit"] is True and hot["ratio"] > 1.5
    assert 7.5 < hot["x"] < 10.5 and abs(hot["y"]) < 0.3
    assert hot["peak_a_per_mm"] == pytest.approx(1.0 / 0.2, rel=0.25)
    assert res.metrics["peak_current_density_a_per_mm"]["value"] == pytest.approx(hot["peak_a_per_mm"], rel=1e-3)


def test_current_limits_follow_ipc2221():
    assert power._ipc_limit_a(1.0, 0.035, 10.0, True) == pytest.approx(2.4, rel=0.03)      # 1 oz outer, 10 C, 1 mm wide
    assert power._ipc_limit_a(1.0, 0.035, 10.0, False) == pytest.approx(1.2, rel=0.03)     # internal layers carry half
    assert 0.8 < power._via_limit_a(0.3, 0.025, 10.0) < 1.2                                 # the usual one amp per via


# ------------------------------------------------------------------ what-ifs

def test_widen_track_scales_resistance(tmp_path):
    b = two_pad_board([((0, 0), (20, 0), 0.25, "F.Cu", "VIN")], pad=0.25)
    base = run(b, tmp=tmp_path / "a")
    wide = run(b, whatif=[{"op": "widen_track", "net": "VIN", "width_mm": 0.5}], tmp=tmp_path / "b")
    assert r_ohm(wide) / r_ohm(base) == pytest.approx(0.5, rel=0.06)
    # the board itself is untouched
    assert b.tracks[0].width == 0.25
    assert wide.detail["delta"]["loads"][0]["change_mv"] < 0


def test_add_via_lets_current_switch_layers(tmp_path):
    """A thin top track and a wide bottom track that only meets the top at the load: a via at the near end moves the current down."""
    parts = [
        ("J1", "SRC", 0, 0, [_pad(1, "smd", "rect", 0, 0, 0.6, 0.6, "VIN", '"F.Cu"')]),
        ("U1", "LOAD", 20, 0, [_pad(1, "thru_hole", "circle", 0, 0, 1.0, 1.0, "VIN", '"F.Cu" "B.Cu"', 0.4)]),
    ]
    tracks = [((0, 0), (20, 0), 0.2, "F.Cu", "VIN"), ((2, 0), (20, 0), 1.0, "B.Cu", "VIN")]
    b = parse_board_text(board_text(parts, tracks), "bench")
    base = run(b, tmp=tmp_path / "a")
    after = run(b, whatif=[{"op": "add_via", "net": "VIN", "at": [2.0, 0.0]}], tmp=tmp_path / "b")
    r0, r1 = r_ohm(base), r_ohm(after)
    assert r1 < 0.4 * r0
    squares = (2.0 - 0.3) / 0.2 + 1.0 / (1 / ((20 - 2.0) / 0.2) + 1 / ((18.0) / 1.0))     # F.Cu stub, then the two routes in parallel
    assert abs(pct(r1, RS * squares)) < 12.0
    assert len(b.vias) == 0


def test_set_current_scales_drop_not_resistance(tmp_path):
    b = two_pad_board([((0, 0), (20, 0), 0.4, "F.Cu", "VIN")])
    one = run(b, tmp=tmp_path / "a")
    two = run(b, whatif=[{"op": "set_current", "ref": "U1", "amps": 2.0}], tmp=tmp_path / "b")
    d1 = one.detail["nets"]["VIN"]["loads"][0]
    d2 = two.detail["nets"]["VIN"]["loads"][0]
    assert d2["amps"] == 2.0
    assert d2["drop_mv"] == pytest.approx(2 * d1["drop_mv"], rel=1e-6)
    assert d2["r_path_mohm"] == pytest.approx(d1["r_path_mohm"], rel=1e-9)


def test_refused_whatifs(tmp_path):
    b = two_pad_board([((0, 0), (20, 0), 0.4, "F.Cu", "VIN")])
    sim = sims.SIMULATORS["power"]
    for bad in ({"op": "remove_gap"}, {"op": "set_part_power", "ref": "U1", "watts": 1}):
        res, _, _ = run_cached(sim, b, AnalyzerContext(), INPUTS, [bad], tmp_path)
        assert res.status == "not_possible" and "not accepted" in res.reason
    for bad in ({"op": "widen_track", "net": "NOPE", "width_mm": 1}, {"op": "widen_track", "net": "VIN", "width_mm": -1},
                {"op": "add_via", "net": "VIN", "at": [1]}, {"op": "set_current", "ref": "ZZ9", "amps": 1}):
        res, _, _ = run_cached(sim, b, AnalyzerContext(), INPUTS, [bad], tmp_path)
        assert res.status == "not_possible", bad


# ------------------------------------------------------------------ registry, cache and overlays

def test_registered_and_cached(tmp_path):
    assert "power" in sims.SIMULATORS and sims.SIMULATORS["power"].kind == "power"
    b = two_pad_board([((0, 0), (20, 0), 0.4, "F.Cu", "VIN")])
    sim = sims.SIMULATORS["power"]
    r1, folder, cached1 = run_cached(sim, b, AnalyzerContext(), INPUTS, [], tmp_path)
    r2, folder2, cached2 = run_cached(sim, b, AnalyzerContext(), INPUTS, [], tmp_path)
    assert (cached1, cached2) == (False, True) and folder == folder2
    assert r2.to_dict()["metrics"] == r1.to_dict()["metrics"]
    assert (folder / "result.json").is_file()
    r3, folder3, cached3 = run_cached(sim, b, AnalyzerContext(), INPUTS, [{"op": "set_current", "ref": "U1", "amps": 0.5}], tmp_path)
    assert folder3 != folder and not cached3


def test_overlays_are_float32_rasters_with_nan_outside_copper(tmp_path):
    b = two_pad_board([((0, 0), (20, 0), 0.4, "F.Cu", "VIN")])
    sim = sims.SIMULATORS["power"]
    res, folder, _ = run_cached(sim, b, AnalyzerContext(), INPUTS, [], tmp_path)
    ids = {o.id for o in res.overlays}
    assert {"drop:F.Cu", "current:F.Cu"} <= ids
    for o in res.overlays:
        a = np.fromfile(folder / o.file, dtype="<f4")
        assert a.size == o.nx * o.ny
        a = a.reshape(o.ny, o.nx)
        assert np.isnan(a).any() and np.isfinite(a).any()
        assert np.nanmax(a) <= o.vmax * 1.0001 and np.nanmin(a) >= 0
    drop = next(o for o in res.overlays if o.id == "drop:F.Cu")
    a = np.fromfile(folder / drop.file, dtype="<f4").reshape(drop.ny, drop.nx)
    assert np.nanmax(a) == pytest.approx(r_ohm(res) * 1000 * 1.0, rel=0.1)       # mV at 1 A is the path resistance in mOhm


def test_current_density_matches_current_over_width(tmp_path):
    """Mid-track sheet current density is I / w."""
    b = two_pad_board([((0, 0), (20, 0), 0.5, "F.Cu", "VIN")])
    sim = sims.SIMULATORS["power"]
    res, folder, _ = run_cached(sim, b, AnalyzerContext(), INPUTS, [], tmp_path)
    o = next(o for o in res.overlays if o.id == "current:F.Cu")
    a = np.fromfile(folder / o.file, dtype="<f4").reshape(o.ny, o.nx)
    j = int((0 - o.y0) / o.dy)
    i = int((10 - o.x0) / o.dx)
    assert a[j, i] == pytest.approx(1.0 / 0.5, rel=0.06)


# ------------------------------------------------------------------ the sample boards

def _sample(name, stem):
    return parse_board(SAMPLES / name / f"{stem}.kicad_pcb", f"v-{name}")


def _expected_squares_usb_ldo_r3():
    """U1 pin 5 to R3 pin 1 on 3V3, by hand. From the source pad edge (x 30.80) along the 0.5 mm track to the corner square
    that starts at x = 32.5 - 0.2, round the bend (0.56 squares), up the 0.4 mm track from y = 18.95 - 0.25 to the
    pad edge (y 15.80), then a third of the 0.95 mm load pad that is 0.8 mm wide."""
    return (32.3 - 30.8025) / 0.5 + (18.7 - 15.8) / 0.4 + 0.559 + (0.95 / 3) / 0.8


def test_sample_usb_ldo_led(tmp_path):
    b = _sample("usb-ldo-led", "usb_ldo_led_node")
    sim = sims.SIMULATORS["power"]
    t0 = time.perf_counter()
    res, folder, _ = run_cached(sim, b, AnalyzerContext(), {"temperature_c": 20.0}, [], tmp_path)
    assert time.perf_counter() - t0 < 10.0
    assert res.status == "ok" and res.trust["status"] == "ok", [c for c in res.trust["checks"] if c["status"] != "ok"]
    nets = res.detail["nets"]
    assert set(nets) == {"VBUS", "VBUS_F", "3V3"}
    assert nets["3V3"]["source"]["ref"] == "U1" and nets["VBUS"]["source"]["ref"] == "J1"
    # the regulator input current is the output current plus the labelled quiescent current, and nobody hid it
    u1 = next(l for l in nets["VBUS_F"]["loads"] if l["ref"] == "U1")
    r3 = next(l for l in nets["3V3"]["loads"] if l["ref"] == "R3")
    assert u1["amps"] == pytest.approx(r3["amps"] + power.DEFAULT_REGULATOR_IQ_A, abs=2e-6)
    assert r3["current_known"] is False and "LED" in r3["current_basis"]
    assert r3["amps"] == pytest.approx((3.3 - 2.0) / 330.0, rel=1e-3)
    # by-hand resistance of the U1 to R3 path (1 % by hand, 3 % allowed)
    expected = RS * _expected_squares_usb_ldo_r3()
    assert abs(pct(r3["r_path_mohm"] / 1000.0, expected)) < 3.0, (r3["r_path_mohm"], expected * 1000)
    # drop = I R
    assert r3["drop_mv"] == pytest.approx(r3["amps"] * r3["r_path_mohm"], rel=1e-3)
    # ground return solved from the connector pads
    assert "GND" in res.detail["return"] and res.detail["return"]["GND"]["trust"] == "ok"
    assert res.metrics["worst_drop_mv"]["value"] > 0


def test_sample_nrf52840_node(tmp_path):
    b = _sample("nrf52840-node", "nrf52840_rf_iot_node")
    sim = sims.SIMULATORS["power"]
    t0 = time.perf_counter()
    res, folder, _ = run_cached(sim, b, AnalyzerContext(), {"temperature_c": 20.0}, [], tmp_path)
    assert time.perf_counter() - t0 < 10.0
    assert res.status == "ok" and res.trust["status"] == "ok", [c for c in res.trust["checks"] if c["status"] == "failed"]
    nets = res.detail["nets"]
    assert {"VBUS_RAW", "VBUS_F", "3V3"} <= set(nets)
    assert nets["3V3"]["source"]["ref"] == "U2"
    # current balance at every net
    for t in list(nets.values()) + list(res.detail["return"].values()):
        c = next(c for c in t["checks"] if c["id"] == "current_continuity")
        assert c["status"] == "ok"
    # U2 (input at VBUS_F) by hand: F1 pad via, 0.778 mm + 12.912 mm of 0.5 mm track on B.Cu, a second via, then the pad
    u2 = next(l for l in nets["VBUS_F"]["loads"] if l["ref"] == "U2")
    via = power_grid.barrel_resistance(0.2, 0.025, 1.6 - 0.035, RHO)
    expected = 2 * via + RS * (0.778 + 12.912) / 0.5
    assert abs(pct(u2["r_path_mohm"] / 1000.0, expected)) < 5.0, (u2["r_path_mohm"], expected * 1000)
    # the unknown MCU current is a labelled default and the per-amp number is given
    u3 = next(l for l in nets["3V3"]["loads"] if l["ref"] == "U3")
    assert u3["current_known"] is False and u3["current_source"] == "default" and u3["drop_per_amp_mv"] > 0
    # the fuse sits in series and says its own drop is left out
    assert "not included" in nets["VBUS_F"]["upstream_note"]
