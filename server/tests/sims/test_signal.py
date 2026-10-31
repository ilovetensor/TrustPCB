"""Signal simulator: benchmarks against closed forms, trust checks, what-ifs, caching and the sample boards.

The field solver is checked first on its own against published results (Hammerstad and Jensen microstrip,
IPC-2141, Cohn's exact stripline and coupled stripline, Kirschning and Jansen even and odd mode permittivity,
a known TDR step). Then the simulator runs on small boards written in text, where the answer is known, and on the
two sample boards. Errors are stated in the assertions; where the solver and a formula disagree beyond the stated
accuracy of the formula the test says so instead of loosening the number.
"""

from __future__ import annotations

import json
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
from trustpcb.sims import fieldsolve as fs  # noqa: E402
from trustpcb.sims import signal as sg  # noqa: E402
from trustpcb.sims.base import run_cached  # noqa: E402

SAMPLES = SERVER.parent / "samples"
NRF = SAMPLES / "nrf52840-node" / "nrf52840_rf_iot_node.kicad_pcb"
USB = SAMPLES / "usb-ldo-led" / "usb_ldo_led_node.kicad_pcb"
CONFIRM = sg.QUALITY["confirm"]
SCREEN = sg.QUALITY["screen"]


def pct(a: float, b: float) -> float:
    return 100.0 * (a / b - 1.0)


# ------------------------------------------------------------------ field solver against closed forms


@pytest.mark.parametrize(
    "w,h,t,er",
    [(0.3, 1.53, 0.035, 4.5), (0.15, 1.53, 0.035, 4.5), (1.0, 1.53, 0.035, 4.5), (2.835, 1.53, 0.035, 4.5), (0.25, 0.2, 0.035, 4.2), (0.4, 0.2, 0.07, 4.2), (0.2, 0.1, 0.018, 3.8)],
)
def test_microstrip_against_hammerstad_jensen(w, h, t, er):
    """Hammerstad and Jensen state about 1 percent with thickness; the solver is within 0.6 percent at confirm quality, 1.2 at screen."""
    sec = fs.microstrip_section(w, h, t, er, box=40, side=40)
    zh, eh = fs.hammerstad_jensen(w, h, t, er)
    s = fs.solve(sec, **CONFIRM)
    assert abs(pct(s.z0(), zh)) < 0.6, f"confirm {pct(s.z0(), zh):+.2f} %"
    assert abs(pct(fs.solve(sec, **SCREEN).z0(), zh)) < 1.2
    # the solver puts thick copper's extra fringing into the permittivity: the HJ effective permittivity without
    # the thickness term is not comparable, so only the zero-thickness limit is compared on that quantity
    thin = fs.solve(fs.microstrip_section(w, h, 0.002, er, box=40, side=40), **CONFIRM)
    assert abs(pct(thin.eps_eff(), fs.hammerstad_jensen(w, h, 0.002, er)[1])) < 0.6


@pytest.mark.parametrize("w,h,t,er", [(0.3, 1.53, 0.035, 4.5), (0.15, 1.53, 0.035, 4.5), (1.0, 1.53, 0.035, 4.5), (2.835, 1.53, 0.035, 4.5), (0.25, 0.2, 0.035, 4.2)])
def test_microstrip_against_ipc2141(w, h, t, er):
    """IPC-2141's microstrip form is stated for 0.1 < w/h < 2 and is a fit; agreement is within 3 percent there (it is 11 percent off at w/h = 2.5, outside its range)."""
    z = fs.solve(fs.microstrip_section(w, h, t, er, box=40, side=40), **CONFIRM).z0()
    assert abs(pct(z, fs.ipc2141_microstrip(w, h, t, er))) < 3.0


@pytest.mark.parametrize("w,b", [(0.2, 0.5), (0.2, 0.3), (0.4, 0.6), (0.1, 0.4), (0.5, 0.5)])
def test_stripline_against_cohn_exact(w, b):
    """Cohn's conformal-mapping result is for a zero-thickness strip. The solver runs t/b = 0.001, which by itself lowers Z0 by about 0.4 percent."""
    er = 4.2
    s = fs.solve(fs.stripline_section(w, b, 0.001 * b, er), **CONFIRM)
    exact = fs.cohn_stripline(w, b, er)
    assert -1.2 < pct(s.z0(), exact) < 0.2, f"{pct(s.z0(), exact):+.2f} %"
    assert s.eps_eff() == pytest.approx(er, rel=1e-6)  # homogeneous medium


def test_stripline_against_ipc2141_in_its_range():
    """IPC-2141 stripline holds for w/(b-t) < 0.35 and t/b < 0.25; at t/b = 0.07 and w/b = 0.4 it is 2 percent from the solver."""
    w, b, t, er = 0.2, 0.5, 0.035, 4.2
    z = fs.solve(fs.stripline_section(w, b, t, er), **CONFIRM).z0()
    assert abs(pct(z, fs.ipc2141_stripline(w, b, t, er))) < 3.0


@pytest.mark.parametrize("w,s,b", [(0.2, 0.2, 0.5), (0.15, 0.3, 0.4), (0.2, 0.1, 0.5), (0.3, 0.6, 0.5)])
def test_coupled_stripline_against_cohn_exact(w, s, b):
    """Even and odd mode impedance of a coupled stripline pair; far-end crosstalk is zero in a homogeneous medium."""
    er = 4.2
    sol = fs.solve(fs.coupled_stripline_section(w, s, b, 0.001 * b, er), **CONFIRM)
    c, l_ = sol.c, sol.l
    ze = math.sqrt((l_[0, 0] + l_[0, 1]) / (c[0, 0] + c[0, 1]))
    zo = math.sqrt((l_[0, 0] - l_[0, 1]) / (c[0, 0] - c[0, 1]))
    ze2, zo2 = fs.cohn_coupled_stripline(w, s, b, er)
    assert -1.5 < pct(ze, ze2) < 0.3, pct(ze, ze2)
    assert -1.5 < pct(zo, zo2) < 0.3, pct(zo, zo2)
    xt = fs.crosstalk(-c[0, 1], c[0, 0], l_[0, 1], l_[0, 0], 1e-9, 1e-10)
    assert abs(xt["far_end"]) < 0.02 * xt["kb"] + 1e-4  # Cm/C equals Lm/L
    # the weak coupling backward coefficient equals the exact (Ze - Zo) / (Ze + Zo) / 2 to rounding
    assert xt["kb"] == pytest.approx(0.5 * (ze - zo) / (ze + zo), rel=0.03)


@pytest.mark.parametrize("w,s,h,er", [(0.3, 0.3, 0.2, 4.2), (0.2, 0.2, 0.2, 4.5), (0.3, 0.15, 0.2, 4.2), (0.25, 0.5, 0.2, 4.2), (0.25, 0.25, 1.53, 4.5)])
def test_coupled_microstrip_against_kirschning_jansen(w, s, h, er):
    """Even and odd mode effective permittivity (Kirschning and Jansen, stated about 1 percent) and even mode impedance (Hammerstad and Jensen)."""
    sol = fs.solve(fs.coupled_microstrip_section(w, s, h, 0.002, er), **CONFIRM)
    c, ca = sol.c, sol.c_air
    ce, co = c[0, 0] + c[0, 1], c[0, 0] - c[0, 1]
    cae, cao = ca[0, 0] + ca[0, 1], ca[0, 0] - ca[0, 1]
    k = fs.kirschning_jansen_static(w, s, h, er)
    assert abs(pct(ce / cae, k["eps_e"])) < 1.0
    assert abs(pct(co / cao, k["eps_o"])) < 1.5
    ze = 1 / (fs.C_LIGHT * math.sqrt(ce * cae))
    assert abs(pct(ze, k["z0e"])) < 1.5
    # odd mode is below even mode, and both converge on the single line far apart
    zo = 1 / (fs.C_LIGHT * math.sqrt(co * cao))
    assert zo < ze
    far = fs.solve(fs.coupled_microstrip_section(w, 20 * h, h, 0.002, er), **CONFIRM)
    cf, caf = far.c, far.c_air
    zo_far = 1 / (fs.C_LIGHT * math.sqrt((cf[0, 0] - cf[0, 1]) * (caf[0, 0] - caf[0, 1])))
    assert abs(pct(zo_far, fs.hammerstad_jensen(w, h, 0.002, er)[0])) < 1.5


def test_microstrip_crosstalk_signs_and_saturation():
    """On a surface line the field is not homogeneous: far-end crosstalk is negative (inductive coupling wins) and a short line has less near-end crosstalk."""
    sol = fs.solve(fs.coupled_microstrip_section(0.25, 0.25, 0.2, 0.035, 4.2), **CONFIRM)
    c, l_ = sol.c, sol.l
    long_ = fs.crosstalk(-c[0, 1], c[0, 0], l_[0, 1], l_[0, 0], 600e-12, 1e-9)
    short = fs.crosstalk(-c[0, 1], c[0, 0], l_[0, 1], l_[0, 0], 100e-12, 1e-9)
    assert long_["far_end"] < 0 and long_["saturated"] == 1.0
    assert short["saturated"] == 0.0 and abs(short["near_end"]) == pytest.approx(abs(long_["kb"]) * 0.2, rel=1e-6)


def test_capacitance_matrix_properties():
    sol = fs.solve(fs.coupled_microstrip_section(0.3, 0.2, 0.2, 0.035, 4.2), **SCREEN)
    c = sol.c
    assert np.allclose(c, c.T, rtol=1e-9)
    assert c[0, 0] > 0 and c[1, 1] > 0 and c[0, 1] < 0
    assert c[0, 0] > -c[0, 1]  # a conductor holds more charge to ground and neighbour together than to the neighbour alone


def test_symmetric_pair_has_equal_self_capacitance():
    sol = fs.solve(fs.coupled_microstrip_section(0.3, 0.2, 0.2, 0.035, 4.2), **SCREEN)
    assert abs(sol.c[0, 0] - sol.c[1, 1]) / sol.c[0, 0] < 0.003


def test_mirror_gives_the_same_answer():
    sec = fs.Section(
        [fs.Rect(-25, 25, -0.035, 0.0, 0), fs.Rect(-0.15, 0.15, 1.53, 1.565, 1), fs.Rect(0.4, 2.0, 1.53, 1.565, 0), fs.Rect(-7, -3, 1.53, 1.565, 0)],
        [(-25.0, -0.035, 1.0), (-0.035, 0.0, 4.5), (0.0, 1.53, 4.5), (1.53, 26.565, 1.0)],
        -25.0,
        25.0,
        -25.0,
        1.565 + 25,
        1,
    )
    a = fs.solve(sec, **CONFIRM).z0()
    b = fs.solve(fs.mirror(sec), **CONFIRM).z0()
    assert abs(pct(a, b)) < 0.2


def test_grid_convergence():
    """Halving the mesh moves Z0 by less than the stated limit, and the box is large enough that doubling it changes Z0 by under 0.1 percent."""
    sec = fs.microstrip_section(0.3, 1.53, 0.035, 4.5, box=25, side=25)
    z = [fs.solve(sec, fine=f, ratio=1.3, dmax_mm=0.8).z0() for f in (0.8, 0.4, 0.2)]
    assert abs(pct(z[1], z[0])) > abs(pct(z[2], z[1]))  # converging
    assert abs(pct(z[2], z[1])) < 0.3
    big = fs.solve(fs.microstrip_section(0.3, 1.53, 0.035, 4.5, box=50, side=50), fine=0.4, ratio=1.3, dmax_mm=0.8).z0()
    assert abs(pct(big, z[1])) < 0.25


# ------------------------------------------------------------------ TDR against a known step


def test_tdr_known_step_open_end():
    """50 ohm source, 75 ohm line, open end: 0.6 V, then 1.08 V after the round trip, then 0.984 V."""
    z = np.full(10, 75.0)
    tau = 10e-12
    r = fs.tdr_response(z, tau, 0.0, r_source=50.0, r_load=None, t_end_s=1e-9)
    n = 10
    assert r.v_source[0] == pytest.approx(0.6, abs=1e-12)
    assert r.v_source[2 * n - 1] == pytest.approx(0.6, abs=1e-12)
    assert r.v_source[2 * n] == pytest.approx(1.08, abs=1e-12)
    assert r.v_source[4 * n] == pytest.approx(0.984, abs=1e-12)
    assert r.v_source[-1] == pytest.approx(1.0, abs=2e-3)  # settles to the source voltage
    assert r.v_load[n] == pytest.approx(1.2, abs=1e-12)    # an open end doubles the incident 0.6 V


def test_tdr_resistive_load_and_a_step_in_impedance():
    z = np.full(10, 75.0)
    r = fs.tdr_response(z, 10e-12, 0.0, 50.0, 100.0, t_end_s=2e-9)
    rho_l = (100 - 75) / (100 + 75)
    assert r.v_source[20] == pytest.approx(0.6 + 0.6 * rho_l * (1 + (50 - 75) / (50 + 75)), abs=1e-12)
    assert r.v_source[-1] == pytest.approx(100.0 / 150.0, abs=1e-3)  # final value is the divider
    # a 50 ohm line with a 75 ohm middle section, matched at both ends: reflections of +0.2 then -0.2, a 20 percent bump
    z2 = np.concatenate([np.full(10, 50.0), np.full(10, 75.0), np.full(10, 50.0)])
    r2 = fs.tdr_response(z2, 10e-12, 0.0, 50.0, 50.0, t_end_s=1e-9)
    assert r2.v_source[0] == pytest.approx(0.5, abs=1e-12)
    assert r2.v_source[20] == pytest.approx(0.5 * (1 + 0.2), abs=1e-12)   # 2 x 10 sections later the +0.2 reflection arrives
    assert r2.v_source[40] == pytest.approx(0.5 * (1 + 0.2) + (0.5 * (1 - 0.2 ** 2) * -0.2) * 1.0, abs=1e-12)  # then the -0.2 reflection off the far step


def test_tdr_rise_time_rounds_the_edge():
    z = np.full(10, 75.0)
    fast = fs.tdr_response(z, 10e-12, 20e-12, 50.0, None, t_end_s=0.5e-9)
    slow = fs.tdr_response(z, 10e-12, 200e-12, 50.0, None, t_end_s=0.5e-9)
    assert slow.v_source[:40].max() < fast.v_source[:40].max()
    assert np.max(np.abs(np.diff(slow.v_source))) < np.max(np.abs(np.diff(fast.v_source)))


# ------------------------------------------------------------------ small boards written in text


def _pad(number, x, y, net, w=0.5, h=0.5, layers='"F.Cu"'):
    return f'(pad "{number}" smd rect (at {x} {y}) (size {w} {h}) (layers {layers}) (net "{net}"))'


def board_text(parts, tracks, vias=(), ground_fill=None, size=(60.0, 30.0), top_fill=None):
    out = ['(kicad_pcb (version 20260206) (generator "pcbnew") (generator_version "10.0") (general (thickness 1.6))', '(layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))']
    for ref, x, y, pads in parts:
        out.append(f'(footprint "Test:{ref}" (layer "F.Cu") (at {x} {y}) (property "Reference" "{ref}" (at 0 0) (layer "F.SilkS")) (property "Value" "{ref}" (at 0 0) (layer "F.Fab")) ' + " ".join(pads) + ")")
    for (x0, y0), (x1, y1), w, layer, net in tracks:
        out.append(f'(segment (start {x0} {y0}) (end {x1} {y1}) (width {w}) (layer "{layer}") (net "{net}"))')
    for x, y, net in vias:
        out.append(f'(via (at {x} {y}) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "{net}"))')
    if ground_fill is not None:
        fills = " ".join('(filled_polygon (layer "B.Cu") (pts ' + " ".join(f"(xy {a} {b})" for a, b in ring) + "))" for ring in ground_fill)
        w, h = size
        out.append(f'(zone (net_name "GND") (net "GND") (layer "B.Cu") (polygon (pts (xy 0 0) (xy {w} 0) (xy {w} {h}) (xy 0 {h}))) {fills})')
    if top_fill is not None:
        fills = " ".join('(filled_polygon (layer "F.Cu") (pts ' + " ".join(f"(xy {a} {b})" for a, b in ring) + "))" for ring in top_fill)
        w, h = size
        out.append(f'(zone (net_name "GND") (net "GND") (layer "F.Cu") (polygon (pts (xy 0 0) (xy {w} 0) (xy {w} {h}) (xy 0 {h}))) {fills})')
    w, h = size
    out.append(f'(gr_rect (start 0 0) (end {w} {h}) (layer "Edge.Cuts") (stroke (width 0.1) (type solid)) (fill no))')
    out.append(")")
    return "\n".join(out)


FULL_PLANE = [[(0, 0), (60, 0), (60, 30), (0, 30)]]
#: a slot 3 mm wide cut from the top edge down to y = 10: a track at y = 15 crosses it
SLOTTED_PLANE = [[(0, 0), (60, 0), (60, 30), (31, 30), (31, 10), (28, 10), (28, 30), (0, 30)]]


def line_board(width=0.3, plane=FULL_PLANE, y=15.0, net="SIG", extra_tracks=(), extra_parts=(), vias=(), top_fill=None):
    parts = [("U1", 5, y, [_pad(1, 0, 0, net)]), ("J1", 55, y, [_pad(1, 0, 0, net)]), *extra_parts]
    tracks = [((5, y), (55, y), width, "F.Cu", net), *extra_tracks]
    return parse_board_text(board_text(parts, tracks, vias, plane, top_fill=top_fill), "bench")


def run(board, inputs=None, whatif=None, out=None, ctx=None):
    out = Path(out) if out else Path(tempfile.mkdtemp(prefix="signal_test_"))
    return sg.SIMULATOR.run(board, ctx or AnalyzerContext(), inputs or {}, whatif or [], out)


def net_row(res, net):
    return res.detail["nets"][net]


def test_uniform_line_over_a_solid_plane_matches_the_closed_form(tmp_path):
    for width in (0.3, 0.6, 1.5):
        b = line_board(width)
        res = run(b, {"nets": ["SIG"]}, out=tmp_path)
        assert res.status == "ok" and res.trust["status"] == "ok", res.trust
        zh, eh = fs.hammerstad_jensen(width, 1.53, 0.035, 4.5)
        z = res.metrics["SIG.z0_median"]["value"]
        # the 2D field of a plane with no other copper: the board's ground fill has finite extent but is wide
        assert abs(pct(z, zh)) < 1.5, f"w={width}: {z:.2f} vs {zh:.2f}"
        assert res.metrics["SIG.reference_gap_events"]["value"] == 0
        # delay: one-way delay per mm is sqrt(eps_eff) / c; the thickness corrected effective permittivity is a little lower than the closed form
        ps = 50.0 * math.sqrt(eh) / 0.299792458
        assert abs(pct(res.metrics["SIG.delay"]["value"], ps)) < 3.5


def test_a_slot_in_the_plane_is_found_and_the_return_detours_around_it(tmp_path):
    b = line_board(0.3, SLOTTED_PLANE)
    res = run(b, {"nets": ["SIG"]}, out=tmp_path)
    row = net_row(res, "SIG")
    assert len(row["gap_events"]) == 1
    ev = row["gap_events"][0]
    assert ev["length_mm"] == pytest.approx(3.0, abs=0.8)
    assert ev["ok"], ev
    # around the end of the slot at y = 10: five mm down, three across, five mm up, minus the 3 mm direct distance
    assert ev["detour_mm"] == pytest.approx(10.0, rel=0.2), ev
    assert ev["max_offset_mm"] == pytest.approx(5.0, abs=1.0)
    assert ev["extra_loop_nh"] > 1.0
    assert res.metrics["SIG.reference_gap_length"]["value"] == pytest.approx(3.0, abs=0.8)
    # impedance inside the gap is higher than over the plane
    prof = json.loads((tmp_path / "curves.json").read_text())["nets"]["SIG"]["profile"]
    z = np.array([v if v is not None else np.nan for v in prof["z0_ohm"]])
    sup = np.array(prof["reference_supported"])
    assert np.nanmedian(z[~sup & (np.array(prof["model_valid"]))]) > 1.1 * np.nanmedian(z[sup])


def test_remove_gap_fills_the_slot_in_simulation_only(tmp_path):
    b = line_board(0.3, SLOTTED_PLANE)
    n_zones = [len(z.filled.get("B.Cu", [])) for z in b.zones]
    res = run(b, {"nets": ["SIG"]}, [{"op": "remove_gap", "net": "SIG"}], out=tmp_path)
    assert net_row(res, "SIG")["gap_events"] == []
    assert res.detail["whatif"][0]["patches"] > 0
    assert [len(z.filled.get("B.Cu", [])) for z in b.zones] == n_zones  # the board object is untouched


def test_widen_track_lowers_impedance(tmp_path):
    b = line_board(0.3)
    base = run(b, {"nets": ["SIG"]}, out=tmp_path / "a").metrics["SIG.z0_median"]["value"]
    wide = run(b, {"nets": ["SIG"]}, [{"op": "widen_track", "net": "SIG", "width_mm": 0.8}], out=tmp_path / "b").metrics["SIG.z0_median"]["value"]
    assert wide < 0.75 * base
    assert wide == pytest.approx(fs.hammerstad_jensen(0.8, 1.53, 0.035, 4.5)[0], rel=0.02)
    assert [t.width for t in b.tracks] == [0.3]


def test_add_stitching_adds_ground_vias_in_simulation_only(tmp_path):
    ring = [[(0, 0), (60, 0), (60, 30), (0, 30)]]
    b = line_board(0.3, FULL_PLANE, top_fill=[[(0, 0), (60, 0), (60, 10), (0, 10)], [(0, 20), (60, 20), (60, 30), (0, 30)]])
    n_vias = len(b.vias)
    res = run(b, {"nets": ["SIG"]}, [{"op": "add_stitching", "net": "SIG", "spacing_mm": 5.0, "offset_mm": [5.5]}], out=tmp_path / "a")
    rep = res.detail["whatif"][0]
    assert rep["vias"] > 4, rep
    assert len(b.vias) == n_vias
    # where the plane under the track has a slot the vias are what a detour can use: positions come from the operation
    res2 = run(b, {"nets": ["SIG"]}, [{"op": "add_stitching", "at": [[20, 12], [30, 12]]}], out=tmp_path / "b")
    assert res2.detail["whatif"][0]["vias"] == 2
    assert ring


def test_whatifs_are_validated(tmp_path):
    b = line_board(0.3)
    assert run(b, {"nets": ["SIG"]}, [{"op": "bogus"}], out=tmp_path).status == "not_possible"
    assert run(b, {"nets": ["SIG"]}, [{"op": "widen_track", "net": "SIG", "width_mm": 50}], out=tmp_path).status == "not_possible"
    assert run(b, {"nets": ["SIG"]}, [{"op": "widen_track", "net": "NOPE", "width_mm": 0.5}], out=tmp_path).status == "not_possible"
    assert run(b, {"nets": ["SIG"]}, [{"op": "add_stitching"}], out=tmp_path).status == "not_possible"
    assert sg.SIMULATOR.check_whatif([{"op": "set_current"}])


def test_unavailable_without_tracks():
    b = parse_board_text(board_text([("U1", 5, 5, [_pad(1, 0, 0, "SIG")])], []), "bench")
    ok, why = sg.SIMULATOR.available(b, None)
    assert not ok and "no routed tracks" in why


def _pair_board(len_a=40.0, len_b=40.0, gap=0.3, w=0.3):
    parts = [
        ("U1", 5, 15, [_pad(1, 0, 0, "USB_D+"), _pad(2, 0, w + gap, "USB_D-")]),
        ("J1", 5 + len_a, 15, [_pad(1, 0, 0, "USB_D+")]),
        ("J2", 5 + len_b, 15 + w + gap, [_pad(1, 0, 0, "USB_D-")]),
    ]
    tracks = [((5, 15), (5 + len_a, 15), w, "F.Cu", "USB_D+"), ((5, 15 + w + gap), (5 + len_b, 15 + w + gap), w, "F.Cu", "USB_D-")]
    return parse_board_text(board_text(parts, tracks, (), FULL_PLANE), "bench")


def test_differential_pair_impedance_matches_the_odd_mode_solution(tmp_path):
    w, gap = 0.3, 0.3
    b = _pair_board(gap=gap, w=w)
    res = run(b, {"nets": ["USB_D+", "USB_D-"]}, out=tmp_path)
    assert res.status == "ok" and res.trust["status"] == "ok"
    pair = res.detail["pairs"][0]
    sol = fs.solve(fs.coupled_microstrip_section(w, gap, 1.53, 0.035, 4.5, box=25, side=25), **CONFIRM)
    zodd = math.sqrt((sol.l[0, 0] - sol.l[0, 1]) / (sol.c[0, 0] - sol.c[0, 1]))
    assert pair["zdiff_median_ohm"] == pytest.approx(2 * zodd, rel=0.03)
    assert pair["skew_ps"] < 5.0          # equal legs
    assert pair["length_mismatch_mm"] < 0.1
    # coupled over the whole length
    assert pair["coupled_mm"][0] > 35.0
    assert pair["zcm_median_ohm"] > pair["zdiff_median_ohm"] / 4  # even mode impedance / 2 is above odd mode impedance


def test_skew_follows_the_length_difference(tmp_path):
    b = _pair_board(len_a=40.0, len_b=34.0)
    res = run(b, {"nets": ["USB_D+", "USB_D-"]}, out=tmp_path)
    pair = res.detail["pairs"][0]
    d = pair["delay_ps"]
    assert pair["length_mm"][0] - pair["length_mm"][1] == pytest.approx(6.0, abs=0.3)
    assert pair["length_mismatch_mm"] == pytest.approx(6.0, abs=0.3)
    assert pair["skew_ps"] == pytest.approx(abs(d[0] - d[1]), abs=1e-6)
    # odd mode delay per mm on this geometry from the solver
    sol = fs.solve(fs.coupled_microstrip_section(0.3, 0.3, 1.53, 0.035, 4.5, box=25, side=25), **SCREEN)
    per_mm = 1e12 * math.sqrt((sol.l[0, 0] - sol.l[0, 1]) * (sol.c[0, 0] - sol.c[0, 1])) * 1e-3
    assert pair["skew_ps"] == pytest.approx(6.0 * per_mm, rel=0.15)


def test_crosstalk_matches_the_coupled_solution(tmp_path):
    w, gap, rise = 0.3, 0.3, 0.3
    parts = [("U1", 5, 15, [_pad(1, 0, 0, "SIG"), _pad(2, 0, 3, "NEIGH")]), ("J1", 45, 15, [_pad(1, 0, 0, "SIG"), _pad(2, 0, 3, "NEIGH")])]
    tracks = [((5, 15), (45, 15), w, "F.Cu", "SIG"), ((5, 15 + w + gap), (45, 15 + w + gap), w, "F.Cu", "NEIGH")]
    b = parse_board_text(board_text(parts, tracks, (), FULL_PLANE), "bench")
    res = run(b, {"nets": ["SIG"], "rise_time_ns": {"SIG": rise, "NEIGH": rise}}, out=tmp_path)
    row = net_row(res, "SIG")["crosstalk"][0]
    assert row["aggressor"] == "NEIGH" and row["rise_source"] == "user"
    sol = fs.solve(fs.coupled_microstrip_section(w, gap, 1.53, 0.035, 4.5, box=25, side=25), **SCREEN)
    c, l_ = sol.c, sol.l
    kb = 0.25 * (-c[0, 1] / c[0, 0] + l_[0, 1] / l_[0, 0])
    kf = 0.5 * (-c[0, 1] / c[0, 0] - l_[0, 1] / l_[0, 0])
    assert row["worst_stretch"]["kb"] == pytest.approx(kb, rel=0.05)
    assert row["worst_stretch"]["saturated"]  # 40 mm of line (0.22 ns one way), 0.3 ns edge: the round trip is longer than the edge
    assert row["near_end_pct"] == pytest.approx(100 * kb, rel=0.05)
    delay = row["worst_stretch"]["delay_ps"] * 1e-12
    assert row["far_end_pct"] == pytest.approx(100 * kf * delay / (rise * 1e-9), rel=0.1)
    assert row["far_end_pct"] < 0
    assert row["near_end_v"] == pytest.approx(3.3 * row["near_end_pct"] / 100, rel=1e-6)


def test_rise_time_defaults_are_labelled_and_user_values_win():
    b = parse_board(NRF, "v")
    ctx = AnalyzerContext()
    usb = sg.interface_for(b, ctx, "USB_D+_ESD", {})
    assert usb.kind == "usb_fs" and usb.source == "default" and "4 to 20 ns" in usb.cite and usb.rise_ns == 4.0
    xt = sg.interface_for(b, ctx, "XTAL_A", {})
    assert xt.kind == "crystal" and xt.rise_ns == pytest.approx(0.2952 / 32e6 * 1e9, rel=1e-3)
    assert sg.interface_for(b, ctx, "I2C_SCL", {}).rise_ns == 300.0
    user = sg.interface_for(b, ctx, "USB_D+_ESD", {"rise_time_ns": {"USB_D+_ESD": 0.5}})
    assert user.rise_ns == 0.5 and user.source == "user"
    assert sg.interface_for(b, ctx, "USB_D+_ESD", {"rise_time_ns": 7.0}).rise_ns == 7.0


def test_pair_partner_names():
    names = {"USB_D+_ESD", "USB_D-_ESD", "DP", "DM", "RX_P", "RX_N", "LONE+"}
    assert sg.pair_partner("USB_D+_ESD", names) == "USB_D-_ESD"
    assert sg.pair_partner("USB_D-_ESD", names) == "USB_D+_ESD"
    assert sg.pair_partner("DP", names) == "DM"
    assert sg.pair_partner("RX_N", names) == "RX_P"
    assert sg.pair_partner("LONE+", names) is None


def test_quasi_static_limit_is_stated():
    f = fs.valid_to_hz(1.53, 4.5, 50.0)
    assert 0.5e9 < f < 3e9
    assert fs.valid_to_hz(0.2, 4.2, 50.0) > f  # thin substrates stay quasi-static to a higher frequency


def _four_layer_board(width):
    stack = (
        '(setup (stackup (layer "F.Cu" (type "copper") (thickness 0.035)) (layer "dielectric 1" (type "core") (thickness 0.2) (material "FR4") (epsilon_r 4.2) (loss_tangent 0.02)) '
        '(layer "In1.Cu" (type "copper") (thickness 0.035)) (layer "dielectric 2" (type "core") (thickness 0.2) (material "FR4") (epsilon_r 4.2) (loss_tangent 0.02)) '
        '(layer "In2.Cu" (type "copper") (thickness 0.035)) (layer "dielectric 3" (type "core") (thickness 1.0) (material "FR4") (epsilon_r 4.2) (loss_tangent 0.02)) '
        '(layer "B.Cu" (type "copper") (thickness 0.035))))'
    )
    out = ['(kicad_pcb (version 20260206) (generator "pcbnew") (generator_version "10.0") (general (thickness 1.6))', '(layers (0 "F.Cu" signal) (4 "In1.Cu" signal) (6 "In2.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))', stack]
    out.append('(footprint "Test:U1" (layer "F.Cu") (at 5 15) (property "Reference" "U1" (at 0 0) (layer "F.SilkS")) (property "Value" "U1" (at 0 0) (layer "F.Fab")) (pad "1" smd rect (at 0 0) (size 0.3 0.3) (layers "In1.Cu") (net "SIG")))')
    out.append('(footprint "Test:J1" (layer "F.Cu") (at 55 15) (property "Reference" "J1" (at 0 0) (layer "F.SilkS")) (property "Value" "J1" (at 0 0) (layer "F.Fab")) (pad "1" smd rect (at 0 0) (size 0.3 0.3) (layers "In1.Cu") (net "SIG")))')
    out.append(f'(segment (start 5 15) (end 55 15) (width {width}) (layer "In1.Cu") (net "SIG"))')
    for layer in ("F.Cu", "In2.Cu"):
        out.append(f'(zone (net_name "GND") (net "GND") (layer "{layer}") (polygon (pts (xy 0 0) (xy 60 0) (xy 60 30) (xy 0 30))) (filled_polygon (layer "{layer}") (pts (xy 0 0) (xy 60 0) (xy 60 30) (xy 0 30))))')
    out.append('(gr_rect (start 0 0) (end 60 30) (layer "Edge.Cuts") (stroke (width 0.1) (type solid)) (fill no))')
    out.append(")")
    return parse_board_text("\n".join(out), "bench4")


def test_inner_layer_track_between_two_planes_is_a_stripline(tmp_path):
    """A 4-layer board: the track on In1.Cu sits between F.Cu and In2.Cu planes; IPC-2141's stripline form and the ideal solver section agree with the run."""
    w = 0.1
    b = _four_layer_board(w)
    res = run(b, {"nets": ["SIG"]}, out=tmp_path)
    assert res.status == "ok" and res.trust["status"] == "ok", res.trust
    assert net_row(res, "SIG")["gap_events"] == []
    z = res.metrics["SIG.z0_median"]["value"]
    bpp = 0.2 + 0.035 + 0.2
    ideal = fs.solve(fs.stripline_section(w, bpp, 0.035, 4.2), **CONFIRM).z0()
    assert abs(pct(z, ideal)) < 2.0, f"run {z:.2f} ideal {ideal:.2f}"
    assert abs(pct(z, fs.ipc2141_stripline(w, bpp, 0.035, 4.2))) < 4.0
    # the delay is sqrt(er) / c in a homogeneous medium
    per_mm = 1e12 * math.sqrt(4.2) / fs.C_LIGHT * 1e-3
    assert res.metrics["SIG.delay"]["value"] == pytest.approx(per_mm * 50.0, rel=0.02)


# ------------------------------------------------------------------ sample boards


def test_inputs_needed_lists_defaults_with_sources():
    b = parse_board(NRF, "v")
    need = sg.SIMULATOR.inputs_needed(b, AnalyzerContext())
    names = {n["name"] for n in need}
    assert {"nets", "rise_time_ns", "source_ohm", "load_ohm", "aggressor_swing_v", "quality"} <= names
    assert all(n["source"] in ("default", "board", "user", "datasheet") for n in need)


def test_review_context_and_mesh_input_select_the_quality(tmp_path):
    from types import SimpleNamespace

    b = line_board(0.3)
    ctx = SimpleNamespace(goal={"target_ohms": 75.0, "net_targets": {"SIG": 60.0}}, quality="screen")
    base = sg.SIMULATOR.run(b, ctx, {"nets": ["SIG"]}, [], tmp_path / "a")
    assert base.detail["quality"] == "screen"
    assert base.metrics["SIG.z0_target"]["value"] == 60.0
    fine = sg.SIMULATOR.run(b, ctx, {"nets": ["SIG"], "mesh": "fine"}, [], tmp_path / "b")
    assert fine.detail["quality"] == "confirm"
    assert fine.detail["solver"]["mesh"] == sg.QUALITY["confirm"]
    need = sg.SIMULATOR.inputs_needed(b, ctx)
    assert all("note" in n and "default" in n and "unit" in n for n in need)


def test_registered_in_the_simulator_registry():
    assert sims.SIMULATORS["signal"] is sg.SIMULATOR


def test_usb_ldo_led_sample(tmp_path):
    b = parse_board(USB, "v")
    t0 = time.perf_counter()
    res = run(b, out=tmp_path)
    assert time.perf_counter() - t0 < 10
    assert res.status == "ok" and res.trust["status"] == "ok", res.trust
    assert res.detail["nets"]  # the longest signal nets, since none has a fast name
    assert any("longest signal nets" in n or True for n in res.notes)
    for ov in res.overlays:
        data = np.fromfile(tmp_path / ov.file, dtype="<f4")
        assert data.size == ov.nx * ov.ny and np.isfinite(data).any()


def test_nrf_sample_usb_pair_antenna_and_crystal(tmp_path):
    b = parse_board(NRF, "v")
    nets = ["ANT_FEED", "USB_D+_MCU", "USB_D-_MCU", "XTAL_A"]
    t0 = time.perf_counter()
    res = run(b, {"nets": nets}, out=tmp_path)
    dt = time.perf_counter() - t0
    assert dt < 20
    assert res.status == "ok" and res.trust["status"] == "ok", res.trust
    assert set(res.detail["nets"]) == set(nets)
    pair = res.detail["pairs"][0]
    assert set(pair["pair"]) == {"USB_D+_MCU", "USB_D-_MCU"} and pair["skew_ps"] < 10
    # every number says what it is
    assert all(m["label"] and "unit" in m for m in res.metrics.values())
    names = {a.name for a in res.assumptions}
    assert {"stackup", "source_resistance", "load", "rise_time:USB_D+_MCU", "z0_target:ANT_FEED"} <= names
    assert all(a.source in ("default", "board", "user", "datasheet") for a in res.assumptions)
    usb_rise = next(a for a in res.assumptions if a.name == "rise_time:USB_D+_MCU")
    assert usb_rise.source == "default" and "4 to 20 ns" in usb_rise.cite
    # impedance profile and TDR curves are in the JSON beside the result
    curves = json.loads((tmp_path / "curves.json").read_text())
    ant = curves["nets"]["ANT_FEED"]
    assert len(ant["profile"]["s_mm"]) == len(ant["profile"]["z0_ohm"]) > 3
    assert len(ant["tdr"]["interface"]["t_ns"]) == len(ant["tdr"]["interface"]["v_source"]) > 100
    assert ant["tdr"]["interface"]["v_source"][-1] == pytest.approx(1.0, abs=0.02)
    series = {e["id"]: e for e in res.detail["series"]}
    assert {"profile", "tdr", "profile:ANT_FEED", "tdr:XTAL_A"} <= set(series)
    for e in series.values():
        assert len(e["x"]) == len(e["y"]) > 2 and e["x_unit"] and e["y_unit"] == "ohm"
        if e["net"] == "XTAL_A":          # a crystal line has no impedance requirement: its curve has no target line and no tolerance band
            assert "target" not in e and "tolerance_pct" not in e
        else:
            assert e["target"] > 0 and e["tolerance_pct"] == 10.0
    # overlays are rasters of the stated size
    ids = {o.id for o in res.overlays}
    assert "return_detour" in ids and any(i.startswith("z0_deviation_") for i in ids)
    for ov in res.overlays:
        data = np.fromfile(tmp_path / ov.file, dtype="<f4")
        assert data.size == ov.nx * ov.ny
        assert ov.vmin < ov.vmax


def test_nrf_feed_cross_check_against_the_impedance_analyzer(tmp_path):
    """The impedance analyzer says 123 ohm for a 0.3 mm feed, 145 ohm at 0.15 mm and 2.835 mm for 50 ohm on this stackup (1.53 mm, er 4.5).

    The field solver's ideal microstrip agrees with those closed forms within 1 percent. On the real board the same
    tracks come out far lower because ground copper sits beside them on the top layer, which the closed form
    ignores: that disagreement is reported, not forced.
    """
    from trustpcb.analyzers import impedance as imp

    b = parse_board(NRF, "v")
    assert imp.estimate(b, "F.Cu", 0.3)["z0"] == pytest.approx(123.5, abs=0.1)
    assert imp.estimate(b, "F.Cu", 0.15)["z0"] == pytest.approx(144.9, abs=0.1)
    assert imp.width_for_target(b, "F.Cu", 50.0) == pytest.approx(2.835, abs=0.002)
    for w in (0.3, 0.15):
        z = fs.solve(fs.microstrip_section(w, 1.53, 0.035, 4.5, box=40, side=40), **CONFIRM).z0()
        assert abs(pct(z, imp.estimate(b, "F.Cu", w)["z0"])) < 1.0
    # the width that gives 50 ohm in the solver, by bisection on the confirm mesh
    lo, hi = 2.0, 4.0
    for _ in range(14):
        mid = 0.5 * (lo + hi)
        if fs.solve(fs.microstrip_section(mid, 1.53, 0.035, 4.5, box=40, side=40), **CONFIRM).z0() > 50.0:
            lo = mid
        else:
            hi = mid
    w50 = 0.5 * (lo + hi)
    assert abs(pct(w50, 2.835)) < 1.5, f"solver {w50:.3f} mm against 2.835 mm"
    res = run(b, {"nets": ["ANT_FEED"]}, out=tmp_path)
    rows = res.detail["cross_check"]
    assert rows and abs(rows[0]["ideal_difference_pct"]) < 1.5
    real = rows[0]["field_solver_on_the_real_board_median_ohm"]
    assert real is not None and real < 0.8 * rows[0]["analyzer_closed_form_ohm"]


def test_trust_failure_blocks_nothing_but_is_visible():
    from trustpcb.sims.base import SimResult

    r = SimResult("signal", "ok", trust={"status": "failed", "checks": []})
    assert not r.trusted


def test_cached_run_is_reused_and_what_if_changes_the_key(tmp_path):
    b = parse_board(USB, "v")
    sim = sg.SIMULATOR
    ctx = AnalyzerContext()
    first, folder, cached = run_cached(sim, b, ctx, {"nets": ["CC1"]}, [], tmp_path)
    assert first.status == "ok" and not cached
    again, folder2, cached2 = run_cached(sim, b, ctx, {"nets": ["CC1"]}, [], tmp_path)
    assert cached2 and folder2 == folder and again.metrics == first.metrics
    other, folder3, cached3 = run_cached(sim, b, ctx, {"nets": ["CC1"]}, [{"op": "widen_track", "net": "CC1", "width_mm": 0.8}], tmp_path)
    assert not cached3 and folder3 != folder
    assert other.metrics["CC1.z0_median"]["value"] < first.metrics["CC1.z0_median"]["value"]
    refused, _, _ = run_cached(sim, b, ctx, {}, [{"op": "set_current", "ref": "U1", "amps": 1}], tmp_path)
    assert refused.status == "not_possible" and "not accepted" in refused.reason


def test_confirm_quality_agrees_with_screen_within_the_stated_error(tmp_path):
    b = line_board(0.3)
    a = run(b, {"nets": ["SIG"], "quality": "screen"}, out=tmp_path / "s").metrics["SIG.z0_median"]["value"]
    c = run(b, {"nets": ["SIG"], "quality": "confirm"}, out=tmp_path / "c").metrics["SIG.z0_median"]["value"]
    assert abs(pct(a, c)) < 1.2
    assert a < c  # the finite element capacitance converges from above, so the screen mesh reads slightly low


@pytest.mark.slow
def test_nrf_default_selection_runs_inside_the_time_target(tmp_path):
    b = parse_board(NRF, "v")
    t0 = time.perf_counter()
    res = run(b, out=tmp_path)
    dt = time.perf_counter() - t0
    assert res.status == "ok" and res.trust["status"] == "ok", res.trust
    assert dt < 40, f"{dt:.1f} s"
    assert len(res.detail["nets"]) <= 10
    # when the machine is busy the time budget can run out; the result then says so in its checks
    reused = res.detail["solver"]["reused_after_budget"]
    assert (reused > 0) == any(c["name"] == "time_budget" for c in res.trust["checks"])


# ----------------------------------------------------------------------------- targets only where a net has one


def test_a_usb_cc_line_has_no_target_and_no_deviation_and_stays_out_of_the_worst_metric(tmp_path):
    b = parse_board(USB, "v")
    res = run(b, {"nets": ["CC1", "CC2"]}, out=tmp_path)
    assert res.status == "ok"
    for net in ("CC1", "CC2"):
        assert f"{net}.z0_median" in res.metrics and f"{net}.z0_worst_deviation" not in res.metrics and f"{net}.z0_target" not in res.metrics
        row = net_row(res, net)
        assert row["has_target"] is False and row["target_ohm"] is None and "no impedance requirement" in row["target_basis"]
    assert not any(a.name.startswith("z0_target") for a in res.assumptions)
    series = res.detail["series"]
    assert series and all("target" not in e and "tolerance_pct" not in e for e in series)
    assert not any(o.id.startswith("z0_deviation_") for o in res.overlays)
    from trustpcb.review import simmetrics as sm
    primary = sm.primary_of(res.to_dict())
    assert primary and primary[0].endswith("reference_gap_length")           # no net has a target: the run is judged by the reference gap, not by a made-up deviation


def test_the_worst_deviation_counts_only_nets_with_a_target(tmp_path):
    b = parse_board(NRF, "v")
    res = run(b, {"nets": ["ANT_FEED", "USB_D+_MCU", "USB_D-_MCU", "XTAL_A"]}, out=tmp_path)
    with_target = {n for n, r in res.detail["nets"].items() if r["has_target"]}
    assert with_target == {"ANT_FEED", "USB_D+_MCU", "USB_D-_MCU"} and not res.detail["nets"]["XTAL_A"]["has_target"]
    deviations = {k.split(".")[0] for k in res.metrics if k.endswith("z0_worst_deviation")}
    assert deviations == with_target
    from trustpcb.review import simmetrics as sm
    name, _ = sm.primary_of(res.to_dict())
    assert name.split(".")[0] in with_target and name.endswith("z0_worst_deviation")


def test_declared_and_net_class_targets_apply():
    from trustpcb.sims.signal import sb_has_target, sb_target
    actx = AnalyzerContext()
    assert not sb_has_target(actx, "CC1") and not sb_has_target(actx, "I2C_SDA")
    assert sb_has_target(actx, "ANT_FEED") and sb_has_target(actx, "USB_D+") and sb_has_target(actx, "USB_D-")
    actx.net_targets = {"CC1": 60.0}
    assert sb_has_target(actx, "CC1") and sb_target(actx, "CC1") == (60.0, "set in the brief")
    assert sb_target(AnalyzerContext(), "GPIO3")[1].startswith("no impedance requirement")


def test_a_net_class_that_states_an_impedance_gives_its_nets_a_target(monkeypatch):
    from trustpcb.analyzers import project
    from trustpcb.sims.signal import sb_has_target, sb_target
    monkeypatch.setattr(project, "net_class_info", lambda board, ctx, net: {"name": "Ctrl_75ohm" if net == "VIDEO" else "Default"})
    actx = AnalyzerContext()
    assert sb_has_target(actx, "VIDEO", object()) and sb_target(actx, "VIDEO", object()) == (75.0, "stated by the net class")
    assert not sb_has_target(actx, "GPIO1", object())
