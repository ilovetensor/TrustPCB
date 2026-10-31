"""Thermal simulator: closed-form benchmarks, a track cross-check, what-ifs, trust checks and the sample boards."""

from __future__ import annotations

import math
import time
from pathlib import Path

import numpy as np
import pytest

from trustpcb.pcb import geom
from trustpcb.pcb.model import Board, Footprint, Layer, Net, Pad, Track, Via, Zone, parse_board
from trustpcb.sims import thermal as T

SERVER = Path(__file__).resolve().parents[2]
SAMPLES = SERVER.parent / "samples"
NRF = SAMPLES / "nrf52840-node" / "nrf52840_rf_iot_node.kicad_pcb"
USB = SAMPLES / "usb-ldo-led" / "usb_ldo_led_node.kicad_pcb"
SIGMA = T.SIGMA


# ----------------------------------------------------------------------------- closed-form benchmarks


def uniform_model(nx, ny, d_mm, kt, nl=1, gz_cell=0.0):
    """kt is the sheet conductance k*t in W/K of every layer; gz_cell the W/K between layers in every cell."""
    area = np.ones((ny, nx))
    gx = [np.full((ny, nx - 1), kt) for _ in range(nl)]
    gy = [np.full((ny - 1, nx), kt) for _ in range(nl)]
    gz = [np.full((ny, nx), gz_cell) for _ in range(nl - 1)]
    return T.Model(ny, nx, d_mm, area, gx, gy, gz)


def test_plate_uniform_heat_two_faces():
    m = uniform_model(40, 30, 1.0, 0.01)
    area_tot = 40 * 30 * 1e-6
    q = np.zeros((1, 30, 40))
    q[0] = 0.8 / (40 * 30)
    h = 7.5
    sol = T.solve_model(m, q, None, 20.0, h, h, 0.0, 0.0)
    exact = 0.8 / (2 * h * area_tot)
    err = np.nanmax(np.abs(sol.temp_c - 20.0 - exact)) / exact
    assert err < 1e-9
    assert sol.balance_error < 1e-9


def test_plate_with_radiation_matches_scalar_root():
    m = uniform_model(40, 30, 1.0, 0.01)
    area_tot = 40 * 30 * 1e-6
    q = np.full((1, 30, 40), 2.0 / (40 * 30))
    h, eps, ta = 5.0, 0.85, 22.0
    sol = T.solve_model(m, q, None, ta, h, h, eps, eps)
    # bisection on 2 A (h dT + eps sigma (T^4 - Ta^4)) = Q
    lo, hi = 0.0, 500.0
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        tk = ta + 273.15 + mid
        f = 2 * area_tot * (h * mid + eps * SIGMA * (tk**4 - (ta + 273.15) ** 4)) - 2.0
        lo, hi = (mid, hi) if f < 0 else (lo, mid)
    exact = 0.5 * (lo + hi)
    got = float(np.nanmean(sol.temp_c)) - ta
    assert abs(got - exact) / exact < 1e-6, (got, exact)
    assert sol.balance_error < 1e-6
    assert sol.converged


def test_two_layers_two_sided_convection():
    nx, ny = 30, 30
    gz_cell = 2e-3
    m = uniform_model(nx, ny, 1.0, 0.02, nl=2, gz_cell=gz_cell)
    a = nx * ny * 1e-6
    q = np.zeros((2, ny, nx))
    q[0] = 1.5 / (nx * ny)
    ht, hb = 8.0, 4.0
    sol = T.solve_model(m, q, None, 25.0, ht, hb, 0.0, 0.0)
    gv = gz_cell * nx * ny
    # theta2 = gv (theta1 - theta2) / (hb A);  Q = ht A theta1 + hb A theta2
    k = gv / (hb * a + gv)  # theta2 = k theta1
    th1 = 1.5 / (ht * a + hb * a * k)
    th2 = k * th1
    assert abs(np.nanmean(sol.temp_c[0]) - 25 - th1) / th1 < 1e-9
    assert abs(np.nanmean(sol.temp_c[1]) - 25 - th2) / th2 < 1e-9


def test_via_resistance_single_column():
    g_via = 6.4e-3
    m = uniform_model(2, 2, 1.0, 1e-6, nl=2, gz_cell=0.0)
    for gz in m.gz:
        gz[0, 0] = g_via
    q = np.zeros((2, 2, 2))
    q[0, 0, 0] = 0.05
    a_cell = 1e-6
    sol = T.solve_model(m, q, None, 25.0, 0.0, 12.0, 0.0, 0.0)
    # only cell (0,0) is connected to the bottom through the via, lateral kt is tiny but non-zero:
    # compare to the series path with the lateral leak negligible
    t_bottom = sol.temp_c[1, 0, 0] - 25.0
    t_top = sol.temp_c[0, 0, 0] - 25.0
    assert (t_top - t_bottom) == pytest.approx(0.05 / g_via, rel=2e-3)
    assert t_bottom > 0


@pytest.mark.parametrize("source_at", ["end", "middle"])
def test_long_fin_point_source_matches_fin_equation(source_at):
    d_mm = 0.25
    nx, ny = 800, 2  # 200 mm long, 0.5 mm wide strip, copper only
    kt = T.K_CU * 35e-6  # W/K
    m = uniform_model(nx, ny, d_mm, kt)
    h = 6.0  # per face
    q = np.zeros((1, ny, nx))
    q_tot = 0.2
    i0 = 0 if source_at == "end" else nx // 2
    q[0, :, i0] = q_tot / ny
    sol = T.solve_model(m, q, None, 25.0, h, h, 0.0, 0.0)
    mm = math.sqrt(2 * h / kt)  # per metre
    length = nx * d_mm * 1e-3
    x = (np.arange(nx) + 0.5) * d_mm * 1e-3
    w = ny * d_mm * 1e-3
    ka = kt * w
    if source_at == "end":
        th0 = q_tot / (ka * mm * math.tanh(mm * length))
        exact = th0 * np.cosh(mm * (length - x)) / math.cosh(mm * length)
    else:
        half = length / 2
        th0 = q_tot / (2 * ka * mm * math.tanh(mm * half))
        s = np.abs(x - (nx // 2 + 0.5) * d_mm * 1e-3)
        exact = th0 * np.cosh(mm * (half - s)) / math.cosh(mm * half)
    got = sol.temp_c[0].mean(axis=0) - 25.0
    # skip the source cell itself: the point source is spread over one cell
    mask = np.arange(nx) != i0
    err = np.max(np.abs(got[mask] - exact[mask]) / exact[mask])
    assert err < 0.01, err
    assert sol.balance_error < 1e-9


# ----------------------------------------------------------------------------- synthetic boards


STACK = {
    "source": "board",
    "assumed": False,
    "total_thickness_mm": 1.6,
    "layers": [
        {"name": "F.Cu", "kind": "copper", "thickness_mm": 0.035},
        {"name": "dielectric 1", "kind": "dielectric", "thickness_mm": 1.53},
        {"name": "B.Cu", "kind": "copper", "thickness_mm": 0.035},
    ],
}


def rect(x0, y0, x1, y1):
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


def make_board(w=50.0, h=50.0, pour=True, tracks=(), vias=(), part=True, ref="U1", value="SOT-X"):
    layers = [Layer("F.Cu", "copper", 0), Layer("B.Cu", "copper", 31)]
    zones = []
    if pour:
        for layer in ("F.Cu", "B.Cu"):
            zones.append(Zone(f"zone:{layer}", "GND", [layer], {layer: [rect(0.5, 0.5, w - 0.5, h - 0.5)]}))
    pads, fps = [], []
    if part:
        cx, cy = w / 2, h / 2
        ring = geom.rect_ring(cx, cy, 3.0, 3.0)
        pads.append(Pad(f"pad:{ref}:1", ref, "1", "GND", cx, cy, 3.0, 3.0, 0.0, "rect", ["F.Cu"], None, [list(p) for p in ring]))
        fps.append(Footprint(f"ref:{ref}", ref, value, "Pkg:X", cx, cy, 0.0, "top", [cx - 2, cy - 2, cx + 2, cy + 2], None, [f"pad:{ref}:1"]))
    return Board(
        version_id="t", title="t", outline=[rect(0, 0, w, h)], bbox=[0, 0, w, h], layers=layers, stackup=STACK, nets=[Net("GND", "GND")],
        footprints=fps, pads=pads, tracks=list(tracks), vias=list(vias), zones=zones, keepouts=[],
    )


def run(board, inputs=None, whatif=None, tmp=None):
    return T.SIMULATOR.run(board, None, inputs or {}, whatif or [], tmp)


def test_board_mean_rise_is_exact_without_radiation(tmp_path):
    b = make_board()
    r = run(b, {"part_power": {"U1": 1.2}, "emissivity": 0.0, "pitch_mm": 0.5}, tmp=tmp_path)
    assert r.status == "ok"
    area = 50.0 * 50.0 * 1e-6
    exact = 1.2 / (2 * 6.0 * area)
    assert r.metrics["mean_rise"]["value"] == pytest.approx(exact, rel=0.01)
    assert r.metrics["energy_balance_error"]["value"] < 1e-6
    assert r.trust["status"] == "ok"
    assert {c["name"] for c in r.trust["checks"]} >= {"energy_balance", "grid_convergence", "sanity_range", "solver"}


def test_no_source_means_no_result(tmp_path):
    r = run(make_board(), {}, tmp=tmp_path)
    assert r.status == "not_possible"
    assert "No heat source" in r.reason
    assert not r.overlays


def test_ldo_without_load_and_defaults_off_gives_no_heat(tmp_path):
    b = make_board(ref="U1", value="AP2112K-3.3")
    b.pads[0].net = "VBUS"
    r = run(b, {"use_defaults": False}, tmp=tmp_path)
    assert r.status == "not_possible"
    assert any("no load current" in n for n in r.notes)


def test_ldo_drop_times_load_is_labelled(tmp_path):
    b = make_board(ref="U1", value="AP2112K-3.3")
    pad2 = Pad("pad:U1:2", "U1", "2", "3V3", 26, 26, 1, 1, 0, "rect", ["F.Cu"], None, [list(p) for p in geom.rect_ring(26, 26, 1, 1)])
    b.pads.append(pad2)
    b.footprints[0].pad_ids.append(pad2.id)
    b.pads[0].net = "VBUS_F"
    b._index()
    r = run(b, {"ldo": {"U1": {"load_a": 0.2}}}, tmp=tmp_path)
    assert r.status == "ok"
    assert r.metrics["total_power"]["value"] == pytest.approx(1.7 * 0.2)
    kinds = {a.name: a.source for a in r.assumptions}
    assert kinds["U1 load current"] == "user"
    assert kinds["U1 input voltage"] == "board"
    src = r.detail["sources"][0]
    assert "5 V - 3.3 V" in src["cite"]


def test_default_only_heat_is_flagged(tmp_path):
    b = make_board(ref="U1", value="AP2112K-3.3")
    pad2 = Pad("pad:U1:2", "U1", "2", "3V3", 26, 26, 1, 1, 0, "rect", ["F.Cu"], None, [list(p) for p in geom.rect_ring(26, 26, 1, 1)])
    b.pads.append(pad2)
    b.footprints[0].pad_ids.append(pad2.id)
    b.pads[0].net = "VBUS_F"
    b._index()
    r = run(b, {}, tmp=tmp_path)
    assert r.status == "ok"
    assert r.metrics["power_basis"]["value"] == "default"
    chk = {c["name"]: c for c in r.trust["checks"]}
    assert chk["heat_source_basis"]["status"] == "warn"


def test_junction_only_with_supplied_theta(tmp_path):
    b = make_board()
    r = run(b, {"part_power": {"U1": 0.5}, "parts": {"U1": {"theta_jb": 40.0, "tj_max_c": 125.0}}}, tmp=tmp_path)
    u1 = next(p for p in r.detail["parts"] if p["ref"] == "U1")
    assert u1["tj_c"] == pytest.approx(u1["t_board_mean_c"] + 0.5 * 40.0, abs=1e-6)
    assert "theta_jb" in u1["tj_basis"]
    assert u1["margin_k"] == pytest.approx(125.0 - u1["tj_c"], abs=1e-6)
    r2 = run(b, {"part_power": {"U1": 0.5}, "parts": {"U1": {"tj_max_c": 125.0}}}, tmp=tmp_path)
    u2 = next(p for p in r2.detail["parts"] if p["ref"] == "U1")
    assert "tj_c" not in u2
    assert "BOARD temperature only" in u2["basis"]


def test_whatifs_set_power_via_pour(tmp_path):
    b = make_board(pour=False)
    base = run(b, {"part_power": {"U1": 0.5}}, tmp=tmp_path)
    doubled = run(b, {"part_power": {"U1": 0.5}, "emissivity": 0.0}, [{"op": "set_part_power", "ref": "U1", "watts": 1.0}], tmp_path)
    single = run(b, {"part_power": {"U1": 0.5}, "emissivity": 0.0}, [], tmp_path)
    assert doubled.metrics["peak_rise"]["value"] == pytest.approx(2 * single.metrics["peak_rise"]["value"], rel=1e-6)
    assert doubled.metrics["peak_rise_change"]["value"] > 0
    # thermal vias under the part and a pour on the back
    wi = [{"op": "add_via", "rect": [24, 24, 26, 26], "pitch": 0.8, "drill": 0.3}, {"op": "add_pour", "layer": "B.Cu", "rect": [10, 10, 40, 40]}]
    cool = run(b, {"part_power": {"U1": 0.5}}, wi, tmp_path)
    assert cool.metrics["peak_rise"]["value"] < base.metrics["peak_rise"]["value"]
    assert cool.metrics["peak_rise_change"]["value"] < 0
    assert cool.detail["copper"]["whatif_vias"] >= 4
    # the design object is untouched
    assert len(b.vias) == 0 and len(b.zones) == 0


def test_unsupported_whatif_refused():
    assert T.SIMULATOR.check_whatif([{"op": "widen_track"}])
    assert T.SIMULATOR.check_whatif([{"op": "add_via"}, {"op": "set_part_power"}, {"op": "add_pour"}]) is None


def test_copper_lowers_peak_and_is_reported(tmp_path):
    b = make_board(pour=True)
    r = run(b, {"part_power": {"U1": 0.5}}, tmp=tmp_path)
    assert r.metrics["peak_rise_without_copper"]["value"] > 3 * r.metrics["peak_rise"]["value"]
    assert r.detail["copper"]["layers"][0]["copper_fraction"] > 0.9


def test_overlays_written(tmp_path):
    b = make_board()
    r = run(b, {"part_power": {"U1": 0.5}}, tmp=tmp_path)
    ids = {o.id for o in r.overlays}
    assert {"temp_board", "temp_F_Cu", "temp_B_Cu"} <= ids
    o = next(x for x in r.overlays if x.id == "temp_F_Cu")
    arr = np.fromfile(tmp_path / o.file, dtype="<f4").reshape(o.ny, o.nx)
    assert np.nanmax(arr) == pytest.approx(r.metrics["peak_temperature"]["value"], abs=0.01) or np.nanmax(arr) <= r.metrics["peak_temperature"]["value"] + 1e-3
    assert o.vmin == 25.0


# ----------------------------------------------------------------------------- IPC cross-check for a track


def track_board(width):
    t = Track("track:1", "N", "F.Cu", width, [0.0, 15.0], [60.0, 15.0], None, 60.0)
    b = make_board(60.0, 30.0, pour=False, part=False, tracks=[t])
    b.footprints.append(Footprint("ref:R1", "R1", "R", "R:R", 30, 15, 0, "top", [29, 14, 31, 16], None, []))
    b._index()
    return b


def test_track_rise_against_ipc2221_and_trend(tmp_path):
    rises = {}
    for amps in (1.0, 2.0, 3.0):
        r = run(track_board(1.0), {"net_currents": {"N": amps}, "pitch_mm": 0.25}, tmp=tmp_path)
        assert r.status == "ok"
        rises[amps] = r.metrics["peak_rise"]["value"]
    ipc = {a: T.ipc2221_rise_k(a, 1.0, 35.0) for a in rises}
    # same order as the curve fit: the board's own conduction and size set the difference
    for a in rises:
        assert 0.4 < rises[a] / ipc[a] < 2.5, (a, rises[a], ipc[a])
    # rise grows faster than I^2 because copper resistance grows with temperature (IPC fit: I^2.27)
    assert rises[2.0] / rises[1.0] > 4.0
    assert rises[3.0] / rises[2.0] > (3 / 2) ** 2
    # narrower track is hotter, as in IPC-2152
    narrow = run(track_board(0.5), {"net_currents": {"N": 1.0}, "pitch_mm": 0.25}, tmp=tmp_path)
    assert narrow.metrics["peak_rise"]["value"] > rises[1.0]


# ----------------------------------------------------------------------------- sample boards


@pytest.mark.parametrize("path,load", [(USB, 0.3), (NRF, 0.1)])
def test_sample_boards_fast_and_trusted(path, load, tmp_path):
    b = parse_board(path)
    ldos = T.find_ldos(b)
    assert len(ldos) == 1 and ldos[0]["vin_v"] == 5.0 and ldos[0]["vout_v"] == 3.3
    t0 = time.perf_counter()
    r = run(b, {"ldo": {ldos[0]["ref"]: {"load_a": load}}}, tmp=tmp_path)
    assert time.perf_counter() - t0 < 15
    assert r.status == "ok" and r.trusted, r.trust
    assert r.metrics["total_power"]["value"] == pytest.approx(1.7 * load)
    assert r.metrics["energy_balance_error"]["value"] < 0.01
    assert r.detail["parts"][0]["ref"] == ldos[0]["ref"]
    # a hand check: area-mean rise = P / ((h + h_rad) * 2 A) with h_rad about 5 W/m2K at these temperatures
    area = b.bbox[2] * b.bbox[3] * 1e-6
    hand = 1.7 * load / (2 * (6.0 + 0.9 * 4 * SIGMA * 300**3) * area)
    assert 0.6 * hand < r.metrics["mean_rise"]["value"] < 1.4 * hand


def four_layer(inner_pour: bool):
    b = make_board(pour=False)
    b.layers = [Layer("F.Cu", "copper", 0), Layer("In1.Cu", "copper", 1), Layer("In2.Cu", "copper", 2), Layer("B.Cu", "copper", 31)]
    b.stackup = {
        "source": "board", "assumed": False, "total_thickness_mm": 1.6,
        "layers": [
            {"name": "F.Cu", "kind": "copper", "thickness_mm": 0.035}, {"name": "dielectric 1", "kind": "dielectric", "thickness_mm": 0.2},
            {"name": "In1.Cu", "kind": "copper", "thickness_mm": 0.0175}, {"name": "dielectric 2", "kind": "dielectric", "thickness_mm": 1.065},
            {"name": "In2.Cu", "kind": "copper", "thickness_mm": 0.0175}, {"name": "dielectric 3", "kind": "dielectric", "thickness_mm": 0.2},
            {"name": "B.Cu", "kind": "copper", "thickness_mm": 0.035},
        ],
    }
    if inner_pour:
        b.zones = [Zone("zone:in1", "GND", ["In1.Cu"], {"In1.Cu": [rect(0.5, 0.5, 49.5, 49.5)]})]
    b.vias = [Via(f"via:{i}", "GND", 25 + 0.8 * (i % 3 - 1), 25 + 0.8 * (i // 3 - 1), 0.6, 0.3, ["F.Cu", "B.Cu"]) for i in range(9)]
    return b


def test_four_layer_inner_plane_spreads_heat(tmp_path):
    with_plane = run(four_layer(True), {"part_power": {"U1": 0.5}}, tmp=tmp_path)
    without = run(four_layer(False), {"part_power": {"U1": 0.5}}, tmp=tmp_path)
    assert with_plane.status == without.status == "ok"
    assert with_plane.detail["grid"]["layers"] == 4
    assert with_plane.metrics["peak_rise"]["value"] < without.metrics["peak_rise"]["value"]
    assert {o.id for o in with_plane.overlays} >= {"temp_In1_Cu", "temp_In2_Cu"}
    assert with_plane.trusted


# ----------------------------------------------------------------------------- the grid convergence measure


@pytest.mark.parametrize("pitch", [0.07, 0.113, 0.2, 0.31, 0.5])
def test_region_mean_covers_the_same_board_on_every_raster(pitch):
    """A mean over whole cells moves with the raster; a mean over a fixed region weighted by cell overlap does not (for a linear field it is the field at the region's centre)."""
    b = make_board()
    g = T.make_grid(b, pitch)
    xs = g.x0 + (np.arange(g.nx) + 0.5) * g.d
    field = np.tile(20.0 + 3.0 * xs, (g.ny, 1))
    got = T._region_mean(field, g, np.ones((g.ny, g.nx)), (23.0, 23.0, 27.0, 27.0), 1.0)
    assert got == pytest.approx(20.0 + 3.0 * 25.0, abs=0.02)


def test_region_mean_ignores_cells_with_no_board_and_a_region_off_the_board():
    b = make_board()
    g = T.make_grid(b, 0.5)
    area = np.ones((g.ny, g.nx))
    area[:, : g.nx // 2] = 0.0
    field = np.full((g.ny, g.nx), 40.0)
    field[:, : g.nx // 2] = np.nan
    assert T._region_mean(field, g, area, (30.0, 20.0, 34.0, 24.0)) == pytest.approx(40.0)
    assert T._region_mean(field, g, area, (2.0, 20.0, 6.0, 24.0)) is None


def test_a_hot_regulator_passes_grid_convergence_at_screen_and_confirm_pitch_and_the_text_has_the_percentages(tmp_path):
    b = parse_board(USB)
    ldo = T.find_ldos(b)[0]
    default = T.default_pitch(b, 2)
    for pitch in (default, round(default * 0.6, 3)):                 # the screen pitch and the confirm pitch (the simulator never goes below 0.1 mm)
        r = run(b, {"ldo": {ldo["ref"]: {"load_a": 0.5}}, "pitch_mm": pitch}, tmp=tmp_path)
        assert r.status == "ok", r.reason
        check = next(c for c in r.trust["checks"] if c["name"] == "grid_convergence")
        part = float(check["value"].split("part mean")[1].split("%")[0])
        assert check["status"] != "failed" and r.trusted and part < 8.0, check
        assert "board mean" in check["value"] and "peak" in check["value"]
        assert "pads plus 2.5 mm" in check["detail"] and "weighted by cell overlap" in check["detail"]


def test_the_part_mean_does_not_depend_on_where_the_pads_fall_in_the_raster():
    """The pad extent alone moved by about 5 % between rasters of almost the same pitch; the fixed region past the pads must hold within 5 % over all of them."""
    b = parse_board(USB)
    ldo = T.find_ldos(b)[0]
    stack = T.board_stack(b)
    params = T.Params(*[T.DEFAULTS[n][0] for n in ("ambient_c", "h_conv", "emissivity", "k_fr4_inplane", "k_fr4_through", "via_plating_um")])
    sources, _ = T.collect_sources(b, stack, {"ldo": {ldo["ref"]: {"load_a": 0.5}}}, [], [], [])
    sites = T.via_sites(b, [], stack)
    region = T.part_region(b, ldo["ref"])[0]
    means = []
    for pitch in (0.1, 0.12, 0.141, 0.15, 0.18):
        c = T.run_case(b, stack, params, pitch, sources, sites, [], params.emissivity)
        means.append(T._region_mean(c.sol.temp_c[T.part_layer(b, stack, ldo["ref"])], c.grid, c.model.area, region, T.CONV_MARGIN_MM) - params.ambient_c)
    assert (max(means) - min(means)) / np.mean(means) < 0.05, means


def test_the_failure_reason_shows_what_was_measured_against_its_limit():
    from trustpcb.review.simmetrics import trust_of
    check = {"name": "grid_convergence", "status": "failed", "value": "board mean 0.40 %, part mean 10.60 %, peak 9.00 %", "limit": "part mean 8 %", "detail": "0.085 mm raster against a coarser 0.127 mm raster."}
    out = trust_of({"status": "ok", "trust": {"status": "failed", "checks": [check]}})
    assert out["status"] == "fail" and "part mean 10.60 %" in out["reasons"][0] and "limit part mean 8 %" in out["reasons"][0] and "0.127 mm" in out["reasons"][0]
