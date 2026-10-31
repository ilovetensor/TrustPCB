"""Quasi-static 2D field solver, closed-form references and a lossless cascaded-line TDR.

The solver treats a cross-section of a printed board as a 2D electrostatics problem. Conductors are
rectangles (copper, via barrels) held at a fixed potential, the dielectric stack is a set of horizontal
slabs with a relative permittivity each, and the outer box is grounded. Laplace's equation is solved with
bilinear finite elements on a graded tensor mesh whose lines pass through every conductor edge and every
dielectric interface, so interfaces are exact. The capacitance matrix per metre comes from the stored
energy, ``C_ij = eps0 * phi_i^T K phi_j``, solved once with the real dielectrics and once with air. From
the two:

* ``L = inv(C_air) / c^2`` is the inductance matrix per metre (TEM assumption: the magnetic field does not
  see the dielectric);
* ``Z0 = 1 / (c * sqrt(C * C_air))`` and ``eps_eff = C / C_air`` for one conductor, the others grounded;
* the propagation delay per metre is ``sqrt(eps_eff) / c``.

Everything is lossless and static. It is valid while the cross-section is small against the wavelength in
the dielectric, so it carries no dispersion, skin effect or dielectric loss; ``valid_to_hz`` gives the
frequency up to which the Z0 numbers can be trusted for a given substrate height.

The module also holds the closed forms the solver is benchmarked against (Hammerstad and Jensen microstrip,
IPC-2141, Cohn's exact shielded stripline and coupled stripline, Kirschning and Jansen even and odd
microstrip modes) and ``tdr_response``, a lattice (Bergeron) model of a cascade of uniform lines.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import scipy.sparse as sp
import scipy.sparse.linalg as spla
from scipy.special import ellipk

C_LIGHT = 299792458.0
MU0 = 4e-7 * math.pi
EPS0 = 1.0 / (MU0 * C_LIGHT**2)
ETA0 = MU0 * C_LIGHT  # 376.73 ohm

# ------------------------------------------------------------------------------------------ geometry


@dataclass(frozen=True)
class Rect:
    """A conductor rectangle in the section plane: x across, z up, millimetres. ``cond`` 0 is ground, 1.. are driven conductors."""

    x0: float
    x1: float
    z0: float
    z1: float
    cond: int


@dataclass
class Section:
    """A cross-section: conductors, dielectric slabs from the bottom up, and the grounded box around them."""

    rects: list[Rect]
    #: (z0, z1, relative permittivity) covering the whole box height without gaps, bottom to top
    slabs: list[tuple[float, float, float]]
    xmin: float
    xmax: float
    zmin: float
    zmax: float
    n_cond: int = 1


@dataclass
class Solution:
    c: np.ndarray  # F/m, with the dielectrics
    c_air: np.ndarray  # F/m, all permittivities one
    nodes: int
    nx: int
    nz: int
    solve_s: float = 0.0

    @property
    def n(self) -> int:
        return self.c.shape[0]

    @property
    def l(self) -> np.ndarray:  # noqa: E743 - inductance matrix, H/m
        try:
            return np.linalg.inv(self.c_air) / C_LIGHT**2
        except np.linalg.LinAlgError:  # a conductor covered entirely by another has no capacitance
            return np.linalg.pinv(self.c_air) / C_LIGHT**2

    def z0(self, i: int = 0) -> float:
        """Impedance of conductor ``i`` with every other conductor grounded, ohm."""
        if self.c[i, i] <= 0 or self.c_air[i, i] <= 0:
            return float("nan")
        return 1.0 / (C_LIGHT * math.sqrt(self.c[i, i] * self.c_air[i, i]))

    def eps_eff(self, i: int = 0) -> float:
        if self.c_air[i, i] <= 0:
            return float("nan")
        return float(self.c[i, i] / self.c_air[i, i])

    def delay_per_m(self, i: int = 0) -> float:
        return math.sqrt(self.eps_eff(i)) / C_LIGHT


# ------------------------------------------------------------------------------------------ mesh


def _interval(a: float, b: float, da: float, db: float, r: float, dmax: float) -> list[float]:
    """Interior points of (a, b), cells growing by ``r`` from ``da`` at a and ``db`` at b, capped at ``dmax``."""
    length = b - a
    if length <= 0:
        return []
    if length <= 1.5 * min(da, db):
        return []
    left: list[float] = []
    right: list[float] = []
    x_l, x_r = a, b
    dl, dr = da, db
    while x_r - x_l > 1.25 * (dl + dr):
        x_l += dl
        x_r -= dr
        left.append(x_l)
        right.append(x_r)
        dl = min(dl * r, dmax)
        dr = min(dr * r, dmax)
    gap = x_r - x_l
    mid: list[float] = []
    if gap > 1.6 * max(dl, dr):
        mid.append(x_l + gap / 2)
    return left + mid + right[::-1]


def axis_mesh(crit: list[float], d_crit: list[float], r: float, dmax: float) -> np.ndarray:
    """Mesh lines through every critical coordinate, fine (``d_crit``) beside each and growing away from it."""
    order = sorted(range(len(crit)), key=lambda k: crit[k])
    c = [crit[k] for k in order]
    d = [d_crit[k] for k in order]
    pts = [c[0]]
    for k in range(len(c) - 1):
        if c[k + 1] - c[k] < 1e-9:
            continue
        pts.extend(_interval(c[k], c[k + 1], d[k], d[k + 1], r, dmax))
        pts.append(c[k + 1])
    return np.array(pts)


# element matrices for a bilinear rectangle, nodes ordered (x0,z0) (x1,z0) (x1,z1) (x0,z1)
_MX = np.array([[2, -2, -1, 1], [-2, 2, 1, -1], [-1, 1, 2, -2], [1, -1, -2, 2]], dtype=float)
_MZ = np.array([[2, 1, -1, -2], [1, 2, -2, -1], [-1, -2, 2, 1], [-2, -1, 1, 2]], dtype=float)


def _assemble(dx: np.ndarray, dz: np.ndarray, eps: np.ndarray) -> sp.csr_matrix:
    nx, nz = len(dx) + 1, len(dz) + 1
    ii, jj = np.meshgrid(np.arange(nx - 1), np.arange(nz - 1), indexing="ij")
    n00 = (ii * nz + jj).ravel()
    n10 = ((ii + 1) * nz + jj).ravel()
    n11 = ((ii + 1) * nz + jj + 1).ravel()
    n01 = (ii * nz + jj + 1).ravel()
    nodes = np.stack([n00, n10, n11, n01], axis=1)  # [cells, 4]
    a = dx[ii].ravel()
    b = dz[jj].ravel()
    e = eps.ravel()
    cx = e * b / (6.0 * a)
    cz = e * a / (6.0 * b)
    vals = cx[:, None, None] * _MX[None] + cz[:, None, None] * _MZ[None]
    rows = np.repeat(nodes[:, :, None], 4, axis=2).ravel()
    cols = np.repeat(nodes[:, None, :], 4, axis=1).ravel()
    n = nx * nz
    return sp.coo_matrix((vals.ravel(), (rows, cols)), shape=(n, n)).tocsr()


def build_mesh(sec: Section, fine: float = 1.0, ratio: float = 1.28, dmax_mm: float = 0.6, far_frac: float = 0.12) -> tuple[np.ndarray, np.ndarray]:
    """Mesh lines (x, z) for ``sec``. ``fine`` scales the cell sizes: 1 is the default, 0.5 is twice as fine."""
    xs = {sec.xmin, sec.xmax}
    zs = {sec.zmin, sec.zmax}
    for r in sec.rects:
        xs.update((r.x0, r.x1))
        zs.update((r.z0, r.z1))
    for z0, z1, _ in sec.slabs:
        zs.update((z0, z1))
    # feature sizes set the finest cell: the thinnest conductor edge-to-edge dimension
    thin_z = min([r.z1 - r.z0 for r in sec.rects if r.z1 - r.z0 > 1e-6] or [0.035])
    thin_x = min([r.x1 - r.x0 for r in sec.rects if r.x1 - r.x0 > 1e-6] or [0.2])
    d_z = max(thin_z * 0.5, 0.004) * fine
    d_x = max(min(thin_x * 0.25, thin_z * 1.0), 0.008) * fine

    sig = [(r.x0, r.x1) for r in sec.rects if r.cond >= 1]

    def d_of_x(v: float, lo: float, hi: float) -> float:
        # cells next to a conductor edge are fine; edges of far ground copper need not be resolved
        if abs(v - lo) < 1e-9 or abs(v - hi) < 1e-9:
            return dmax_mm * fine if fine >= 1 else dmax_mm
        dist = min([max(0.0, a - v, v - b) for a, b in sig] or [0.0])
        return max(d_x, min(dmax_mm, far_frac * dist))

    def build(vals: set[float], lo: float, hi: float, d0, is_x: bool) -> np.ndarray:
        crit = sorted(round(v, 7) for v in vals)
        keep = [crit[0]]
        for v in crit[1:]:
            if v - keep[-1] > 1e-6:
                keep.append(v)
        if is_x:
            dc = [d_of_x(v, lo, hi) for v in keep]
        else:
            dc = [dmax_mm * fine if (abs(v - lo) < 1e-9 or abs(v - hi) < 1e-9) else d0 for v in keep]
        return axis_mesh(keep, dc, ratio, dmax_mm * fine if fine >= 1 else dmax_mm)

    x = build(xs, sec.xmin, sec.xmax, d_x, True)
    z = build(zs, sec.zmin, sec.zmax, d_z, False)
    return x, z


def solve(sec: Section, fine: float = 1.0, ratio: float = 1.28, dmax_mm: float = 0.6, mesh: tuple[np.ndarray, np.ndarray] | None = None) -> Solution:
    """Capacitance matrices (with dielectrics and with air) of ``sec`` per metre."""
    import time

    t0 = time.perf_counter()
    x, z = mesh if mesh is not None else build_mesh(sec, fine, ratio, dmax_mm)
    nx, nz = len(x), len(z)
    dx, dz = np.diff(x), np.diff(z)
    # cell permittivity from the slab holding the cell centre
    zc = 0.5 * (z[:-1] + z[1:])
    edges = np.array([s[1] for s in sec.slabs[:-1]])
    slab_eps = np.array([s[2] for s in sec.slabs])
    eps_z = slab_eps[np.searchsorted(edges, zc)]
    eps = np.broadcast_to(eps_z[None, :], (nx - 1, nz - 1)).copy()
    # nodes: conductor id per node (-1 free), box boundary is ground
    cond = np.full((nx, nz), -1, dtype=np.int32)
    cond[0, :] = cond[-1, :] = cond[:, 0] = cond[:, -1] = 0
    tol = 1e-7
    ordered = [r for r in sec.rects if r.cond == 0] + sorted((r for r in sec.rects if r.cond != 0), key=lambda r: -r.cond)
    for pass_signal in (False, True):
        for r in ordered:
            if (r.cond != 0) != pass_signal:
                continue
            i0 = np.searchsorted(x, r.x0 - tol)
            i1 = np.searchsorted(x, r.x1 + tol, side="right")
            j0 = np.searchsorted(z, r.z0 - tol)
            j1 = np.searchsorted(z, r.z1 + tol, side="right")
            cond[i0:i1, j0:j1] = r.cond
    flat = cond.ravel()
    n_sig = sec.n_cond
    fixed = flat >= 0
    free = ~fixed
    cs: list[np.ndarray] = []
    for e in (eps, np.ones_like(eps)):
        k = _assemble(dx, dz, e)
        phi = np.zeros((nx * nz, n_sig))
        for c in range(1, n_sig + 1):
            phi[flat == c, c - 1] = 1.0
        if free.any():
            fi = np.flatnonzero(free)
            di = np.flatnonzero(fixed)
            kff = k[fi][:, fi].tocsc()
            kfd = k[fi][:, di]
            rhs = -(kfd @ phi[di])
            lu = spla.splu(kff, permc_spec="MMD_AT_PLUS_A", options={"SymmetricMode": True})
            phi[fi] = lu.solve(np.asarray(rhs))
        cs.append(EPS0 * (phi.T @ (k @ phi)))
    c, c_air = cs
    c = 0.5 * (c + c.T)
    c_air = 0.5 * (c_air + c_air.T)
    return Solution(c=c, c_air=c_air, nodes=nx * nz, nx=nx, nz=nz, solve_s=time.perf_counter() - t0)


def mirror(sec: Section) -> Section:
    """The section reflected left to right."""
    return Section(
        [Rect(-r.x1, -r.x0, r.z0, r.z1, r.cond) for r in sec.rects],
        list(sec.slabs),
        -sec.xmax,
        -sec.xmin,
        sec.zmin,
        sec.zmax,
        sec.n_cond,
    )


# ------------------------------------------------------------------------------------------ canonical sections


def microstrip_section(w: float, h: float, t: float, er: float, box: float = 40.0, side: float = 40.0) -> Section:
    """Strip of width ``w`` and thickness ``t`` on a substrate of height ``h`` over a ground plane (all mm)."""
    gnd_t = max(t, 0.035)
    rects = [Rect(-side, side, -gnd_t, 0.0, 0), Rect(-w / 2, w / 2, h, h + t, 1)]
    slabs = [(-gnd_t - box, -gnd_t, 1.0), (-gnd_t, 0.0, er), (0.0, h, er), (h, h + t + box, 1.0)]
    return Section(rects, _merge_slabs(slabs), -side, side, -gnd_t - box, h + t + box, 1)


def coupled_microstrip_section(w: float, s: float, h: float, t: float, er: float, box: float = 40.0, side: float = 40.0) -> Section:
    gnd_t = max(t, 0.035)
    rects = [
        Rect(-side, side, -gnd_t, 0.0, 0),
        Rect(-s / 2 - w, -s / 2, h, h + t, 1),
        Rect(s / 2, s / 2 + w, h, h + t, 2),
    ]
    slabs = [(-gnd_t - box, -gnd_t, 1.0), (-gnd_t, 0.0, er), (0.0, h, er), (h, h + t + box, 1.0)]
    return Section(rects, _merge_slabs(slabs), -side, side, -gnd_t - box, h + t + box, 2)


def stripline_section(w: float, b: float, t: float, er: float, side: float = 30.0, plane_t: float = 0.035) -> Section:
    """Strip centred between two planes ``b`` apart (inner faces), in a uniform dielectric, shielded by the planes and side walls."""
    z_mid = b / 2
    rects = [
        Rect(-side, side, -plane_t, 0.0, 0),
        Rect(-side, side, b, b + plane_t, 0),
        Rect(-w / 2, w / 2, z_mid - t / 2, z_mid + t / 2, 1),
    ]
    slabs = [(-plane_t, 0.0, er), (0.0, b, er), (b, b + plane_t, er)]
    return Section(rects, slabs, -side, side, -plane_t, b + plane_t, 1)


def coupled_stripline_section(w: float, s: float, b: float, t: float, er: float, side: float = 30.0, plane_t: float = 0.035) -> Section:
    z_mid = b / 2
    rects = [
        Rect(-side, side, -plane_t, 0.0, 0),
        Rect(-side, side, b, b + plane_t, 0),
        Rect(-s / 2 - w, -s / 2, z_mid - t / 2, z_mid + t / 2, 1),
        Rect(s / 2, s / 2 + w, z_mid - t / 2, z_mid + t / 2, 2),
    ]
    slabs = [(-plane_t, 0.0, er), (0.0, b, er), (b, b + plane_t, er)]
    return Section(rects, slabs, -side, side, -plane_t, b + plane_t, 2)


def _merge_slabs(slabs: list[tuple[float, float, float]]) -> list[tuple[float, float, float]]:
    out: list[tuple[float, float, float]] = []
    for s in sorted(slabs):
        if s[1] - s[0] < 1e-12:
            continue
        if out and abs(out[-1][2] - s[2]) < 1e-12:
            out[-1] = (out[-1][0], s[1], s[2])
        else:
            out.append(s)
    return out


# ------------------------------------------------------------------------------------------ closed forms


def _z01(u: float) -> float:
    f = 6.0 + (2 * math.pi - 6.0) * math.exp(-((30.666 / u) ** 0.7528))
    return ETA0 / (2 * math.pi) * math.log(f / u + math.sqrt(1 + (2 / u) ** 2))


def _eeff_u(u: float, er: float) -> float:
    a = 1 + math.log((u**4 + (u / 52) ** 2) / (u**4 + 0.432)) / 49 + math.log(1 + (u / 18.1) ** 3) / 18.7
    b = 0.564 * ((er - 0.9) / (er + 3)) ** 0.053
    return (er + 1) / 2 + (er - 1) / 2 * (1 + 10 / u) ** (-a * b)


def hammerstad_jensen(w: float, h: float, t: float, er: float) -> tuple[float, float]:
    """(Z0 ohm, effective permittivity) of a microstrip: Hammerstad and Jensen 1980 with the thickness correction."""
    u = w / h
    if t > 0:
        x = t / h
        coth2 = 1.0 / math.tanh(math.sqrt(6.517 * u)) ** 2
        du1 = x / math.pi * math.log(1 + 4 * math.e / (x * coth2))
        dur = 0.5 * (1 + 1 / math.cosh(math.sqrt(max(er - 1, 0)))) * du1
        u = u + dur
    ee = _eeff_u(u, er)
    return _z01(u) / math.sqrt(ee), ee


def ipc2141_microstrip(w: float, h: float, t: float, er: float) -> float:
    """IPC-2141 surface microstrip: 87 / sqrt(er + 1.41) * ln(5.98 h / (0.8 w + t)). Stated for 0.1 < w/h < 2.0 and er < 15."""
    return 87.0 / math.sqrt(er + 1.41) * math.log(5.98 * h / (0.8 * w + t))


def ipc2141_stripline(w: float, b: float, t: float, er: float) -> float:
    """IPC-2141 symmetric stripline: 60 / sqrt(er) * ln(1.9 b / (0.8 w + t)), ``b`` the plane to plane spacing. Stated for w/(b-t) < 0.35 and t/b < 0.25."""
    return 60.0 / math.sqrt(er) * math.log(1.9 * b / (0.8 * w + t))


def cohn_stripline(w: float, b: float, er: float) -> float:
    """Exact Z0 of a zero-thickness strip centred between wide planes ``b`` apart (Cohn 1954, conformal mapping)."""
    k = 1.0 / math.cosh(math.pi * w / (2 * b))
    kp = math.sqrt(1 - k * k)
    return 30.0 * math.pi / math.sqrt(er) * ellipk(k**2) / ellipk(kp**2)


def cohn_coupled_stripline(w: float, s: float, b: float, er: float) -> tuple[float, float]:
    """Exact (Z0e, Z0o) of two zero-thickness strips centred between wide planes (Cohn 1955)."""
    th = math.tanh(math.pi * w / (2 * b))
    ke = th * math.tanh(math.pi * (w + s) / (2 * b))
    ko = th / math.tanh(math.pi * (w + s) / (2 * b))
    z = []
    for k in (ke, ko):
        kp = math.sqrt(1 - k * k)
        z.append(30.0 * math.pi / math.sqrt(er) * ellipk(kp**2) / ellipk(k**2))
    return z[0], z[1]


def kirschning_jansen_static(w: float, s: float, h: float, er: float) -> dict[str, float]:
    """Static even and odd mode effective permittivity, and the even mode impedance, of zero-thickness edge-coupled microstrip.

    Effective permittivities from Kirschning and Jansen 1984; the even mode impedance from the Hammerstad
    and Jensen 1980 coupled-line expressions. Stated ranges: 0.1 <= w/h <= 10, 0.1 <= s/h <= 10, 1 <= er <= 18.
    The odd mode impedance is not given here: the published expression could not be reproduced reliably, so
    the odd mode is checked through its effective permittivity and, for striplines, Cohn's exact result.
    """
    u, g = w / h, s / h
    z0u, ee_u = hammerstad_jensen(w, h, 0.0, er)
    v = u * (20 + g * g) / (10 + g * g) + g * math.exp(-g)
    ae = 1 + math.log((v**4 + (v / 52) ** 2) / (v**4 + 0.432)) / 49 + math.log(1 + (v / 18.1) ** 3) / 18.7
    be = 0.564 * ((er - 0.9) / (er + 3)) ** 0.053
    ee_e = (er + 1) / 2 + (er - 1) / 2 * (1 + 10 / v) ** (-ae * be)
    ao = 0.7287 * (ee_u - (er + 1) / 2) * (1 - math.exp(-0.179 * u))
    bo = 0.747 * er / (0.15 + er)
    co = bo - (bo - 0.207) * math.exp(-0.414 * u)
    do = 0.593 + 0.694 * math.exp(-0.562 * u)
    ee_o = ((er + 1) / 2 + ao - ee_u) * math.exp(-co * g**do) + ee_u
    q1 = 0.8695 * u**0.194
    q2 = 1 + 0.7519 * g + 0.189 * g**2.31
    q3 = 0.1975 + (16.6 + (8.4 / g) ** 6) ** (-0.387) + math.log(g**10 / (1 + (g / 3.4) ** 10)) / 241
    q4 = 2 * q1 / q2 / (math.exp(-g) * u**q3 + (2 - math.exp(-g)) * u ** (-q3))
    z0e = z0u * math.sqrt(ee_u / ee_e) / (1 - z0u / ETA0 * math.sqrt(ee_u) * q4)
    return {"eps_e": ee_e, "eps_o": ee_o, "z0e": z0e, "z0_single": z0u, "eps_single": ee_u}


def valid_to_hz(h_mm: float, er: float, z0: float) -> float:
    """Upper frequency up to which a quasi-static microstrip model is trusted here.

    The lower of one tenth of the dispersion corner ``f_p = Z0 / (2 mu0 h)`` (Getsinger; the effective
    permittivity has moved by a few percent by then) and the frequency at which the wavelength in the
    substrate is 20 substrate heights.
    """
    h = h_mm * 1e-3
    f_p = z0 / (2 * MU0 * h)
    f_wl = C_LIGHT / (20.0 * h * math.sqrt(er))
    return min(f_p / 10.0, f_wl)


# ------------------------------------------------------------------------------------------ crosstalk


def crosstalk(cm: float, c_self: float, lm: float, l_self: float, delay_s: float, rise_s: float) -> dict[str, float]:
    """Weak-coupling near-end and far-end crosstalk of one coupled stretch, as fractions of the aggressor swing.

    ``cm``, ``lm`` are the mutual capacitance (positive) and inductance per metre, ``c_self``, ``l_self`` the
    victim's own, ``delay_s`` the one-way delay of the stretch and ``rise_s`` the 10 to 90 percent rise time.
    Backward: Kb = (Cm/C + Lm/L) / 4, flat when 2*delay >= rise. Forward: Kf = (Cm/C - Lm/L) / 2 times
    delay / rise, so zero for a homogeneous medium. Matched terminations are assumed.
    """
    kb = 0.25 * (cm / c_self + lm / l_self)
    kf = 0.5 * (cm / c_self - lm / l_self)
    sat = min(1.0, 2.0 * delay_s / rise_s)
    return {"kb": kb, "near_end": kb * sat, "far_end": kf * delay_s / rise_s, "saturated": float(2.0 * delay_s >= rise_s)}


# ------------------------------------------------------------------------------------------ TDR


@dataclass
class TdrResult:
    t_s: np.ndarray
    v_source: np.ndarray
    v_load: np.ndarray
    z_apparent: np.ndarray
    tau_s: float
    z_line: np.ndarray = field(default_factory=lambda: np.zeros(0))


def resample_by_delay(z: np.ndarray, delay_s: np.ndarray, n: int) -> tuple[np.ndarray, float]:
    """A profile given as (impedance, one-way delay) per piece, resampled to ``n`` pieces of equal delay."""
    total = float(delay_s.sum())
    edges = np.concatenate([[0.0], np.cumsum(delay_s)])
    tau = total / n
    mids = (np.arange(n) + 0.5) * tau
    idx = np.clip(np.searchsorted(edges, mids, side="right") - 1, 0, len(z) - 1)
    return z[idx], tau


def tdr_response(
    z_line: np.ndarray,
    tau_s: float,
    rise_s: float,
    r_source: float = 50.0,
    r_load: float | None = None,
    v_step: float = 1.0,
    t_end_s: float | None = None,
) -> TdrResult:
    """Lossless cascaded-line step response. ``z_line[k]`` is the impedance of section ``k``, each with one-way delay ``tau_s``.

    A source ``v_step`` ramping over ``rise_s / 0.8`` (10 to 90 percent ``rise_s``) with series resistance
    ``r_source`` drives the first section; the last section ends in ``r_load`` (``None`` is open). Returns the
    voltage at the source and at the load against time, and the apparent impedance ``Rs * v / (V - v)`` seen at
    the source node. The scattering lattice is exact for ``tau_s`` steps.
    """
    z = np.asarray(z_line, dtype=float)
    n = len(z)
    total = n * tau_s
    if t_end_s is None:
        t_end_s = 2 * total * 3 + 3 * rise_s
    steps = int(math.ceil(t_end_s / tau_s)) + 1
    rho = (z[1:] - z[:-1]) / (z[1:] + z[:-1])  # junction j = 1..n-1
    rho_s = (r_source - z[0]) / (r_source + z[0])
    rho_l = 1.0 if r_load is None else (r_load - z[-1]) / (r_load + z[-1])
    ramp = rise_s / 0.8
    fin = np.zeros(n + 1)  # forward wave arriving at junction j from the left (j >= 1)
    bin_ = np.zeros(n + 1)  # backward wave arriving at junction j from the right (j <= n-1)
    v_src = np.empty(steps)
    v_ld = np.empty(steps)
    divider = z[0] / (r_source + z[0])
    for k in range(steps):
        t = k * tau_s
        vs = v_step if ramp <= 0 or t >= ramp else v_step * t / ramp
        fout = np.empty(n + 1)
        bout = np.empty(n + 1)
        fout[0] = vs * divider + rho_s * bin_[0]
        fout[1:n] = (1 + rho) * fin[1:n] - rho * bin_[1:n]
        bout[1:n] = rho * fin[1:n] + (1 - rho) * bin_[1:n]
        bout[n] = rho_l * fin[n]
        v_src[k] = fout[0] + bin_[0]
        v_ld[k] = fin[n] + bout[n]
        fin[1:] = fout[:n]
        bin_[:n] = bout[1:]
        fin[0] = 0.0
        bin_[n] = 0.0
    t = np.arange(steps) * tau_s
    vs_t = v_step * np.clip(t / ramp, 0.0, 1.0) if ramp > 0 else np.full(steps, v_step)
    # apparent impedance Rs * v / (Vs - v) with the instantaneous source voltage; undefined while the source is still near zero
    # or once the node has reached the source voltage (an open end settling)
    with np.errstate(divide="ignore", invalid="ignore"):
        zap = r_source * v_src / (vs_t - v_src)
    zap = np.where((vs_t > 0.02 * v_step) & ((vs_t - v_src) > 1e-3 * v_step), zap, np.nan)
    return TdrResult(t, v_src, v_ld, zap, tau_s, z)
