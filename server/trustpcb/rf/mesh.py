"""Graded rectilinear mesh for the antenna model.

Rules:

* lines at every axis-aligned copper edge inside the fine zone, with the thirds-rule snap
  (one line a third of a cell outside the edge, one two thirds inside) where the copper is wide
  enough, so field peaks at copper edges sit at the right place in a cell;
* every gap between fixed lines is covered by at least two cells when it is a copper width or gap;
* cell size grows by at most 1.4 from one cell to the next (checked and repaired);
* at most 1/20 wavelength inside the substrate and in air (screen), finer for confirm;
* output lines are in scenario coordinates: board y is negated, because the worker flips nothing.

Two qualities: ``screen`` for comparing and ``confirm`` for absolute claims.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np
from shapely.geometry import Point, box

from . import geometry as geo
from .types import C0, RfSetup

RATIO = 1.4

QUALITY: dict[str, dict[str, float]] = {
    # h_feat: cell next to fine copper edges; h_zone: largest cell in the fine zone;
    # n_sub: cells per wavelength in the substrate; n_air: in air
    "screen": {"h_feat": 0.30, "h_zone": 1.20, "n_sub": 22.0, "n_air": 20.0, "pad_wl": 0.20, "hz_sub": 0.45, "min_across": 2.0},
    "confirm": {"h_feat": 0.20, "h_zone": 0.70, "n_sub": 30.0, "n_air": 26.0, "pad_wl": 0.25, "hz_sub": 0.30, "min_across": 2.0},
}


def effective_port_width(setup: RfSetup) -> tuple[float, str]:
    """Strip width the solver gets, clamped to 0.9 of the port height (ground layer to feed layer).

    The worker now reads the excitation axis from ``port.axis`` (always z), so the clamp is no longer needed to
    steer it. It stays because a wider port is a different physical model: results of the validated runs were
    made with it, and relaxing it needs fresh benchmark runs and a MODEL_VERSION bump."""
    z = setup.stackup["layer_z"]
    dz = abs(z[setup.port_layer] - z[setup.ground_layer])
    w = setup.port_width_mm
    if w <= 0:
        return 0.0, setup.port_axis
    return round(min(w, 0.9 * dz), 3), setup.port_axis


@dataclass
class Mesh:
    x: list[float]
    y: list[float]  # scenario y = -board y, increasing
    z: list[float]
    report: dict[str, Any]

    @property
    def cells(self) -> int:
        return len(self.x) * len(self.y) * len(self.z)


# --------------------------------------------------------------------------- 1D grading


def _grade(fixed: list[float], seeds: list[tuple[float, float]], cap, locked: set[tuple[float, float]], ratio: float = RATIO) -> list[float]:
    """Lines containing every ``fixed`` line, cells below cap(x) and growing by at most ``ratio``."""
    pts = sorted(set(round(v, 6) for v in fixed))
    sx = np.array([s[0] for s in seeds]) if seeds else np.zeros(0)
    ss = np.array([s[1] for s in seeds]) if seeds else np.zeros(0)
    g = 1.0 + 0.7 * (ratio - 1.0)

    def target(x: float) -> float:
        t = cap(x)
        if len(sx):
            t = min(t, float(np.min(ss * 1.0 + (g - 1.0) * np.abs(x - sx))))
        return max(t, 1e-3)

    out = [pts[0]]
    for a, b in zip(pts[:-1], pts[1:]):
        if (round(a, 6), round(b, 6)) in locked:
            out.append(b)
            continue
        xs = [a]
        while xs[-1] < b - 1e-9:
            step = target(xs[-1])
            step = min(step, target(min(xs[-1] + step, b)))
            xs.append(xs[-1] + step)
        n = len(xs) - 1
        # last cell may be a sliver: drop it into its neighbour when it is under half a cell
        if n > 1 and (b - xs[-2]) < 0.5 * (xs[-2] - xs[-3]):
            xs.pop(-1)
            n -= 1
            xs[-1] = xs[-1]
        xs = a + (np.array(xs[: n + 1]) - a) * ((b - a) / (xs[n] - a)) if xs[n] != a else np.array([a, b])
        xs[-1] = b
        out.extend(float(v) for v in xs[1:])
    arr = np.array(out)
    keep = np.r_[True, np.diff(arr) > 1e-4]
    out = [float(v) for v in arr[keep]]
    return _repair_ratio(out, set(round(p, 6) for p in pts), locked, ratio)


def _repair_ratio(lines: list[float], fixed: set[float], locked, ratio: float) -> list[float]:
    """Insert lines so neighbouring cells differ by at most ``ratio``.

    A cell more than ``ratio`` times its smaller neighbour is cut so that the piece next to the
    small cell is 1.3 times it and the rest is checked again: a jump becomes a geometric
    progression. A violation that cannot be cut cleanly (the cell is under 1.6 times its
    neighbour) is left and shows in the reported growth ratio.
    """
    lines = list(lines)
    skip: set[float] = set()
    for _ in range(3000):
        d = np.diff(lines)
        r = d[1:] / d[:-1]
        bad = [i for i in np.nonzero((r > ratio * 1.005) | (r < 1 / (ratio * 1.005)))[0] if round(lines[i + 1], 6) not in skip]
        if not bad:
            break
        i = int(bad[0])
        if r[i] > 1:
            j, small = i + 1, d[i]
        else:
            j, small = i, d[i + 1]
        piece = 1.3 * small
        if d[j] < 1.6 * small + 1e-9 or d[j] - piece < 0.3 * piece:
            skip.add(round(lines[i + 1], 6))
            continue
        at = lines[j] + piece if r[i] > 1 else lines[j + 1] - piece
        lines.insert(j + 1, at)
    return lines


# --------------------------------------------------------------------------- building


def _axis_edges(geom, tol: float = 1e-3, min_len: float = 0.25):
    """Axis-aligned boundary edges of the copper: [(axis, coordinate, metal side +-1, y0, y1)]."""
    out = []
    for a, b, _ in geo.polygon_edges(geom):
        dx, dy = b[0] - a[0], b[1] - a[1]
        if abs(dx) < tol and abs(dy) >= min_len:  # vertical: coordinate is x; metal on the left
            out.append(("x", (a[0] + b[0]) / 2, -1 if dy > 0 else 1, (min(a[1], b[1]), max(a[1], b[1]))))
        elif abs(dy) < tol and abs(dx) >= min_len:
            out.append(("y", (a[1] + b[1]) / 2, 1 if dx > 0 else -1, (min(a[0], b[0]), max(a[0], b[0]))))
    return out


def build_mesh(model: geo.CopperModel, setup: RfSetup, quality: str, *, eps_r: float | None = None, port_scn: dict | None = None) -> Mesh:
    """Build the mesh; when the 2-cells-across rule is not met, refine around narrow copper and retry."""
    mesh = None
    for k, scale in enumerate((1.0, 0.8, 0.64, 0.5, 0.4)):
        mesh = _build_mesh(model, setup, quality, eps_r, scale)
        across = mesh.report.get("min_cells_across_feature")
        if across is None or across >= 2.0 * 1.03:
            break
    mesh.report["narrow_refinements"] = k
    return mesh


def _build_mesh(model: geo.CopperModel, setup: RfSetup, quality: str, eps_r, narrow_scale: float) -> Mesh:
    q = QUALITY[quality]
    slabs = setup.stackup["slabs"]
    er_max = eps_r if eps_r is not None else max(s["epsilon_r"] for s in slabs)
    f_lo, f_hi = setup.sweep_hz
    lam0 = C0 / f_hi * 1e3
    lam_sub = lam0 / math.sqrt(er_max)
    h_sub = lam_sub / q["n_sub"]
    h_air = lam0 / q["n_air"]
    h_feat, h_zone = q["h_feat"], min(q["h_zone"], h_sub)
    pad = q["pad_wl"] * C0 / setup.f0_hz * 1e3
    reg = model.region
    fz = model.fine_zone
    notes: list[str] = []

    # ---- fixed lines, in board coordinates -----------------------------------
    fbox = box(*fz)
    edges: dict[str, dict[float, int]] = {"x": {}, "y": {}}  # coordinate -> metal side, 0 when not wide
    for lname, geom in model.layers.items():
        g = geom.intersection(fbox)
        if g.is_empty:
            continue
        gb = g.buffer(1e-6)
        for axis, c, m, span in _axis_edges(g):
            mid = (span[0] + span[1]) / 2
            wide = all(gb.contains(Point(c + m * k * h_feat, mid) if axis == "x" else Point(mid, c + m * k * h_feat)) for k in (0.5, 1.5, 2.5))
            key = round(c, 3)
            cur = edges[axis].get(key)
            edges[axis][key] = m if (wide and cur in (None, m)) else 0

    mandatory = {"x": [], "y": []}
    px, py = setup.port_xy
    pw, _ = effective_port_width(setup)
    if pw > 0 and setup.port_axis == "x":
        mandatory["x"] += [px - pw / 2, px, px + pw / 2]
        mandatory["y"] += [py]
    elif pw > 0:
        mandatory["y"] += [py - pw / 2, py, py + pw / 2]
        mandatory["x"] += [px]
    else:
        mandatory["x"].append(px)
        mandatory["y"].append(py)
    out_box = {"x": (reg[0] - pad, reg[2] + pad), "y": (reg[1] - pad, reg[3] + pad)}
    # narrow antenna tracks: seed the mesh finer than the track width / min_across along their extent
    narrow = [i for i in model.items if i.antenna and i.kind == "track" and i.points and i.width and i.width < 1.6]
    fixed: dict[str, list[float]] = {"x": [], "y": []}
    seeds: dict[str, list[tuple[float, float]]] = {"x": [], "y": []}
    locked: dict[str, set] = {"x": set(), "y": set()}
    thirds = 0
    for ai, axis in enumerate("xy"):
        lo, hi = reg[ai], reg[ai + 2]
        flo, fhi = max(fz[ai], lo), min(fz[ai + 2], hi)
        must = sorted(set(round(v, 6) for v in mandatory[axis] + [out_box[axis][0], out_box[axis][1], lo, hi, flo, fhi]))
        seeds[axis] += [(lo, h_sub), (hi, h_sub), (flo, h_zone), (fhi, h_zone)]
        # greedy pass over copper edges in sorted order; thirds pair where there is room
        final: list[float] = []
        for c in sorted(edges[axis]):
            m = edges[axis][c]
            last = final[-1] if final else -1e9
            if m:
                lo_l, hi_l = sorted((c - m * h_feat / 3, c + m * 2 * h_feat / 3))
                if lo_l - last >= 0.7 * h_feat and all(abs(lo_l - v) >= 0.7 * h_feat and abs(hi_l - v) >= 0.7 * h_feat for v in must):
                    final += [lo_l, hi_l]
                    thirds += 1
                    continue
            if c - last >= 0.7 * h_feat and all(abs(c - v) >= 0.7 * h_feat for v in must):
                final.append(c)
        fixed[axis] = sorted(set(final) | set(must))
        for v in final:
            seeds[axis].append((v, h_feat))
        for v in must:
            seeds[axis].append((v, min(h_feat, h_zone) if flo <= v <= fhi else h_sub))
        for it in narrow:
            h_n = narrow_scale * it.width / (q["min_across"] + 1.2)
            for (ax_, ay_), (bx_, by_) in zip(it.points[:-1], it.points[1:]):
                length = math.hypot(bx_ - ax_, by_ - ay_)
                n = max(1, int(length / 0.3))
                for k in range(n + 1):
                    t = k / n
                    seeds[axis].append((ax_ + (bx_ - ax_) * t if axis == "x" else ay_ + (by_ - ay_) * t, h_n))
        # a gap closer than min_across * h_feat between fixed lines is covered by min_across cells
        for a_, b_ in zip(fixed[axis][:-1], fixed[axis][1:]):
            if (round(a_, 6), round(b_, 6)) in locked[axis]:
                continue
            gap = b_ - a_
            if gap < q["min_across"] * h_feat * 1.5:
                seeds[axis].append(((a_ + b_) / 2, max(gap / q["min_across"], 0.05)))
        # snap lines onto the seeded fine spacing is not needed: grading covers each gap

    def cap_for(axis):
        ai = 0 if axis == "x" else 1
        lo, hi = reg[ai], reg[ai + 2]
        flo, fhi = max(fz[ai], lo), min(fz[ai + 2], hi)

        def cap(x):
            if flo <= x <= fhi:
                return h_zone
            if lo <= x <= hi:
                return h_sub
            return h_air

        return cap

    xs = _grade(fixed["x"], seeds["x"], cap_for("x"), locked["x"])
    # y: grade in board y, then negate
    ys_b = _grade(fixed["y"], seeds["y"], cap_for("y"), locked["y"])
    ys = sorted(-v for v in ys_b)

    # ---- z -----------------------------------------------------------------------
    lz = setup.stackup["layer_z"]
    zs = sorted(lz.values())
    zfix = list(zs)
    zseeds = []
    hz_sub = min(q["hz_sub"], h_sub)
    for s in slabs:
        zfix += [s["zmin"], s["zmax"]]
    zlo, zhi = min(zs) - pad, max(zs) + pad
    zfix += [zlo, zhi]

    def zcap(z):
        return hz_sub if (zs[0] - 1e-9 <= z <= zs[-1] + 1e-9) else h_air

    for v in zs:
        zseeds.append((v, hz_sub))
    zl = _grade(zfix, zseeds, zcap, set())

    dbg = {a: [(round(v, 3)) for v in fixed[a]] for a in 'xy'}
    dbg_seeds = {a: seeds[a] for a in 'xy'}
    mesh = Mesh([round(v, 5) for v in xs], [round(v, 5) for v in ys], [round(v, 5) for v in zl], {})
    mesh.report = _report(mesh, model, setup, q, quality, thirds, lam_sub, er_max)
    mesh.report["notes"] = notes
    mesh._dbg = (dbg, dbg_seeds)
    return mesh


# --------------------------------------------------------------------------- checks


def _local_cell(lines: np.ndarray, v: float) -> float:
    i = int(np.clip(np.searchsorted(lines, v) - 1, 0, len(lines) - 2))
    return float(lines[i + 1] - lines[i])


def _report(mesh: Mesh, model, setup: RfSetup, q, quality: str, thirds: int, lam_sub: float, er_max: float) -> dict[str, Any]:
    x, y, z = (np.asarray(a) for a in (mesh.x, mesh.y, mesh.z))
    rep: dict[str, Any] = {"quality": quality, "cells": mesh.cells, "nx": len(x), "ny": len(y), "nz": len(z), "thirds_edges": thirds}
    ratios = []
    for a in (x, y, z):
        d = np.diff(a)
        r = d[1:] / d[:-1]
        ratios.append(float(np.max(np.maximum(r, 1 / r))))
    rep["max_growth_ratio"] = round(max(ratios), 3)
    rep["min_cell_mm"] = round(float(min(np.diff(x).min(), np.diff(y).min(), np.diff(z).min())), 4)
    # largest cell inside the substrate volume (the worker's cells-per-wavelength rule)
    reg = model.region
    ymin_s, ymax_s = -reg[3], -reg[1]
    zmin, zmax = min(setup.stackup["layer_z"].values()), max(setup.stackup["layer_z"].values())
    def rng(a, lo, hi):
        s = a[(a >= lo - 1e-9) & (a <= hi + 1e-9)]
        return float(np.max(np.diff(s))) if len(s) > 1 else 0.0
    worst_sub = max(rng(x, reg[0], reg[2]), rng(y, ymin_s, ymax_s), rng(z, zmin, zmax))
    rep["max_cell_substrate_mm"] = round(worst_sub, 4)
    rep["cells_per_wavelength_substrate"] = round(lam_sub / worst_sub, 2) if worst_sub else None
    rep["substrate_eps_r"] = er_max
    rep["lambda_substrate_mm"] = round(lam_sub, 3)
    # cells across the narrowest antenna copper and gaps
    across, where = _cells_across(model, setup, x, y)
    rep["narrowest_feature_mm"] = setup.narrowest_mm
    rep["narrowest_feature"] = setup.narrowest_what
    rep["min_cells_across_feature"] = round(across, 2) if across is not None else None
    rep["min_cells_across_where"] = where
    rep["required_cells_across"] = 2.0
    rep["unresolved_small_gaps"] = [w for w in setup.warnings if "gaps between antenna copper" in w]
    ok_wave = (rep["cells_per_wavelength_substrate"] or 0) >= 20.0
    ok_across = across is None or across >= 2.0
    rep["rule_ok"] = bool(ok_wave and ok_across)
    rep["rule_reasons"] = []
    if not ok_wave:
        rep["rule_reasons"].append(f"only {rep['cells_per_wavelength_substrate']} cells per wavelength in the substrate (need 20)")
    if not ok_across:
        rep["rule_reasons"].append(f"only {across:.2f} cells across the narrowest antenna feature at {where} (need 2)")
    return rep


def _cells_across(model, setup: RfSetup, x: np.ndarray, y: np.ndarray):
    """Smallest (feature width / local cell across it) over antenna tracks and resolvable gaps."""
    best, where = None, None
    items = geo.collect_items_cached(model)
    ant = [i for i in items if i.antenna]
    for i in ant:
        if i.kind == "track" and i.points and i.width:
            segs = list(zip(i.points[:-1], i.points[1:]))
            for a, b in segs:
                dx, dy = b[0] - a[0], b[1] - a[1]
                n = math.hypot(dx, dy)
                if n < 1e-9:
                    continue
                mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
                hx, hy = _local_cell(x, mx), _local_cell(y, -my)
                c = i.width / max(hx, hy)
                if best is None or c < best:
                    best, where = c, f"{i.source} at ({mx:.1f}, {my:.1f})"
    return best, where
