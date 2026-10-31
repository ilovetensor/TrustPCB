"""Steady-state board temperature.

The board is a stack of in-plane conduction layers, one per copper layer. Each layer is a raster
over the board outline. Between two neighbouring cells of a layer the conductance is the copper
that really crosses their shared face (tracks, zone fills and pads, rendered at 4x resolution)
plus the share of FR-4 around that layer. Neighbouring copper layers are coupled through the
dielectric between them and, in parallel, through plated via and plated-hole barrels. The top
face of the first layer and the bottom face of the last layer lose heat to ambient air by natural
convection (a stated ``h``) and by radiation (``eps * sigma * (T^4 - Ta^4)``, linearised and
iterated). Heat comes from sources the caller states: part power, an LDO's drop times its load,
copper loss from a stated net current, or labelled defaults. With no known source the run says so.

Everything here is a model of a bare board in still air: no enclosure, no airflow, no component
bodies shading or finning the board. Results are estimates and every input is listed with its
source. The closed-form benchmarks in ``tests/sims/test_thermal.py`` guard the solver.
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import scipy.sparse as sp
from PIL import Image, ImageDraw
from scipy.ndimage import maximum_filter
from scipy.sparse.linalg import LinearOperator, cg, splu
from shapely.geometry import LineString, Polygon

from ..pcb.model import Board
from .base import Assumption, Overlay, SimResult, Simulator

SIGMA = 5.670374419e-8  # W/m2K4
K_CU = 390.0  # W/mK
RHO_CU_20 = 1.72e-8  # ohm m
ALPHA_CU = 0.00393  # 1/K
SS = 4  # raster supersampling per cell side

DEFAULTS: dict[str, tuple[float, str, str, str]] = {
    # name: (value, unit, source, cite)
    "ambient_c": (25.0, "C", "default", "still room air; set the real ambient inside the enclosure"),
    "h_conv": (
        6.0,
        "W/m2K",
        "default",
        "natural convection on a small board in still air: vertical plate h = 1.42 (dT/L)^0.25 is about 5.8 W/m2K at dT 20 K and L 70 mm; radiation is added separately",
    ),
    "emissivity": (0.9, "-", "default", "solder mask and FR-4 are about 0.9 in the infrared"),
    "k_fr4_inplane": (0.8, "W/mK", "default", "woven glass FR-4 in the plane of the weave, typically 0.8 to 1.0"),
    "k_fr4_through": (0.3, "W/mK", "default", "FR-4 through the thickness, typically 0.25 to 0.35"),
    "via_plating_um": (25.0, "um", "default", "about 1 mil of copper plating in a plated hole"),
    "default_ldo_load_a": (0.1, "A", "default", "placeholder load for a regulator when no current is supplied; replace with a datasheet, power-simulation or user figure"),
}

MAX_RISE_K = 200.0  # above this the linear-surface model and FR-4 are not credible
WARN_RISE_K = 100.0

LDO_PATTERN = re.compile(
    r"(AP2112|AP2210|AP7\d{3}|AMS1117|LD1117|LM1117|NCP11\d{2}|LM317|MCP170\d|MCP180\d|XC62\d{2}|XC63\d{2}|RT9013|RT9193|TLV7\d{2}|TLV7\d{3}|TPS7\d{3}|TPS7A\d+|LP5907|LP2985|LP3985|ME6211|HT7\d{3}|MIC5\d{3}|MIC29\d{3}|SPX\d{4}|L78\d{2}|78L\d{2}|LDO|LT1\d{3}|ADP1\d{3}|ADP7\d{3}|LM1086|NCV\d+)",
    re.IGNORECASE,
)
SKIP_PART = re.compile(r"^(H\d*|FID\d*|MH\d*|TP\d*|LOGO\d*|G\*+|REF\*+)$", re.IGNORECASE)


# ------------------------------------------------------------------------- core solver


@dataclass
class Model:
    """A layered raster conduction model. All conductances are W/K and belong to one cell face."""

    ny: int
    nx: int
    d_mm: float
    area: np.ndarray  # (ny, nx) fraction of the cell inside the board, 0 outside
    gx: list[np.ndarray]  # per layer (ny, nx-1)
    gy: list[np.ndarray]  # per layer (ny-1, nx)
    gz: list[np.ndarray]  # per gap (ny, nx)
    x0: float = 0.0
    y0: float = 0.0
    _lap: sp.csr_matrix | None = field(default=None, repr=False)
    _idx: np.ndarray | None = field(default=None, repr=False)

    @property
    def nl(self) -> int:
        return len(self.gx)

    @property
    def cell_m2(self) -> float:
        return (self.d_mm * 1e-3) ** 2

    def index(self) -> np.ndarray:
        if self._idx is None:
            idx = -np.ones((self.nl, self.ny, self.nx), dtype=np.int64)
            inside = self.area > 0
            n = int(inside.sum())
            for layer in range(self.nl):
                idx[layer][inside] = np.arange(layer * n, (layer + 1) * n)
            self._idx = idx
        return self._idx

    def laplacian(self) -> sp.csr_matrix:
        if self._lap is not None:
            return self._lap
        idx = self.index()
        inside = self.area > 0
        n = int(inside.sum()) * self.nl
        rows: list[np.ndarray] = []
        cols: list[np.ndarray] = []
        vals: list[np.ndarray] = []

        def edges(a: np.ndarray, b: np.ndarray, g: np.ndarray, valid: np.ndarray) -> None:
            ok = valid & (g > 0)
            rows.append(a[ok])
            cols.append(b[ok])
            vals.append(g[ok])

        for layer in range(self.nl):
            ix = idx[layer]
            both = inside[:, :-1] & inside[:, 1:]
            edges(ix[:, :-1], ix[:, 1:], self.gx[layer], both)
            both = inside[:-1, :] & inside[1:, :]
            edges(ix[:-1, :], ix[1:, :], self.gy[layer], both)
        for layer in range(self.nl - 1):
            edges(idx[layer], idx[layer + 1], self.gz[layer], inside)
        r = np.concatenate(rows) if rows else np.zeros(0, dtype=np.int64)
        c = np.concatenate(cols) if cols else np.zeros(0, dtype=np.int64)
        v = np.concatenate(vals) if vals else np.zeros(0)
        w = sp.coo_matrix((np.concatenate([v, v]), (np.concatenate([r, c]), np.concatenate([c, r]))), shape=(n, n)).tocsr()
        deg = np.asarray(w.sum(axis=1)).ravel()
        self._lap = (sp.diags(deg) - w).tocsr()
        return self._lap


@dataclass
class Solution:
    temp_c: np.ndarray  # (nl, ny, nx), NaN outside the board
    iterations: int
    converged: bool
    residual: float
    q_in_w: float
    q_conv_w: float
    q_rad_w: float
    solve_s: float
    q_w: np.ndarray | None = None  # final source field, W per cell

    @property
    def q_out_w(self) -> float:
        return self.q_conv_w + self.q_rad_w

    @property
    def balance_error(self) -> float:
        return abs(self.q_in_w - self.q_out_w) / self.q_in_w if self.q_in_w > 0 else 0.0


def _factor(m: sp.spmatrix):
    """LU of a symmetric positive definite matrix, ordered for symmetry (about 1.5x faster than the default)."""
    return splu(m.tocsc(), permc_spec="MMD_AT_PLUS_A", diag_pivot_thresh=0.0, options={"SymmetricMode": True})


def solve_model(
    model: Model,
    q_fixed: np.ndarray,
    q_joule: np.ndarray | None = None,
    ambient_c: float = 25.0,
    h_top: float = 6.0,
    h_bot: float = 6.0,
    eps_top: float = 0.9,
    eps_bot: float = 0.9,
    tol_k: float = 1e-4,
    max_iter: int = 40,
) -> Solution:
    """Solve the layered model. ``q_fixed`` and ``q_joule`` are W per cell, shape (nl, ny, nx).

    ``q_joule`` is copper loss stated at 20 C; it scales with ``1 + alpha (T - 20)`` of its own
    node in every iteration, and so does the radiation term, so both are iterated together.
    """
    t0 = time.perf_counter()
    nl, ny, nx = model.nl, model.ny, model.nx
    inside = model.area > 0
    n_layer = int(inside.sum())
    n = n_layer * nl
    lap = model.laplacian()
    a_cell = model.cell_m2
    area_in = model.area[inside]
    qf = q_fixed[:, inside].reshape(n)
    qj = q_joule[:, inside].reshape(n) if q_joule is not None else np.zeros(n)

    h = np.zeros((nl, n_layer))
    eps = np.zeros((nl, n_layer))
    h[0] += h_top  # a one-layer board has both faces on the same node
    eps[0] += eps_top
    h[nl - 1] += h_bot
    eps[nl - 1] += eps_bot
    face_area = np.tile(area_in * a_cell, nl)  # m2 of one face per node
    g_conv = h.reshape(n) * face_area
    eps_flat = eps.reshape(n)
    ta_k = ambient_c + 273.15

    theta = np.zeros(n)
    lu = None
    iterations = 0
    converged = False
    residual = 0.0
    for iterations in range(1, max_iter + 1):
        t_k = ta_k + theta
        g_rad = eps_flat * SIGMA * face_area * (t_k**2 + ta_k**2) * (t_k + ta_k)
        gam = g_conv + g_rad
        b = qf + qj * (1.0 + ALPHA_CU * (theta + ambient_c - 20.0))
        m = (lap + sp.diags(gam)).tocsr()
        if lu is None:
            lu = _factor(m)
            new = lu.solve(b)
        else:
            pre = LinearOperator(m.shape, matvec=lu.solve)
            new, info = cg(m, b, x0=theta, rtol=1e-12, atol=0.0, M=pre, maxiter=60)
            if info != 0:
                lu = _factor(m)
                new = lu.solve(b)
        residual = float(np.linalg.norm(m @ new - b) / max(np.linalg.norm(b), 1e-300))
        change = float(np.max(np.abs(new - theta))) if n else 0.0
        theta = new
        if change < tol_k:
            converged = True
            break
    t_k = ta_k + theta
    t_c = theta + ambient_c
    q_final = qf + qj * (1.0 + ALPHA_CU * (t_c - 20.0))
    q_conv = float(np.sum(g_conv * theta))
    q_rad = float(np.sum(eps_flat * SIGMA * face_area * (t_k**4 - ta_k**4)))
    full = np.full((nl, ny, nx), np.nan)
    full[:, inside] = t_c.reshape(nl, n_layer)
    qfull = np.zeros((nl, ny, nx))
    qfull[:, inside] = q_final.reshape(nl, n_layer)
    return Solution(full, iterations, converged, residual, float(q_final.sum()), q_conv, q_rad, time.perf_counter() - t0, qfull)


# ------------------------------------------------------------------------- stackup and raster


@dataclass
class Grid:
    x0: float
    y0: float
    d: float
    nx: int
    ny: int

    def cell(self, x: float, y: float) -> tuple[int, int]:
        return int(math.floor((y - self.y0) / self.d)), int(math.floor((x - self.x0) / self.d))


@dataclass
class Stack:
    names: list[str]
    cu_mm: list[float]
    gap_mm: list[float]
    source: str


def board_stack(board: Board) -> Stack:
    copper = board.copper_layers() or ["F.Cu", "B.Cu"]
    layers = (board.stackup or {}).get("layers", [])
    cu: dict[str, float] = {}
    gaps: list[float] = []
    names: list[str] = []
    acc = 0.0
    seen = False
    ok = True
    for item in layers:
        kind = item.get("kind")
        if kind == "copper":
            if seen:
                gaps.append(acc)
            acc = 0.0
            seen = True
            names.append(item["name"])
            cu[item["name"]] = item.get("thickness_mm") or 0.035
        elif kind == "dielectric" and seen:
            t = item.get("thickness_mm")
            if t is None:
                ok = False
            acc += t or 0.0
    if not ok or sorted(names) != sorted(copper) or len(gaps) != len(names) - 1 or any(g <= 0 for g in gaps):
        total = float(board.thickness or 1.6)
        n = len(copper)
        t_cu = [0.035 if i in (0, n - 1) else 0.0175 for i in range(n)]
        gap = max((total - sum(t_cu)) / max(n - 1, 1), 0.05)
        return Stack(list(copper), t_cu, [gap] * (n - 1), "default")
    src = "board" if not (board.stackup or {}).get("assumed", False) else "default"
    return Stack(names, [cu[x] for x in names], gaps, src)


def _outline_rings(board: Board) -> list[list[list[float]]]:
    rings = [r for r in (board.outline or []) if len(r) >= 3]
    return rings


def make_grid(board: Board, pitch: float) -> Grid:
    rings = _outline_rings(board)
    if rings:
        xs = [p[0] for r in rings for p in r]
        ys = [p[1] for r in rings for p in r]
        x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
    else:
        x0, y0, x1, y1 = board.bbox
    nx = max(int(math.ceil((x1 - x0) / pitch - 1e-9)), 2)
    ny = max(int(math.ceil((y1 - y0) / pitch - 1e-9)), 2)
    return Grid(x0, y0, pitch, nx, ny)


def default_pitch(board: Board, layers: int = 2, cells: int = 60000) -> float:
    rings = _outline_rings(board)
    if rings:
        xs = [p[0] for r in rings for p in r]
        ys = [p[1] for r in rings for p in r]
        w, h = max(xs) - min(xs), max(ys) - min(ys)
    else:
        w, h = board.bbox[2] - board.bbox[0], board.bbox[3] - board.bbox[1]
    per_layer = min(cells, 120000 // max(layers, 1))
    return max(0.1, round(math.sqrt(max(w * h, 1.0) / per_layer), 3))


class _Canvas:
    def __init__(self, grid: Grid):
        self.grid = grid
        self.img = Image.new("L", (grid.nx * SS, grid.ny * SS), 0)
        self.draw = ImageDraw.Draw(self.img)

    def px(self, pts) -> list[tuple[float, float]]:
        g = self.grid
        k = SS / g.d
        return [((x - g.x0) * k - 0.5, (y - g.y0) * k - 0.5) for x, y in pts]

    def poly(self, pts, value: int = 255) -> None:
        if len(pts) >= 3:
            self.draw.polygon(self.px(pts), fill=value)

    def circle(self, x: float, y: float, r: float, value: int = 255) -> None:
        g = self.grid
        k = SS / g.d
        cx, cy = (x - g.x0) * k - 0.5, (y - g.y0) * k - 0.5
        self.draw.ellipse([cx - r * k, cy - r * k, cx + r * k, cy + r * k], fill=value)

    def array(self) -> np.ndarray:
        return np.asarray(self.img, dtype=np.float32) / 255.0


def _stadium(points, width: float):
    if len(points) < 2:
        return None
    try:
        poly = LineString(points).buffer(width / 2.0, cap_style="round", quad_segs=4)
    except Exception:
        return None
    if poly.is_empty:
        return None
    if poly.geom_type == "MultiPolygon":
        return list(poly.geoms)
    return [poly]


def board_area_fraction(board: Board, grid: Grid) -> np.ndarray:
    """Fraction of every cell that is board: inside the outline and outside unplated holes."""
    canvas = _Canvas(grid)
    rings = _outline_rings(board)
    if not rings:
        canvas.poly([(grid.x0, grid.y0), (grid.x0 + grid.nx * grid.d, grid.y0), (grid.x0 + grid.nx * grid.d, grid.y0 + grid.ny * grid.d), (grid.x0, grid.y0 + grid.ny * grid.d)])
    else:
        polys = [Polygon(r) for r in rings]
        depth = []
        for i, p in enumerate(polys):
            if not p.is_valid:
                p = p.buffer(0)
                polys[i] = p
            depth.append(sum(1 for j, q in enumerate(polys) if j != i and q.area > p.area and q.contains(p.representative_point())))
        order = sorted(range(len(rings)), key=lambda i: depth[i])
        for i in order:
            canvas.poly(rings[i], 255 if depth[i] % 2 == 0 else 0)
    for pad in board.pads:
        if pad.type == "np_thru_hole" and pad.drill:
            canvas.circle(pad.x, pad.y, pad.drill / 2.0, 0)
    a = canvas.array().reshape(grid.ny, SS, grid.nx, SS).mean(axis=(1, 3))
    a[a < 0.25] = 0.0
    return a.astype(np.float64)


def copper_masks(board: Board, grid: Grid, stack: Stack, extra_pours: list[dict] | None = None, no_copper: bool = False) -> list[np.ndarray]:
    """One supersampled 0/1 raster per copper layer, from tracks, zone fills, pads, vias and added pours."""
    canvases = {name: _Canvas(grid) for name in stack.names}
    if no_copper:
        return [c.array() for c in canvases.values()]
    for z in board.zones:
        if z.is_keepout:
            continue
        for layer, rings in z.filled.items():
            c = canvases.get(layer)
            if c is not None:
                for ring in rings:
                    c.poly(ring)
    for t in board.tracks:
        c = canvases.get(t.layer)
        if c is None:
            continue
        for p in _stadium(t.points(), t.width) or []:
            c.poly(list(p.exterior.coords))
    for pad in board.pads:
        if pad.type == "np_thru_hole":
            continue
        for layer in pad.layers:
            c = canvases.get(layer)
            if c is not None and pad.ring:
                c.poly(pad.ring)
    for v in board.vias:
        for layer in _via_span(stack, v.layers):
            canvases[layer].circle(v.x, v.y, v.diameter / 2.0)
    for pour in extra_pours or []:
        c = canvases.get(pour["layer"])
        if c is not None:
            c.poly(pour["polygon"])
    return [canvases[n].array() for n in stack.names]


def _via_span(stack: Stack, layers: list[str]) -> list[str]:
    idx = [stack.names.index(x) for x in layers if x in stack.names]
    if not idx:
        return []
    return stack.names[min(idx) : max(idx) + 1]


def _face_cover(mask: np.ndarray, grid: Grid) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Copper fraction of each cell and on each shared face, from a supersampled raster."""
    ny, nx = grid.ny, grid.nx
    cell = mask.reshape(ny, SS, nx, SS).mean(axis=(1, 3))
    rows = mask.reshape(ny, SS, nx * SS).mean(axis=1)  # (ny, nx*SS)
    b = np.arange(1, nx) * SS
    cov_x = 0.5 * (rows[:, b - 1] + rows[:, b])
    cols = mask.reshape(ny * SS, nx, SS).mean(axis=2)  # (ny*SS, nx)
    b = np.arange(1, ny) * SS
    cov_y = 0.5 * (cols[b - 1, :] + cols[b, :])
    return cell, cov_x, cov_y


@dataclass
class Params:
    ambient_c: float = 25.0
    h_conv: float = 6.0
    emissivity: float = 0.9
    k_in: float = 0.8
    k_z: float = 0.3
    plating_um: float = 25.0


def via_sites(board: Board, extra_vias: list[dict], stack: Stack) -> list[dict]:
    """Every plated barrel: vias, plated holes and what-if vias, as dicts with position, drill and layer span."""
    sites = []
    for v in board.vias:
        span = _via_span(stack, v.layers)
        if len(span) > 1:
            sites.append({"x": v.x, "y": v.y, "drill": v.drill, "span": span, "kind": "via", "net": v.net})
    for p in board.pads:
        if p.type == "thru_hole" and p.drill and p.drill > 0:
            span = [x for x in stack.names if x in p.layers]
            if len(span) > 1:
                span = stack.names[stack.names.index(span[0]) : stack.names.index(span[-1]) + 1]
                sites.append({"x": p.x, "y": p.y, "drill": float(p.drill), "span": span, "kind": "hole", "net": p.net})
    for v in extra_vias:
        sites.append(dict(v, kind="whatif"))
    return sites


def build_model(board: Board, grid: Grid, stack: Stack, params: Params, sites: list[dict], pours: list[dict], no_copper: bool = False) -> tuple[Model, list[np.ndarray]]:
    area = board_area_fraction(board, grid)
    masks = copper_masks(board, grid, stack, pours, no_copper)
    # copper cannot exist where there is no board (holes and outside), so the face cover is cut by the area
    nl = len(stack.names)
    t_share = []
    for i in range(nl):
        up = stack.gap_mm[i - 1] if i > 0 else 0.0
        down = stack.gap_mm[i] if i < nl - 1 else 0.0
        t_share.append(0.5 * (up + down))
    gx, gy, cells = [], [], []
    for i in range(nl):
        cell, cx, cy = _face_cover(masks[i], grid)
        kt_fr4 = params.k_in * t_share[i] * 1e-3
        kt_cu = K_CU * stack.cu_mm[i] * 1e-3
        gx.append(kt_cu * cx + kt_fr4)
        gy.append(kt_cu * cy + kt_fr4)
        cells.append(cell)
    a_m2 = (grid.d * 1e-3) ** 2
    gz = []
    for i in range(nl - 1):
        gap = stack.gap_mm[i] * 1e-3
        gz.append(params.k_z * a_m2 * area / gap)
    # plated barrels in parallel with the dielectric
    plating = params.plating_um * 1e-3
    for s in sites:
        r_in = s["drill"] / 2.0
        a_barrel = math.pi * ((r_in + plating) ** 2 - r_in**2) * 1e-6  # m2
        pts = _spread_points(s["x"], s["y"], s.get("diameter", s["drill"]) / 2.0, grid)
        if not pts:
            continue
        i0 = stack.names.index(s["span"][0])
        for k in range(len(s["span"]) - 1):
            i = i0 + k
            seg = (stack.gap_mm[i] + 0.5 * (stack.cu_mm[i] + stack.cu_mm[i + 1])) * 1e-3
            g_total = K_CU * a_barrel / seg
            for (j, c) in pts:
                if 0 <= j < grid.ny and 0 <= c < grid.nx and area[j, c] > 0:
                    gz[i][j, c] += g_total / len(pts)
    return Model(grid.ny, grid.nx, grid.d, area, gx, gy, gz, grid.x0, grid.y0), cells


def _spread_points(x: float, y: float, r: float, grid: Grid) -> list[tuple[int, int]]:
    if r <= grid.d / 2.0:
        return [grid.cell(x, y)]
    pts = []
    n = 5
    for a in np.linspace(-r, r, n):
        for b in np.linspace(-r, r, n):
            if a * a + b * b <= r * r:
                pts.append(grid.cell(x + a, y + b))
    return pts or [grid.cell(x, y)]


# ------------------------------------------------------------------------- heat sources


@dataclass
class HeatSource:
    label: str
    watts: float
    kind: str  # 'user' | 'datasheet' | 'power_sim' | 'derived' | 'default'
    cite: str
    layer: int
    ref: str | None = None
    region: tuple[float, float, float, float] | None = None
    points: list[tuple[float, float]] | None = None
    joule: bool = False
    #: when set, the heat enters where these copper shapes are (the part's pads) instead of over the whole box
    rings: list[list[list[float]]] | None = None


def rasterize_sources(sources: list[HeatSource], grid: Grid, area: np.ndarray, nl: int) -> tuple[np.ndarray, np.ndarray, list[str]]:
    q_fixed = np.zeros((nl, grid.ny, grid.nx))
    q_joule = np.zeros((nl, grid.ny, grid.nx))
    lost: list[str] = []
    for s in sources:
        target = q_joule if s.joule else q_fixed
        w = np.zeros((grid.ny, grid.nx))
        if s.rings:
            canvas = _Canvas(grid)
            for ring in s.rings:
                canvas.poly(ring)
            w = canvas.array().reshape(grid.ny, SS, grid.nx, SS).mean(axis=(1, 3))
        elif s.region is not None:
            x0, y0, x1, y1 = s.region
            if x1 - x0 < grid.d * 0.25:
                cx = 0.5 * (x0 + x1)
                x0, x1 = cx - grid.d * 0.125, cx + grid.d * 0.125
            if y1 - y0 < grid.d * 0.25:
                cy = 0.5 * (y0 + y1)
                y0, y1 = cy - grid.d * 0.125, cy + grid.d * 0.125
            xe = grid.x0 + np.arange(grid.nx + 1) * grid.d
            ye = grid.y0 + np.arange(grid.ny + 1) * grid.d
            ox = np.clip(np.minimum(xe[1:], x1) - np.maximum(xe[:-1], x0), 0, None)
            oy = np.clip(np.minimum(ye[1:], y1) - np.maximum(ye[:-1], y0), 0, None)
            w = np.outer(oy, ox)
        elif s.points:
            for x, y in s.points:
                j, c = grid.cell(x, y)
                if 0 <= j < grid.ny and 0 <= c < grid.nx:
                    w[j, c] += 1.0
        w = w * (area > 0)
        total = float(w.sum())
        if total <= 0:
            lost.append(s.label)
            continue
        target[s.layer] += s.watts * w / total
    return q_fixed, q_joule, lost


def _net_volts(name: str | None) -> float | None:
    if not name:
        return None
    n = name.strip().upper().lstrip("+/")
    if "VBUS" in n and not re.search(r"\d+V\d*", n):
        return 5.0
    m = re.fullmatch(r".*?(\d+)V(\d+)(?:_.*)?", n)
    if m:
        return float(f"{m.group(1)}.{m.group(2)}")
    m = re.fullmatch(r"(\d+(?:\.\d+)?)V", n)
    if m:
        return float(m.group(1))
    return None


def find_ldos(board: Board) -> list[dict]:
    out = []
    for f in board.footprints:
        if f.dnp or SKIP_PART.match(f.ref):
            continue
        text = f"{f.value} {f.lib_id}"
        if not f.ref.upper().startswith(("U", "Q", "IC", "VR")) or not LDO_PATTERN.search(text):
            continue
        nets = sorted({p.net for p in board.pads_of(f.ref) if p.net})
        volts = {n: _net_volts(n) for n in nets if _net_volts(n) is not None}
        vout = None
        m = re.search(r"-(\d(?:\.\d)?)\b", f.value)
        if m:
            vout = float(m.group(1))
        elif volts:
            vout = min(volts.values())
        vin = max(volts.values()) if volts else None
        out.append(
            {
                "ref": f.ref,
                "value": f.value,
                "vin_v": vin,
                "vout_v": vout,
                "nets": {n: v for n, v in volts.items()},
                "basis": "vin from the highest supply-looking net name on its pads, vout from the value text or the lowest",
            }
        )
    return out


def part_region(board: Board, ref: str) -> tuple[tuple[float, float, float, float], str, list[list[list[float]]]] | None:
    """Where a part's heat enters the board: its exposed pad if it has one, otherwise the box around its pads."""
    pads = board.pads_of(ref)
    pads = [p for p in pads if p.ring]
    if not pads:
        f = board.footprint(ref)
        return (tuple(f.bbox), "footprint box", []) if f else None  # type: ignore[return-value]
    boxes = []
    for p in pads:
        xs = [q[0] for q in p.ring]
        ys = [q[1] for q in p.ring]
        boxes.append((min(xs), min(ys), max(xs), max(ys)))
    x0, y0 = min(b[0] for b in boxes), min(b[1] for b in boxes)
    x1, y1 = max(b[2] for b in boxes), max(b[3] for b in boxes)
    total = (x1 - x0) * (y1 - y0)
    for b in boxes:
        a = (b[2] - b[0]) * (b[3] - b[1])
        if a >= 4.0 and total > 0 and a >= 0.2 * total and len(boxes) > 4:
            return b, "exposed pad", [p.ring for p, bb in zip(pads, boxes) if bb == b][:1]
    return (x0, y0, x1, y1), "pad extent", [p.ring for p in pads]


def part_layer(board: Board, stack: Stack, ref: str) -> int:
    f = board.footprint(ref)
    return len(stack.names) - 1 if f is not None and f.side == "bottom" else 0


def _num(v: Any) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def collect_sources(board: Board, stack: Stack, inputs: dict, whatif: list[dict], assumptions: list[Assumption], notes: list[str]) -> tuple[list[HeatSource], dict[str, dict]]:
    """Turn the stated inputs into heat sources. Nothing is invented: a part with no stated power gets none."""
    sources: list[HeatSource] = []
    info: dict[str, dict] = {}
    use_defaults = bool(inputs.get("use_defaults", True))
    refs = {f.ref for f in board.footprints}

    def add_part(ref: str, watts: float, kind: str, cite: str, label: str | None = None) -> None:
        reg = part_region(board, ref)
        if reg is None:
            notes.append(f"{ref}: no footprint on the board, its power was not applied")
            return
        region, what, rings = reg
        sources.append(HeatSource(label or ref, watts, kind, cite, part_layer(board, stack, ref), ref=ref, region=region, rings=rings or None))
        info[ref] = {"power_w": watts, "power_kind": kind, "cite": cite, "region_basis": what}
        assumptions.append(Assumption(f"power {ref}", round(watts, 5), "W", {"user": "user", "datasheet": "datasheet", "power_sim": "user"}.get(kind, "default"), cite))

    explicit: dict[str, tuple[float, str, str]] = {}
    for ref, v in (inputs.get("part_power") or {}).items():
        if ref not in refs:
            notes.append(f"part_power names {ref}, which is not on the board")
            continue
        if isinstance(v, dict):
            w = _num(v.get("watts"))
            kind = v.get("source", "user") if v.get("source") in ("user", "datasheet", "power_sim", "default") else "user"
            cite = v.get("cite") or {"user": "stated by the user", "datasheet": "datasheet", "power_sim": "power simulation", "default": "default"}[kind]
        else:
            w, kind, cite = _num(v), "user", "stated by the user"
        if w is None or w < 0:
            notes.append(f"part_power for {ref} is not a non-negative number; ignored")
            continue
        explicit[ref] = (w, kind, cite)
    for op in whatif or []:
        if op.get("op") == "set_part_power":
            ref, w = op.get("ref"), _num(op.get("watts"))
            if ref in refs and w is not None and w >= 0:
                explicit[ref] = (w, "user", "what-if in simulation memory, not an edit of the design")
    for ref, (w, kind, cite) in explicit.items():
        if w > 0:
            add_part(ref, w, kind, cite)

    for ldo in find_ldos(board):
        ref = ldo["ref"]
        if ref in explicit:
            continue
        spec = (inputs.get("ldo") or {}).get(ref, {})
        vin = _num(spec.get("vin_v")) if spec.get("vin_v") is not None else ldo["vin_v"]
        vout = _num(spec.get("vout_v")) if spec.get("vout_v") is not None else ldo["vout_v"]
        vsrc = "user" if spec.get("vin_v") is not None and spec.get("vout_v") is not None else "board"
        load = _num(spec.get("load_a"))
        iq = _num(spec.get("iq_a")) or 0.0
        if vin is None or vout is None or vin <= vout:
            notes.append(f"{ref} looks like a regulator ({ldo['value']}) but its input and output voltages are not known (vin {vin}, vout {vout}); no heat was assumed for it")
            continue
        load_kind = "user"
        if load is None:
            if not use_defaults:
                notes.append(f"{ref} is a regulator dropping {vin - vout:.2f} V but no load current was supplied; no heat was assumed for it")
                continue
            load = DEFAULTS["default_ldo_load_a"][0]
            load_kind = "default"
        for nm, val, unit, src, cite in (
            (f"{ref} input voltage", vin, "V", vsrc, ldo["basis"] if vsrc == "board" else "stated by the user"),
            (f"{ref} output voltage", vout, "V", vsrc, ldo["basis"] if vsrc == "board" else "stated by the user"),
            (f"{ref} load current", load, "A", "default" if load_kind == "default" else "user", DEFAULTS["default_ldo_load_a"][3] if load_kind == "default" else "stated by the user"),
        ):
            assumptions.append(Assumption(nm, round(val, 4), unit, src, cite))
        watts = (vin - vout) * load + vin * iq
        if watts > 0:
            kind = "default" if load_kind == "default" else "derived"
            cite = f"({vin:g} V - {vout:g} V) x {load:g} A" + (f" + {vin:g} V x {iq:g} A ground current" if iq else "") + (" with a placeholder load" if load_kind == "default" else "")
            if vsrc == "board" and load_kind != "default":
                cite += "; voltages read from net names"
            add_part(ref, watts, kind, cite, f"{ref} regulator drop")
            info[ref]["ldo"] = {"vin_v": vin, "vout_v": vout, "load_a": load, "drop_v": vin - vout}

    # copper loss from a stated net current, hotter as the copper warms
    nets_i = inputs.get("net_currents") or {}
    for net, amps in nets_i.items():
        a = _num(amps)
        if a is None or a <= 0:
            continue
        tracks = [t for t in board.tracks if t.net == net and t.layer in stack.names]
        if not tracks:
            notes.append(f"net_currents names {net}, which has no tracks on a copper layer; no copper loss applied")
            continue
        for t in tracks:
            r = RHO_CU_20 * (t.length * 1e-3) / ((t.width * 1e-3) * (stack.cu_mm[stack.names.index(t.layer)] * 1e-3))
            w = a * a * r
            pts = _sample_polyline(t.points(), 0.1)
            sources.append(HeatSource(f"{net} copper {t.id}", w, "user", f"I^2 R at 20 C with {a:g} A through every track of the net", stack.names.index(t.layer), points=pts, joule=True))
        assumptions.append(Assumption(f"current {net}", a, "A", "user", "the whole current is taken through every track of the net (an upper bound for branches); copper loss is I^2 R at 20 C scaled by 1 + 0.00393 (T - 20)"))
    for tid, watts in (inputs.get("track_watts") or {}).items():
        w = _num(watts)
        trk = next((t for t in board.tracks if t.id == tid), None)
        if w is None or w <= 0 or trk is None or trk.layer not in stack.names:
            continue
        sources.append(HeatSource(f"copper {tid}", w, "power_sim", "I^2 R from the power simulation", stack.names.index(trk.layer), points=_sample_polyline(trk.points(), 0.1)))
    for extra in inputs.get("extra_sources") or []:
        w = _num(extra.get("watts"))
        if w is None or w <= 0 or _num(extra.get("x")) is None or _num(extra.get("y")) is None:
            continue
        layer = extra.get("layer")
        li = stack.names.index(layer) if layer in stack.names else 0
        half = (_num(extra.get("size_mm")) or 1.0) / 2.0
        x, y = float(extra["x"]), float(extra["y"])
        kind = extra.get("source") if extra.get("source") in ("user", "datasheet", "power_sim", "default") else "user"
        sources.append(HeatSource(str(extra.get("label") or "extra source"), w, kind, extra.get("cite") or "stated by the caller", li, region=(x - half, y - half, x + half, y + half)))
        assumptions.append(Assumption(f"power {extra.get('label') or 'extra source'}", w, "W", "default" if kind == "default" else "user", extra.get("cite") or "stated by the caller"))
    return sources, info


def _sample_polyline(points, step: float) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for (x0, y0), (x1, y1) in zip(points[:-1], points[1:]):
        n = max(int(math.ceil(math.hypot(x1 - x0, y1 - y0) / step)), 1)
        for k in range(n):
            f = (k + 0.5) / n
            out.append((x0 + f * (x1 - x0), y0 + f * (y1 - y0)))
    return out or [tuple(points[0])]


# ------------------------------------------------------------------------- what-ifs


def parse_whatif(whatif: list[dict], stack: Stack, board: Board) -> tuple[list[dict], list[dict], list[str]]:
    """What-if vias and copper pours, in memory only. Returns (vias, pours, notes)."""
    vias: list[dict] = []
    pours: list[dict] = []
    notes: list[str] = []
    for op in whatif or []:
        name = op.get("op")
        if name == "add_via":
            drill = _num(op.get("drill")) or 0.3
            dia = _num(op.get("diameter")) or max(drill + 0.3, 0.6)
            span = _via_span(stack, op["layers"]) if op.get("layers") else list(stack.names)
            if "at" in op and op["at"]:
                pts = [tuple(op["at"])]
            elif "rect" in op:
                x0, y0, x1, y1 = op["rect"]
                pitch = _num(op.get("pitch")) or 1.0
                pts = [(x, y) for x in np.arange(x0, x1 + 1e-9, pitch) for y in np.arange(y0, y1 + 1e-9, pitch)]
            else:
                notes.append("add_via needs 'at' [x, y] or 'rect' with a 'pitch'; ignored")
                continue
            for x, y in pts[:400]:
                vias.append({"x": float(x), "y": float(y), "drill": drill, "diameter": dia, "span": span, "net": op.get("net")})
            notes.append(f"what-if: {min(len(pts), 400)} thermal via(s), drill {drill:g} mm, in simulation memory only")
        elif name == "add_pour":
            layer = op.get("layer")
            if layer not in stack.names:
                notes.append(f"add_pour layer {layer!r} is not a copper layer; ignored")
                continue
            if op.get("polygon"):
                poly = [tuple(p) for p in op["polygon"]]
            elif op.get("rect"):
                x0, y0, x1, y1 = op["rect"]
                poly = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
            else:
                notes.append("add_pour needs 'polygon' or 'rect'; ignored")
                continue
            pours.append({"layer": layer, "polygon": poly})
            notes.append(f"what-if: copper pour on {layer}, in simulation memory only")
    return vias, pours, notes


# ------------------------------------------------------------------------- analysis


def _cells_of(grid: Grid, region, margin: float = 0.0):
    x0, y0, x1, y1 = region
    c0 = max(int(math.floor((x0 - margin - grid.x0) / grid.d)), 0)
    c1 = min(int(math.ceil((x1 + margin - grid.x0) / grid.d)), grid.nx)
    j0 = max(int(math.floor((y0 - margin - grid.y0) / grid.d)), 0)
    j1 = min(int(math.ceil((y1 + margin - grid.y0) / grid.d)), grid.ny)
    return j0, j1, c0, c1


def _field_stats(t: np.ndarray, grid: Grid, region) -> tuple[float, float, tuple[float, float]] | None:
    j0, j1, c0, c1 = _cells_of(grid, region)
    sub = t[j0:j1, c0:c1]
    if sub.size == 0 or np.all(np.isnan(sub)):
        return None
    jj, cc = np.unravel_index(np.nanargmax(sub), sub.shape)
    return float(np.nanmax(sub)), float(np.nanmean(sub)), (float(grid.x0 + (cc + c0 + 0.5) * grid.d), float(grid.y0 + (jj + j0 + 0.5) * grid.d))


@dataclass
class Case:
    grid: Grid
    model: Model
    cells: list[np.ndarray]
    sol: Solution
    lost: list[str]


def run_case(board: Board, stack: Stack, params: Params, pitch: float, sources: list[HeatSource], sites: list[dict], pours: list[dict], inputs_eps: float, no_copper: bool = False) -> Case:
    grid = make_grid(board, pitch)
    model, cells = build_model(board, grid, stack, params, sites, pours, no_copper)
    qf, qj, lost = rasterize_sources(sources, grid, model.area, model.nl)
    sol = solve_model(model, qf, qj, params.ambient_c, params.h_conv, params.h_conv, inputs_eps, inputs_eps)
    return Case(grid, model, cells, sol, lost)


def _peak(case: Case) -> tuple[float, tuple[int, int, int]]:
    t = case.sol.temp_c
    k = int(np.nanargmax(t))
    return float(np.nanmax(t)), np.unravel_index(k, t.shape)  # type: ignore[return-value]


def _mean_rise(case: Case, ambient: float) -> float:
    t = case.sol.temp_c
    w = np.broadcast_to(case.model.area, t.shape)
    ok = ~np.isnan(t)
    return float(np.sum((t[ok] - ambient) * w[ok]) / np.sum(w[ok]))


class ThermalSimulator(Simulator):
    kind = "thermal"
    label = "Board temperature"
    whatif_ops = ("set_part_power", "add_via", "add_pour")

    def available(self, board, ctx) -> tuple[bool, str]:
        if not board.copper_layers():
            return False, "the board has no copper layers"
        if not board.footprints:
            return False, "the board has no parts to carry heat sources"
        return True, ""

    def inputs_needed(self, board, ctx) -> list[dict]:
        need: list[dict] = []
        for name, (val, unit, src, cite) in DEFAULTS.items():
            if name in ("k_fr4_inplane", "k_fr4_through", "via_plating_um"):
                continue
            need.append({"name": name, "label": name.replace("_", " "), "value": val, "unit": unit, "source": src, "cite": cite, "required": False})
        for ldo in find_ldos(board):
            vin, vout = ldo["vin_v"], ldo["vout_v"]
            need.append(
                {
                    "name": f"ldo.{ldo['ref']}",
                    "label": f"{ldo['ref']} ({ldo['value']}) regulator: input and output volts and load current",
                    "value": {"vin_v": vin, "vout_v": vout, "load_a": DEFAULTS["default_ldo_load_a"][0]},
                    "unit": "V, V, A",
                    "source": "default",
                    "cite": ldo["basis"] + "; the load is a placeholder and is the number that matters",
                    "required": False,
                    "heat": f"{(vin - vout) * DEFAULTS['default_ldo_load_a'][0]:.3f} W at the placeholder load" if vin and vout and vin > vout else None,
                }
            )
        need.append({"name": "part_power", "label": "power dissipated by a part, {ref: watts or {watts, source, cite}}", "value": {}, "unit": "W", "source": "none", "cite": "from a datasheet, a measurement or the power simulation; a part with no value gets no heat", "required": not find_ldos(board)})
        need.append({"name": "net_currents", "label": "copper loss from a net current, {net: amps}", "value": {}, "unit": "A", "source": "none", "cite": "from the power simulation or the user", "required": False})
        need.append({"name": "parts", "label": "per part limits and theta values, {ref: {theta_jb, theta_ja, tj_max_c, t_max_c}}", "value": {}, "unit": "K/W, C", "source": "none", "cite": "datasheet; with no theta no junction temperature is given", "required": False})
        return need

    def run(self, board, ctx, inputs: dict, whatif: list[dict], out_dir: Path) -> SimResult:
        t0 = time.perf_counter()
        inputs = dict(inputs or {})
        out_dir = Path(out_dir)
        stack = board_stack(board)
        assumptions: list[Assumption] = []
        notes: list[str] = []

        def pick(name: str) -> float:
            v = _num(inputs.get(name))
            val, unit, src, cite = DEFAULTS[name]
            if v is None:
                assumptions.append(Assumption(name, val, unit, src, cite))
                return val
            assumptions.append(Assumption(name, v, unit, "user", "stated by the user"))
            return v

        params = Params(
            ambient_c=pick("ambient_c"),
            h_conv=pick("h_conv"),
            emissivity=min(max(pick("emissivity"), 0.0), 1.0),
            k_in=pick("k_fr4_inplane"),
            k_z=pick("k_fr4_through"),
            plating_um=pick("via_plating_um"),
        )
        if params.h_conv <= 0:
            return SimResult(self.kind, "not_possible", "h_conv must be positive: with no way to lose heat there is no steady state")
        assumptions.append(Assumption("stackup", stack.source, "-", "board" if stack.source == "board" else "default", "copper " + ", ".join(f"{n} {t * 1000:.0f} um" for n, t in zip(stack.names, stack.cu_mm)) + "; dielectric " + ", ".join(f"{g:.2f} mm" for g in stack.gap_mm)))

        extra_vias, pours, wnotes = parse_whatif(whatif, stack, board)
        notes += wnotes
        sources, info = collect_sources(board, stack, inputs, whatif, assumptions, notes)
        if not sources:
            return SimResult(
                self.kind,
                "not_possible",
                "No heat source is known, so no temperature is given. Supply the power of at least one part (part_power), a regulator's load current (ldo), or a net current (net_currents).",
                notes=notes,
                assumptions=assumptions,
                solve_s=round(time.perf_counter() - t0, 3),
            )

        sites = via_sites(board, extra_vias, stack)
        pitch = _num(inputs.get("pitch_mm")) or default_pitch(board, len(stack.names))
        pitch = min(max(pitch, 0.1), 5.0)
        eps = params.emissivity
        base = run_case(board, stack, params, pitch, sources, sites, pours, eps)
        if base.lost:
            notes.append("heat sources that fall outside the board and were dropped: " + ", ".join(base.lost))
        sol = base.sol
        if sol.q_in_w <= 0:
            return SimResult(self.kind, "not_possible", "every heat source fell outside the board or was zero", notes=notes, assumptions=assumptions)

        # grid convergence: the same case on a raster 1.5 times as coarse; the gap bounds the error of the fine one
        ref_pitch, ref_label = round(pitch * 1.5, 4), "coarser"
        ref_case = run_case(board, stack, params, ref_pitch, sources, sites, pours, eps)

        ta = params.ambient_c
        peak_t, (pl, pj, pc) = _peak(base)
        peak_ref = float(np.nanmax(ref_case.sol.temp_c))
        mean_rise = _mean_rise(base, ta)
        mean_ref = _mean_rise(ref_case, ta)
        peak_diff = abs(peak_t - peak_ref) / max(peak_t - ta, 1e-9)
        mean_diff = abs(mean_rise - mean_ref) / max(mean_rise, 1e-9)

        checks: list[dict[str, Any]] = []

        def check(name: str, status: str, value: float | str, limit: float | str, detail: str) -> None:
            checks.append({"name": name, "status": status, "value": value, "limit": limit, "detail": detail})

        bal = sol.balance_error
        check("energy_balance", "ok" if bal <= 0.01 else "failed", round(bal, 6), 0.01, f"heat in {sol.q_in_w:.4f} W, out by convection {sol.q_conv_w:.4f} W and radiation {sol.q_rad_w:.4f} W")
        check("solver", "ok" if (sol.converged and sol.residual < 1e-8) else "failed", f"{sol.iterations} iterations, residual {sol.residual:.1e}", "residual < 1e-8", "radiation and copper-loss iteration, then the linear residual")
        # the mean over each powered part's own footprint is far less mesh-sensitive than a single hot cell
        part_diff = 0.0
        for ref in info:
            reg = part_region(board, ref)
            if reg is None:
                continue
            li = part_layer(board, stack, ref)
            a, b = _field_stats(sol.temp_c[li], base.grid, reg[0]), _field_stats(ref_case.sol.temp_c[li], ref_case.grid, reg[0])
            if a and b:
                part_diff = max(part_diff, abs(a[1] - b[1]) / max(a[1] - ta, 1e-9))
        status = "failed" if (mean_diff > 0.03 or part_diff > 0.08 or peak_diff > 0.30) else ("warn" if peak_diff > 0.12 else "ok")
        check(
            "grid_convergence",
            status,
            f"board mean {mean_diff * 100:.2f} %, part mean {part_diff * 100:.2f} %, peak {peak_diff * 100:.2f} %",
            "board mean 3 %, part mean 8 %, peak 30 % (warn above 12 %), as a share of the rise",
            f"{pitch:g} mm raster against a {ref_label} {ref_pitch:g} mm raster (peak {peak_t - ta:.2f} K against {peak_ref - ta:.2f} K). The single hottest cell depends on how finely pad copper is resolved; the part and board means are the steadier numbers. Re-run with a smaller pitch_mm to confirm.",
        )
        rise = peak_t - ta
        tmin = float(np.nanmin(sol.temp_c))
        sane = np.isfinite(sol.temp_c[~np.isnan(sol.temp_c)]).all() and tmin >= ta - 1e-6 and rise < MAX_RISE_K
        check("sanity_range", "ok" if sane else "failed", f"peak rise {rise:.1f} K, minimum {tmin - ta:+.3f} K", f"0 to {MAX_RISE_K:g} K", "no cooling below ambient and a rise the surface model can describe" + ("; above %g K treat the number as an alarm, not a prediction" % WARN_RISE_K if rise >= WARN_RISE_K else ""))
        kinds = {s.kind for s in sources}
        basis = "default" if kinds <= {"default"} else ("mixed" if "default" in kinds else "stated")
        check("heat_source_basis", "warn" if basis == "default" else "ok", basis, "stated or derived inputs", "all heat comes from labelled defaults: the temperatures show the method, not this board's real heating" if basis == "default" else "heat comes from stated or derived inputs" + ("; some from labelled defaults" if basis == "mixed" else ""))
        trust_ok = all(c["status"] != "failed" for c in checks)

        # copper effect and what-if delta
        extras: dict[str, Any] = {}
        if time.perf_counter() - t0 < 9.0:
            nocu = run_case(board, stack, params, pitch, sources, sites, [], eps, no_copper=True)
            r_nocu = float(np.nanmax(nocu.sol.temp_c)) - ta
            extras["peak_rise_without_copper_k"] = r_nocu
        base_whatif: Case | None = None
        if (extra_vias or pours or any(op.get("op") == "set_part_power" for op in whatif or [])) and time.perf_counter() - t0 < 9.0:
            s0, _ = collect_sources(board, stack, inputs, [], [], [])
            base_whatif = run_case(board, stack, params, pitch, s0, via_sites(board, [], stack), [], eps) if s0 else None

        # per part
        cfg = inputs.get("parts") or {}
        parts: list[dict[str, Any]] = []
        sol_t = sol.temp_c
        for f in board.footprints:
            if SKIP_PART.match(f.ref):
                continue
            reg = part_region(board, f.ref)
            if reg is None:
                continue
            li = part_layer(board, stack, f.ref)
            st = _field_stats(sol_t[li], base.grid, reg[0])
            if st is None:
                continue
            tmax, tmean, (hx, hy) = st
            row: dict[str, Any] = {
                "ref": f.ref,
                "value": f.value,
                "layer": stack.names[li],
                "power_w": info.get(f.ref, {}).get("power_w", 0.0),
                "power_basis": info.get(f.ref, {}).get("cite") if f.ref in info else "no power stated: heat only arrives from neighbours",
                "power_kind": info.get(f.ref, {}).get("power_kind", "none"),
                "t_board_max_c": round(tmax, 3),
                "t_board_mean_c": round(tmean, 3),
                "rise_max_k": round(tmax - ta, 3),
                "hottest_at": [round(float(hx), 2), round(float(hy), 2)],
            }
            c = cfg.get(f.ref) or {}
            p = row["power_w"]
            tjb, tja = _num(c.get("theta_jb")), _num(c.get("theta_ja"))
            tjmax, tmaxc = _num(c.get("tj_max_c")), _num(c.get("t_max_c"))
            if tjb is not None and p > 0:
                row["tj_c"] = round(tmean + p * tjb, 3)
                row["tj_basis"] = f"board temperature under the part plus power x theta_jb {tjb:g} K/W (theta supplied by the caller)"
            elif tjb is not None:
                row["tj_c"] = round(tmean, 3)
                row["tj_basis"] = "no power stated, equals the board temperature"
            if tja is not None and p > 0:
                row["tj_ja_c"] = round(ta + p * tja, 3)
                row["tj_ja_basis"] = f"ambient plus power x theta_ja {tja:g} K/W (JEDEC board, not this layout)"
            limit = None
            if tjmax is not None:
                if "tj_c" in row:
                    limit = {"limit_c": tjmax, "margin_k": round(tjmax - row["tj_c"], 3), "basis": "junction limit against the junction temperature from theta_jb"}
                else:
                    limit = {"limit_c": tjmax, "margin_k": round(tjmax - tmax, 3), "basis": "junction limit against the BOARD temperature only: the junction is hotter, so this margin is an upper bound"}
            elif tmaxc is not None:
                limit = {"limit_c": tmaxc, "margin_k": round(tmaxc - tmax, 3), "basis": "limit on local board or case temperature"}
            if limit:
                row.update(limit)
            if base_whatif is not None:
                bst = _field_stats(base_whatif.sol.temp_c[li], base_whatif.grid, reg[0])
                if bst:
                    row["delta_max_k_vs_base"] = round(tmax - bst[0], 3)
            parts.append(row)
        parts.sort(key=lambda r: (-r["t_board_max_c"], r["ref"]))

        # hot spots
        tb = np.nanmax(np.where(np.isnan(sol_t), -np.inf, sol_t), axis=0)
        tb = np.where(np.isfinite(tb), tb, np.nan)
        hot = _hotspots(tb, base.grid, board, ta)

        # copper areas
        a_mm2 = base.grid.d**2
        area_mm2 = float(base.model.area.sum() * a_mm2)
        cu_rows = []
        for i, name in enumerate(stack.names):
            frac = float((base.cells[i] * base.model.area).sum() * a_mm2 / max(area_mm2, 1e-9))
            cu_rows.append({"layer": name, "copper_fraction": round(frac, 4), "copper_mm2": round(frac * area_mm2, 1), "thickness_um": round(stack.cu_mm[i] * 1000, 1)})
        n_th = sum(1 for s in sites if s["kind"] in ("via", "whatif"))
        copper = {"layers": cu_rows, "board_area_mm2": round(area_mm2, 1), "vias": n_th, "plated_holes": sum(1 for s in sites if s["kind"] == "hole"), "whatif_vias": len(extra_vias), "whatif_pours": len(pours)}
        for ref, d in info.items():
            reg = part_region(board, ref)
            if reg is None:
                continue
            li = part_layer(board, stack, ref)
            cx, cy = 0.5 * (reg[0][0] + reg[0][2]), 0.5 * (reg[0][1] + reg[0][3])
            r = 10.0
            j0, j1, c0, c1 = _cells_of(base.grid, (cx, cy, cx, cy), r)
            d["copper_within_10mm_mm2"] = round(float(base.cells[li][j0:j1, c0:c1].sum() * a_mm2), 1)
            d["vias_within_5mm"] = sum(1 for s in sites if math.hypot(s["x"] - cx, s["y"] - cy) <= 5.0)

        # overlays
        overlays = self._write_overlays(base, stack, ta, out_dir, tb)

        total_w = sol.q_in_w
        metrics: dict[str, dict] = {
            "ambient": {"value": ta, "unit": "C", "label": "ambient air temperature (input)"},
            "total_power": {"value": round(total_w, 5), "unit": "W", "label": "heat put into the board (sum of the stated sources)"},
            "peak_temperature": {"value": round(peak_t, 3), "unit": "C", "label": "hottest board point, simulated"},
            "peak_rise": {"value": round(rise, 3), "unit": "K", "label": "hottest board point above ambient, simulated"},
            "peak_at": {"value": [round(float(base.grid.x0 + (pc + 0.5) * base.grid.d), 2), round(float(base.grid.y0 + (pj + 0.5) * base.grid.d), 2)], "unit": "mm", "label": f"where the hottest point is, on {stack.names[pl]}"},
            "mean_rise": {"value": round(mean_rise, 3), "unit": "K", "label": "board area-mean rise above ambient, simulated"},
            "heat_out_convection": {"value": round(sol.q_conv_w, 5), "unit": "W", "label": "heat leaving by convection"},
            "heat_out_radiation": {"value": round(sol.q_rad_w, 5), "unit": "W", "label": "heat leaving by radiation"},
            "energy_balance_error": {"value": round(bal, 6), "unit": "fraction", "label": "relative gap between heat in and heat out"},
            "power_basis": {"value": basis, "unit": "-", "label": "where the heat numbers come from: stated, mixed or default only"},
        }
        if "peak_rise_without_copper_k" in extras:
            metrics["peak_rise_without_copper"] = {"value": round(extras["peak_rise_without_copper_k"], 3), "unit": "K", "label": "peak rise if the board had no copper at all (FR-4 only), same heat; the difference is the copper's effect"}
        if base_whatif is not None:
            b_peak = float(np.nanmax(base_whatif.sol.temp_c)) - ta
            metrics["peak_rise_before_whatif"] = {"value": round(b_peak, 3), "unit": "K", "label": "peak rise without the what-ifs"}
            metrics["peak_rise_change"] = {"value": round(rise - b_peak, 3), "unit": "K", "label": "change of the peak rise caused by the what-ifs"}
        hottest = next((p for p in parts if p["power_w"] > 0), None)
        if hottest:
            metrics["hottest_powered_part"] = {"value": hottest["ref"], "unit": "-", "label": f"powered part with the highest board temperature, {hottest['t_board_max_c']:.1f} C"}

        notes += [
            "Heat leaves only through the two board faces, by convection with the stated h and by radiation. There is no enclosure, no forced airflow, no neighbouring heat and no component bodies shading or finning the board.",
            "Board edges, mounting hardware, connectors' metal and cables are treated as adiabatic; real boards lose a little more through them, so these are conservative.",
            "In-plane copper is homogenised per cell face (copper that crosses the face, at 4x resolution), so a track narrower than a cell conducts correctly along itself but heat spreads across cells only through copper that is really there.",
            f"Raster pitch {pitch:g} mm; row 0 of every overlay is y = {base.grid.y0:g} mm and y grows downward as in the board file.",
            "Junction temperature is given only for parts whose theta values were supplied, and says so. Board temperature is a lower bound of a part's junction temperature.",
        ]
        detail = {
            "parts": parts,
            "hotspots": hot,
            "copper": copper,
            "sources": [{"label": s.label, "watts": round(s.watts, 6), "kind": s.kind, "cite": s.cite, "layer": stack.names[s.layer]} for s in sources],
            "grid": {"pitch_mm": pitch, "nx": base.grid.nx, "ny": base.grid.ny, "layers": len(stack.names), "x0": base.grid.x0, "y0": base.grid.y0, "reference_pitch_mm": ref_pitch},
            "stackup": {"names": stack.names, "copper_mm": stack.cu_mm, "gap_mm": stack.gap_mm, "source": stack.source},
            "whatif": list(whatif or []),
            "solver": {"iterations": sol.iterations, "residual": sol.residual, "solve_s": round(sol.solve_s, 3)},
        }
        res = SimResult(
            kind=self.kind,
            status="ok",
            metrics=metrics,
            trust={"status": "ok" if trust_ok else "failed", "checks": checks},
            overlays=overlays,
            assumptions=_dedupe(assumptions),
            notes=notes,
            solve_s=round(time.perf_counter() - t0, 3),
            detail=detail,
        )
        return res

    def _write_overlays(self, case: Case, stack: Stack, ta: float, out_dir: Path, tb: np.ndarray) -> list[Overlay]:
        out_dir.mkdir(parents=True, exist_ok=True)
        g = case.grid
        peak = float(np.nanmax(case.sol.temp_c))
        vmax = max(peak, ta + 1.0)
        items = [("temp_board", "Board temperature (hottest layer at each point), simulated", "board", tb)]
        for i, name in enumerate(stack.names):
            items.append((f"temp_{name.replace('.', '_')}", f"{name} temperature, simulated", name, case.sol.temp_c[i]))
        overlays = []
        for oid, label, layer, arr in items:
            fname = f"{oid}.bin"
            np.asarray(arr, dtype="<f4").tofile(out_dir / fname)
            overlays.append(Overlay(oid, label, "C", layer, g.x0, g.y0, g.d, g.d, g.nx, g.ny, float(ta), float(vmax), fname, "sequential"))
        return overlays


def _hotspots(tb: np.ndarray, grid: Grid, board: Board, ta: float, count: int = 8, sep_mm: float = 3.0) -> list[dict]:
    filled = np.where(np.isnan(tb), -np.inf, tb)
    size = max(int(sep_mm / grid.d), 1) * 2 + 1
    peaks = (filled == maximum_filter(filled, size=size)) & np.isfinite(filled)
    js, cs = np.nonzero(peaks)
    order = np.argsort(-filled[js, cs])
    out = []
    for k in order[: count * 3]:
        j, c = int(js[k]), int(cs[k])
        x, y = grid.x0 + (c + 0.5) * grid.d, grid.y0 + (j + 0.5) * grid.d
        if filled[j, c] - ta < 0.05 and out:
            break
        near, best = None, 1e9
        for f in board.footprints:
            if SKIP_PART.match(f.ref):
                continue
            b = f.bbox
            dx = max(b[0] - x, 0, x - b[2])
            dy = max(b[1] - y, 0, y - b[3])
            dist = math.hypot(dx, dy)
            if dist < best:
                best, near = dist, f.ref
        out.append({"x": round(x, 2), "y": round(y, 2), "temp_c": round(float(filled[j, c]), 3), "rise_k": round(float(filled[j, c] - ta), 3), "near_ref": near if best < 5.0 else None})
        if len(out) >= count:
            break
    return out


def _dedupe(items: list[Assumption]) -> list[Assumption]:
    seen: set[tuple] = set()
    out = []
    for a in items:
        key = (a.name, str(a.value))
        if key not in seen:
            seen.add(key)
            out.append(a)
    return out


# ------------------------------------------------------------------------- reference formulas


def ipc2221_rise_k(current_a: float, width_mm: float, thickness_um: float = 35.0, external: bool = True) -> float:
    """Temperature rise of a track by the IPC-2221 curve fit I = k dT^0.44 A^0.725 (A in square mils).

    This is the equation that IPC-2152 revised with measured data. It is used here only as a cross-check.
    """
    area_mil2 = (width_mm / 0.0254) * (thickness_um / 25.4)
    k = 0.048 if external else 0.024
    return (current_a / (k * area_mil2**0.725)) ** (1.0 / 0.44)


SIMULATOR = ThermalSimulator()
