"""Steady-state conduction on real copper: a resistor network over a grid, solved with scipy.

The problem is the Laplace equation for the voltage in a thin conducting sheet,
``div(sigma t grad V) = 0``, with the source pads held at a fixed voltage and the load pads drawing
current. This module is geometry in, numbers out; ``power.py`` turns a board net into a ``Problem``.

* Copper of each layer is rasterised with supersampling to a coverage fraction per cell (0 to 1).
  The conductance between two neighbouring cells is ``sigma t`` times the harmonic mean of their
  coverage, which makes a straight track of any width and offset give ``R = rho L / (w t)`` without
  depending on where the cell edges fall.
* Layers are joined by barrels (vias and plated holes): a resistor ``rho L / (pi t_p (d + t_p))``
  per layer pair, shared over the cells under the pad.
* Sources are fixed at zero and the unknown is the drop ``u = V_source - V``; a load extracts a
  current spread over its pad.
* The sparse system is factorised once (SuperLU) and solved for every load at one amp, which gives
  the matrix ``Z`` of drop per amp between every pair of terminals. The field at the assumed
  currents is ``sum(I_k u_k)``.

Copper is ``RHO_CU_20`` at 20 C, scaled by ``ALPHA_CU`` per kelvin (the resistance rises about
0.39 % per degree, so a board at 85 C carries about 25 % more drop than at 20 C).
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

import numpy as np
from PIL import Image, ImageDraw
from scipy import sparse
from scipy.sparse import csgraph
from scipy.sparse.linalg import splu

#: resistivity of annealed copper at 20 C, ohm mm (1.724e-8 ohm m). Plated copper is a little higher.
RHO_CU_20 = 1.724e-5
#: temperature coefficient of copper resistance per kelvin
ALPHA_CU = 0.00393

DIAG_WEIGHT = 0.0       # weight of each diagonal link; an edge link then has 1 - 2 * DIAG_WEIGHT
MEANFN = lambda a, b: np.sqrt((a * a + b * b) / 2.0)   # quadratic mean of the two cells coverage
MIN_COVER = 0.01        # cells with less copper than this are dropped
BARREL_COVER = 0.3      # a barrel joins a layer only where the cell has at least this much copper
MAX_RASTER_PIXELS = 120_000_000


def rho_at(temp_c: float) -> float:
    """Copper resistivity in ohm mm at ``temp_c``."""
    return RHO_CU_20 * (1.0 + ALPHA_CU * (temp_c - 20.0))


def barrel_resistance(drill: float, plating: float, length: float, rho: float) -> float:
    """Resistance of a plated barrel: a thin tube of wall ``plating`` around a hole of diameter ``drill`` (mm)."""
    area = math.pi * plating * (drill + plating)
    return rho * length / area


Ring = list[tuple[float, float]]


@dataclass
class Barrel:
    id: str
    x: float
    y: float
    radius: float                 # pad radius: the cells under it share the barrel
    layers: list[int]             # layer indexes joined, top to bottom
    seg_ohm: list[float]          # resistance between layers[i] and layers[i+1]


@dataclass
class Terminal:
    name: str
    role: str                     # 'source' | 'load' | 'probe'
    rings: dict[int, list[Ring]]  # layer index -> pad outlines
    amps: float = 0.0


@dataclass
class Problem:
    layers: list[str]
    sheet_ohm: list[float]                        # per layer, ohm per square
    polys: dict[int, list[Ring]]                  # copper per layer
    terminals: list[Terminal]
    barrels: list[Barrel] = field(default_factory=list)


@dataclass
class Solution:
    h: float
    x0: float
    y0: float
    nx: int
    ny: int
    ncells: int
    u: np.ndarray                 # [L, ny, nx] drop in volts at the given currents, NaN where no copper or not connected
    k: np.ndarray                 # [L, ny, nx] sheet current density A/mm, NaN where nothing
    cover: np.ndarray             # [L, ny, nx]
    term_u: dict[str, float]      # mean drop at each terminal at the given currents (NaN when not connected)
    term_connected: dict[str, bool]
    z: np.ndarray                 # [n_terminals, n_loads] drop (V) at terminal i per amp drawn at load j
    loads: list[str]
    terms: list[str]
    source_amps: dict[str, float]  # current delivered by each source terminal
    barrel_amps: dict[str, float]  # largest current through each barrel
    barrel_ohm: dict[str, float]
    residual: float
    solve_s: float
    raster_s: float
    cells_per_layer: list[int]
    unsolved_cells: int = 0


# ------------------------------------------------------------------ rasterisation

def capsule_ring(p0: tuple[float, float], p1: tuple[float, float], width: float, steps: int = 12) -> Ring:
    """Outline of a segment of the given width with round ends."""
    (x0, y0), (x1, y1) = p0, p1
    r = width / 2.0
    ang = math.atan2(y1 - y0, x1 - x0) if (x1 != x0 or y1 != y0) else 0.0
    pts: Ring = []
    for i in range(steps + 1):          # cap at p1, from the left side round to the right side
        a = ang - math.pi / 2 + math.pi * i / steps
        pts.append((x1 + r * math.cos(a), y1 + r * math.sin(a)))
    for i in range(steps + 1):          # cap at p0
        a = ang + math.pi / 2 + math.pi * i / steps
        pts.append((x0 + r * math.cos(a), y0 + r * math.sin(a)))
    return pts


def circle_ring(cx: float, cy: float, r: float, steps: int = 24) -> Ring:
    return [(cx + r * math.cos(2 * math.pi * i / steps), cy + r * math.sin(2 * math.pi * i / steps)) for i in range(steps)]


def _inset(ring: Ring, d: float) -> Ring | None:
    """The ring pulled in by ``d``. Pillow fills the boundary pixels too, which would make every shape one subpixel too wide."""
    try:
        from shapely.geometry import Polygon

        poly = Polygon(ring)
        if not poly.is_valid:
            poly = poly.buffer(0)
        out = poly.buffer(-d, join_style=2, mitre_limit=3.0)
    except Exception:  # noqa: BLE001  (a degenerate ring is simply not drawn)
        return None
    if out.is_empty:
        return None
    if out.geom_type != "Polygon":
        return None if not hasattr(out, "geoms") else max(out.geoms, key=lambda g: g.area).exterior.coords[:-1] and list(max(out.geoms, key=lambda g: g.area).exterior.coords)[:-1]
    return list(out.exterior.coords)[:-1]


def _draw(rings: list[Ring], x0: float, y0: float, hs: float, size: tuple[int, int]) -> Image.Image:
    img = Image.new("L", size, 0)
    d = ImageDraw.Draw(img)
    for ring in rings:
        if len(ring) < 3:
            continue
        inner = _inset(ring, 0.5 * hs)
        if not inner or len(inner) < 3:
            continue
        d.polygon([((x - x0) / hs - 0.5, (y - y0) / hs - 0.5) for x, y in inner], fill=1)
    return img


def _block_cover(img: Image.Image, ss: int) -> np.ndarray:
    a = np.asarray(img, dtype=np.uint8)
    ny, nx = a.shape[0] // ss, a.shape[1] // ss
    return (a.reshape(ny, ss, nx, ss).sum(axis=(1, 3), dtype=np.uint32) / float(ss * ss)).astype(np.float32)


def _bbox(prob: Problem) -> tuple[float, float, float, float]:
    xs: list[float] = []
    ys: list[float] = []
    for rings in prob.polys.values():
        for r in rings:
            for x, y in r:
                xs.append(x)
                ys.append(y)
    for t in prob.terminals:
        for rings in t.rings.values():
            for r in rings:
                for x, y in r:
                    xs.append(x)
                    ys.append(y)
    for b in prob.barrels:
        xs += [b.x - b.radius, b.x + b.radius]
        ys += [b.y - b.radius, b.y + b.radius]
    if not xs:
        raise ValueError("no copper in the problem")
    return min(xs), min(ys), max(xs), max(ys)


def choose_supersample(h: float, min_feature: float, nx: int, ny: int, n_layers: int) -> int:
    """Subpixels per cell edge: fine enough that the narrowest feature is resolved to about 1/64, within a memory cap."""
    ss = max(4, min(20, math.ceil(h / max(min_feature / 64.0, 1e-4))))
    while ss > 2 and (nx * ss) * (ny * ss) > MAX_RASTER_PIXELS:
        ss -= 1
    return ss


def solve(prob: Problem, h: float, ss: int | None = None, min_feature: float | None = None, diag: float = DIAG_WEIGHT) -> Solution:
    """Rasterise at pitch ``h`` mm and solve. ``ss`` is the supersampling factor (chosen from ``min_feature`` when omitted)."""
    t0 = time.perf_counter()
    nl = len(prob.layers)
    xmin, ymin, xmax, ymax = _bbox(prob)
    x0, y0 = xmin - 2 * h, ymin - 2 * h
    nx = int(math.ceil((xmax - x0) / h)) + 3
    ny = int(math.ceil((ymax - y0) / h)) + 3
    if ss is None:
        ss = choose_supersample(h, min_feature if min_feature else h * 4, nx, ny, nl)
    hs = h / ss

    cover = np.zeros((nl, ny, nx), dtype=np.float32)
    for l in range(nl):
        rings = prob.polys.get(l)
        if rings:
            cover[l] = _block_cover(_draw(rings, x0, y0, hs, (nx * ss, ny * ss)), ss)
    active = cover >= MIN_COVER
    n = int(active.sum())
    raster_s = time.perf_counter() - t0
    cells_per_layer = [int(active[l].sum()) for l in range(nl)]
    idx = np.full((nl, ny, nx), -1, dtype=np.int64)
    idx[active] = np.arange(n)

    rs = np.asarray(prob.sheet_ohm, dtype=np.float64).reshape(nl, 1, 1)
    sg = 1.0 / rs                                   # sheet conductance, siemens per square
    rows: list[np.ndarray] = []
    cols: list[np.ndarray] = []
    gs: list[np.ndarray] = []
    for axis in (2, 1):
        sl_a = (slice(None), slice(None), slice(None, -1)) if axis == 2 else (slice(None), slice(None, -1), slice(None))
        sl_b = (slice(None), slice(None), slice(1, None)) if axis == 2 else (slice(None), slice(1, None), slice(None))
        both = active[sl_a] & active[sl_b]
        fa, fb = cover[sl_a].astype(np.float64), cover[sl_b].astype(np.float64)
        with np.errstate(invalid="ignore", divide="ignore"):
            fab = np.where(both, MEANFN(fa, fb), 0.0)
        g = (fab * sg * (1.0 - 2.0 * diag))[both]
        rows.append(idx[sl_a][both])
        cols.append(idx[sl_b][both])
        gs.append(g)
    if diag > 0:
        for dj, di in ((1, 1), (1, -1)):
            sa = (slice(None), slice(0, ny - 1), slice(0, nx - 1) if di == 1 else slice(1, nx))
            sb = (slice(None), slice(1, ny), slice(1, nx) if di == 1 else slice(0, nx - 1))
            both = active[sa] & active[sb]
            fa, fb = cover[sa].astype(np.float64), cover[sb].astype(np.float64)
            with np.errstate(invalid="ignore", divide="ignore"):
                fab = np.where(both, MEANFN(fa, fb), 0.0)
            rows.append(idx[sa][both])
            cols.append(idx[sb][both])
            gs.append((fab * sg * diag)[both])

    cell_x = lambda i: x0 + (i + 0.5) * h           # noqa: E731
    cell_y = lambda j: y0 + (j + 0.5) * h           # noqa: E731

    # barrels
    barrel_edges: list[tuple[int, int, float, int, int]] = []   # (node a, node b, g, barrel index, segment)
    barrel_ohm: dict[str, float] = {}
    for bi, b in enumerate(prob.barrels):
        r_sel = max(b.radius, 0.5 * h)
        i_lo, i_hi = max(int((b.x - r_sel - x0) / h) - 1, 0), min(int((b.x + r_sel - x0) / h) + 2, nx)
        j_lo, j_hi = max(int((b.y - r_sel - y0) / h) - 1, 0), min(int((b.y + r_sel - y0) / h) + 2, ny)
        cells: list[tuple[int, int]] = []
        top, bot = b.layers[0], b.layers[-1]
        for j in range(j_lo, j_hi):
            for i in range(i_lo, i_hi):
                if math.hypot(cell_x(i) - b.x, cell_y(j) - b.y) <= r_sel and cover[top, j, i] >= BARREL_COVER and cover[bot, j, i] >= BARREL_COVER:
                    cells.append((j, i))
        if not cells:
            ci, cj = int((b.x - x0) / h), int((b.y - y0) / h)
            if 0 <= ci < nx and 0 <= cj < ny and active[top, cj, ci] and active[bot, cj, ci]:
                cells = [(cj, ci)]
        barrel_ohm[b.id] = sum(b.seg_ohm)
        if not cells:
            continue
        for j, i in cells:
            avail = [(k, l) for k, l in enumerate(b.layers) if cover[l, j, i] >= BARREL_COVER]
            for (k1, l1), (k2, l2) in zip(avail[:-1], avail[1:]):
                r = sum(b.seg_ohm[k1:k2])
                barrel_edges.append((int(idx[l1, j, i]), int(idx[l2, j, i]), 1.0 / (r * len(cells)), bi, k1))
    if barrel_edges:
        rows.append(np.fromiter((e[0] for e in barrel_edges), dtype=np.int64, count=len(barrel_edges)))
        cols.append(np.fromiter((e[1] for e in barrel_edges), dtype=np.int64, count=len(barrel_edges)))
        gs.append(np.fromiter((e[2] for e in barrel_edges), dtype=np.float64, count=len(barrel_edges)))
    r_all = np.concatenate(rows) if rows else np.zeros(0, dtype=np.int64)
    c_all = np.concatenate(cols) if cols else np.zeros(0, dtype=np.int64)
    g_all = np.concatenate(gs) if gs else np.zeros(0)

    # terminals
    term_nodes: dict[str, dict[int, float]] = {}      # name -> {node: weight}
    dirichlet: set[int] = set()
    term_dirichlet: dict[str, list[int]] = {}
    for t in prob.terminals:
        nodes: dict[int, float] = {}
        for l, rings in t.rings.items():
            xs = [p[0] for r in rings for p in r]
            ys = [p[1] for r in rings for p in r]
            ia, ib = max(int((min(xs) - x0) / h) - 1, 0), min(int((max(xs) - x0) / h) + 2, nx)
            ja, jb = max(int((min(ys) - y0) / h) - 1, 0), min(int((max(ys) - y0) / h) + 2, ny)
            if ib <= ia or jb <= ja:
                continue
            img = _draw(rings, x0 + ia * h, y0 + ja * h, hs, ((ib - ia) * ss, (jb - ja) * ss))
            fp = _block_cover(img, ss)
            win = idx[l, ja:jb, ia:ib]
            ok = (win >= 0) & (fp >= 0.02)
            for jj, ii in zip(*np.nonzero(ok)):
                nodes[int(win[jj, ii])] = nodes.get(int(win[jj, ii]), 0.0) + float(fp[jj, ii])
        term_nodes[t.name] = nodes
        if t.role == "source" and nodes:
            big = [k for k, w in nodes.items() if w >= 0.5]
            chosen = [k for k in (big or [max(nodes, key=nodes.get)]) if k not in dirichlet]
            dirichlet.update(chosen)
            term_dirichlet[t.name] = chosen

    # connectivity: components that touch a source are solved, the rest are reported
    if n == 0:
        raise ValueError("no copper cells at this pitch")
    adj = sparse.coo_matrix((np.ones(len(r_all)), (r_all, c_all)), shape=(n, n))
    n_comp, label = csgraph.connected_components(adj, directed=False)
    live_comp = np.zeros(n_comp, dtype=bool)
    if dirichlet:
        live_comp[np.unique(label[list(dirichlet)])] = True
    live = live_comp[label]
    free_mask = live.copy()
    if dirichlet:
        free_mask[list(dirichlet)] = False
    free_ids = np.nonzero(free_mask)[0]
    fpos = np.full(n, -1, dtype=np.int64)
    fpos[free_ids] = np.arange(len(free_ids))
    nf = len(free_ids)

    loads = [t for t in prob.terminals if t.role == "load"]
    terms = [t for t in prob.terminals if t.role != "source"]
    load_names = [t.name for t in loads]
    term_names = [t.name for t in terms]

    # weights per terminal over the free nodes
    def weights(name: str) -> tuple[np.ndarray, np.ndarray]:
        nodes = term_nodes.get(name, {})
        ids = np.fromiter((k for k in nodes if fpos[k] >= 0), dtype=np.int64)
        w = np.fromiter((nodes[k] for k in nodes if fpos[k] >= 0), dtype=np.float64)
        if w.sum() > 0:
            w = w / w.sum()
        return fpos[ids] if len(ids) else ids, w

    term_connected: dict[str, bool] = {}
    for t in prob.terminals:
        nodes = term_nodes.get(t.name, {})
        term_connected[t.name] = bool(nodes) and (t.role == "source" or any(live[k] for k in nodes))

    t1 = time.perf_counter()
    u_free = np.zeros(nf)
    z = np.full((len(terms), len(loads)), np.nan)
    residual = 0.0
    if nf and len(g_all):
        # Laplacian restricted to the free nodes
        deg = np.bincount(r_all, weights=g_all, minlength=n) + np.bincount(c_all, weights=g_all, minlength=n)
        fa_, fb_ = fpos[r_all], fpos[c_all]
        m = (fa_ >= 0) & (fb_ >= 0)
        off = sparse.coo_matrix((-g_all[m], (fa_[m], fb_[m])), shape=(nf, nf))
        lap = (off + off.T + sparse.diags(deg[free_ids])).tocsc()
        lu = splu(lap)
        wt = [weights(t.name) for t in terms]
        wl = [weights(t.name) for t in loads]
        bmat = np.zeros((nf, len(loads)))
        for k, (ids, w) in enumerate(wl):
            if len(ids):
                bmat[ids, k] = w
        umat = lu.solve(bmat) if len(loads) else np.zeros((nf, 0))
        amps = np.asarray([t.amps for t in loads], dtype=np.float64)
        rhs = bmat @ amps if len(loads) else np.zeros(nf)
        u_free = umat @ amps if len(loads) else np.zeros(nf)
        res_norm = np.linalg.norm(rhs)
        if res_norm > 0:
            residual = float(np.linalg.norm(lap @ u_free - rhs) / res_norm)
        else:
            u_free = np.zeros(nf)
        for i, (ids, w) in enumerate(wt):
            if len(ids) and len(loads):
                z[i, :] = w @ umat[ids, :]
            elif term_connected.get(terms[i].name):
                z[i, :] = 0.0
        # a load with no copper connection to a source has no drop to speak of
        for k, t in enumerate(loads):
            if not term_connected[t.name]:
                z[:, k] = np.nan
    solve_s = time.perf_counter() - t1

    # full-node drop
    u_node = np.full(n, np.nan)
    u_node[free_ids] = u_free
    if dirichlet:
        u_node[list(dirichlet)] = 0.0
    u = np.full((nl, ny, nx), np.nan, dtype=np.float32)
    u[active] = u_node
    unsolved = int((~live).sum())

    term_u: dict[str, float] = {}
    for t in prob.terminals:
        nodes = term_nodes.get(t.name, {})
        pairs = [(w, u_node[k]) for k, w in nodes.items() if not math.isnan(u_node[k])]
        tot = sum(w for w, _ in pairs)
        term_u[t.name] = sum(w * v for w, v in pairs) / tot if tot > 0 else float("nan")

    # currents out of sources and through barrels
    source_amps: dict[str, float] = {}
    barrel_amps: dict[str, float] = {}
    if len(g_all):
        flow = g_all * (u_node[c_all] - u_node[r_all])      # current from node r to node c: V = Vs - u, so I = g (V_r - V_c)
        flow = np.where(np.isnan(flow), 0.0, flow)
        out = np.bincount(r_all, weights=flow, minlength=n) - np.bincount(c_all, weights=flow, minlength=n)
        for name, dn in term_dirichlet.items():
            source_amps[name] = float(sum(out[k] for k in dn))
    per: dict[tuple[int, int], float] = {}
    for (a, c, g, bi, k1) in barrel_edges:
        f = g * (u_node[c] - u_node[a])
        if not math.isnan(f):
            per[(bi, k1)] = per.get((bi, k1), 0.0) + abs(f)
    for (bi, _), v in per.items():
        bid = prob.barrels[bi].id
        barrel_amps[bid] = max(barrel_amps.get(bid, 0.0), v)

    # sheet current density
    k_field = np.full((nl, ny, nx), np.nan, dtype=np.float32)
    uu = np.where(active, np.nan_to_num(u, nan=0.0), 0.0).astype(np.float64)
    live_cells = np.zeros((nl, ny, nx), dtype=bool)
    live_cells[active] = live
    gradx = np.zeros((nl, ny, nx))
    grady = np.zeros((nl, ny, nx))
    cnt_x = np.zeros((nl, ny, nx))
    cnt_y = np.zeros((nl, ny, nx))
    both = live_cells[:, :, :-1] & live_cells[:, :, 1:]
    d = np.where(both, (uu[:, :, :-1] - uu[:, :, 1:]) / h, 0.0)
    gradx[:, :, :-1] += d
    gradx[:, :, 1:] += d
    cnt_x[:, :, :-1] += both
    cnt_x[:, :, 1:] += both
    both = live_cells[:, :-1, :] & live_cells[:, 1:, :]
    d = np.where(both, (uu[:, :-1, :] - uu[:, 1:, :]) / h, 0.0)
    grady[:, :-1, :] += d
    grady[:, 1:, :] += d
    cnt_y[:, :-1, :] += both
    cnt_y[:, 1:, :] += both
    with np.errstate(invalid="ignore", divide="ignore"):
        gx = np.where(cnt_x > 0, gradx / cnt_x, 0.0)
        gy = np.where(cnt_y > 0, grady / cnt_y, 0.0)
    kk = np.hypot(gx, gy) * sg
    k_field[live_cells] = kk[live_cells].astype(np.float32)

    return Solution(
        h=h, x0=x0, y0=y0, nx=nx, ny=ny, ncells=n, u=u, k=k_field, cover=cover, term_u=term_u,
        term_connected=term_connected, z=z, loads=load_names, terms=term_names, source_amps=source_amps,
        barrel_amps=barrel_amps, barrel_ohm=barrel_ohm, residual=residual, solve_s=solve_s,
        raster_s=raster_s, cells_per_layer=cells_per_layer, unsolved_cells=unsolved,
    )
