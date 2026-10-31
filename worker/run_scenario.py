#!/usr/bin/env python3
"""TrustPCB RF worker: run one openEMS scenario and write processed results.

    python3 run_scenario.py /job

Reads /job/scenario.json, writes progress.jsonl while the solver runs, then
writes s11.json, zin.json, pattern.json, field_xy.bin, field_xz.bin,
field.json, pulse.bin, pulse.json, current.bin, current.json and result.json
into the same directory. Raw solver files (the _sim directory) are deleted.
This file uses no application imports.

Binary files are little-endian Float32, C order. See the sidecar JSON files
for shapes, plane positions (scenario millimetres) and value scales.
"""
from __future__ import annotations

import json
import math
import os
import pathlib
import re
import shutil
import sys
import threading
import time
import traceback

import numpy as np

MAX_OUTPUT_BYTES = 30 * 1024 * 1024
C0 = 299792458.0
EPS0 = 8.854187817e-12


# ----------------------------------------------------------------- utilities
class Progress:
    """Append-only progress.jsonl writer."""

    def __init__(self, path: pathlib.Path):
        self.path = path
        self.t0 = time.time()
        self.lock = threading.Lock()
        path.write_text("")

    def emit(self, **kw):
        kw = {"t": round(time.time() - self.t0, 2), **kw}
        with self.lock, open(self.path, "a") as f:
            f.write(json.dumps(kw) + "\n")


def jdump(path: pathlib.Path, obj) -> None:
    path.write_text(json.dumps(obj, separators=(",", ":"), allow_nan=False))


def clean(a, nd=7):
    """List of finite python floats."""
    a = np.nan_to_num(np.asarray(a, dtype=float), nan=0.0, posinf=0.0, neginf=0.0)
    return [round(float(v), nd) for v in a]


def write_f32(path: pathlib.Path, arr: np.ndarray) -> None:
    np.ascontiguousarray(arr, dtype="<f4").tofile(path)


def nearest(lines, v):
    lines = np.asarray(lines, float)
    return float(lines[np.argmin(np.abs(lines - v))])


# -------------------------------------------------------------- solver output
class SolverLog:
    """Redirect the process stdout/stderr (C++ solver included) into a pipe,
    parse openEMS progress lines and keep the full text in solver.log."""

    TS = re.compile(r"Timestep:\s*(\d+)\s*\|\|")
    EN = re.compile(r"Energy:\s*~?\s*([0-9.eE+-]+)\s*\(\s*(-?)\s*([0-9.]+|inf)\s*dB\)")
    DONE = re.compile(r"end-criteria of\s*(-?[0-9.]+)\s*dB reached after\s*(\d+)\s*timesteps\s*\(\s*(-?)\s*([0-9.]+)\s*dB\)")
    SP = re.compile(r"Speed:\s*([0-9.]+)\s*MC/s")
    AT = re.compile(r"\[@\s*([0-9:.hmsd ]+)\]")

    def __init__(self, progress: Progress, logpath: pathlib.Path):
        self.progress = progress
        self.logpath = logpath
        self.last_energy_db = None
        self.last_ts = 0
        self.max_ts = None
        self.timestep_s = None
        self.nyquist = None
        self.cells = None
        self.reached_end = False
        self._last_emit = 0.0

    def __enter__(self):
        sys.stdout.flush()
        sys.stderr.flush()
        self.saved = (os.dup(1), os.dup(2))
        r, w = os.pipe()
        os.dup2(w, 1)
        os.dup2(w, 2)
        os.close(w)
        self.rfd = r
        self.th = threading.Thread(target=self._pump, daemon=True)
        self.th.start()
        return self

    def __exit__(self, *exc):
        try:
            sys.stdout.flush()
        except Exception:
            pass
        os.dup2(self.saved[0], 1)
        os.dup2(self.saved[1], 2)
        os.close(self.saved[0])
        os.close(self.saved[1])
        self.th.join(timeout=10)
        return False

    def _pump(self):
        buf = b""
        with open(self.rfd, "rb", closefd=True) as src, open(self.logpath, "wb") as log:
            while True:
                chunk = src.read1(4096) if hasattr(src, "read1") else src.read(4096)
                if not chunk:
                    break
                log.write(chunk)
                log.flush()
                buf += chunk
                parts = re.split(rb"[\r\n]", buf)
                buf = parts.pop()
                for p in parts:
                    self._line(p.decode("utf8", "replace"))
            if buf:
                self._line(buf.decode("utf8", "replace"))

    def _line(self, s: str):
        m = self.TS.search(s)
        if m:
            self.last_ts = int(m.group(1))
            e = self.EN.search(s)
            sp = self.SP.search(s)
            if e:
                try:
                    self.last_energy_db = float(e.group(2) + e.group(3))
                except ValueError:
                    pass
            now = time.time()
            if now - self._last_emit >= 0.5:
                self._last_emit = now
                self.progress.emit(
                    phase="solve", timestep=self.last_ts, max_timesteps=self.max_ts,
                    energy_db=self.last_energy_db,
                    speed_mcs=float(sp.group(1)) if sp else None,
                )
            return
        m = self.DONE.search(s)
        if m:
            self.last_ts = int(m.group(2))
            self.last_energy_db = float(m.group(3) + m.group(4))
            self.reached_end = True
            return
        m = re.search(r"Max\. number of timesteps:\s*(\d+)", s)
        if m:
            self.max_ts = int(m.group(1))
        m = re.search(r"FDTD timestep is:\s*([0-9.eE+-]+)\s*s;\s*Nyquist rate:\s*(\d+)", s)
        if m:
            self.timestep_s = float(m.group(1))
            self.nyquist = int(m.group(2))
        m = re.search(r"Number of cells.*?:\s*(\d+)", s)
        if m:
            self.cells = int(m.group(1))


# ------------------------------------------------------------------ geometry
def point_in_triangles(px, py, tris):
    """Boolean mask of grid points inside any triangle. px, py: 1D arrays (full grid via meshgrid)."""
    X, Y = np.meshgrid(px, py)
    inside = np.zeros(X.shape, bool)
    for t in tris:
        (x0, y0), (x1, y1), (x2, y2) = t
        xmin, xmax = min(x0, x1, x2), max(x0, x1, x2)
        ymin, ymax = min(y0, y1, y2), max(y0, y1, y2)
        ix = np.nonzero((px >= xmin - 1e-9) & (px <= xmax + 1e-9))[0]
        iy = np.nonzero((py >= ymin - 1e-9) & (py <= ymax + 1e-9))[0]
        if not len(ix) or not len(iy):
            continue
        sx, sy = slice(ix[0], ix[-1] + 1), slice(iy[0], iy[-1] + 1)
        xx, yy = X[sy, sx], Y[sy, sx]
        d = (y1 - y2) * (x0 - x2) + (x2 - x1) * (y0 - y2)
        if abs(d) < 1e-15:
            continue
        a = ((y1 - y2) * (xx - x2) + (x2 - x1) * (yy - y2)) / d
        b = ((y2 - y0) * (xx - x2) + (x0 - x2) * (yy - y2)) / d
        c = 1 - a - b
        eps = -1e-9
        inside[sy, sx] |= (a >= eps) & (b >= eps) & (c >= eps)
    return inside


def point_on_metal(sc, x, y, z):
    for m in sc["metals"]:
        if abs(m["z_mm"] - z) < 1e-6 and point_in_triangles(np.array([x]), np.array([y]), m["triangles"]).any():
            return True
    for v in sc.get("vias", []):
        z0, z1 = sorted((v["z0_mm"], v["z1_mm"]))
        r = v["diameter_mm"] / 2
        if z0 - 1e-6 <= z <= z1 + 1e-6 and (x - v["x"]) ** 2 + (y - v["y"]) ** 2 <= (r + 1e-6) ** 2:
            return True
    return False


def port_axis(p, a, b) -> int:
    """Excitation axis of the lumped port: the explicit ``port.axis`` ("x", "y" or "z") when given, else the longest side of
    the port box (older scenario files)."""
    named = p.get("axis")
    if named is not None:
        if str(named).lower() not in ("x", "y", "z"):
            raise ValueError(f"port.axis must be x, y or z, got {named!r}")
        return "xyz".index(str(named).lower())
    return int(np.argmax(np.abs(np.asarray(b, float) - np.asarray(a, float))))


def build_model(sc, sim_dir):
    from CSXCAD import ContinuousStructure
    from openEMS import openEMS

    unit = 1e-3 * float(sc.get("unit_mm", 1))
    f0, fc = float(sc["f0_hz"]), float(sc["fc_hz"])
    ec = sc.get("end_criteria", -40.0)
    ec_lin = 10 ** (ec / 10.0) if ec < 0 else float(ec)
    fd = openEMS(NrTS=int(sc.get("max_timesteps", 60000)), EndCriteria=ec_lin)
    fd.SetGaussExcite(f0, fc)
    bc = sc.get("boundary", "MUR")
    fd.SetBoundaryCond([bc] * 6)
    csx = ContinuousStructure()
    fd.SetCSX(csx)
    grid = csx.GetGrid()
    grid.SetDeltaUnit(unit)
    for ax in "xyz":
        grid.AddLine(ax, list(sc["mesh"][ax]))

    # dielectrics
    for i, s in enumerate(sc["substrates"]):
        er, td = float(s["epsilon_r"]), float(s.get("loss_tangent", 0.0))
        kappa = 2 * math.pi * f0 * EPS0 * er * td
        mat = csx.AddMaterial(s.get("name", f"sub{i}"), epsilon=er, kappa=kappa)
        x0, y0, x1, y1 = s["box"]
        mat.AddBox([x0, y0, s["zmin"]], [x1, y1, s["zmax"]], priority=0)
    for i, m in enumerate(sc.get("materials", [])):
        er = float(m.get("epsilon_r", 1.0))
        kappa = float(m.get("kappa", 2 * math.pi * f0 * EPS0 * er * float(m.get("loss_tangent", 0.0))))
        mat = csx.AddMaterial(m.get("name", f"mat{i}"), epsilon=er, kappa=kappa)
        b = m["box"]
        mat.AddBox(b[:3], b[3:], priority=1)

    # copper: zero-thickness polygons (triangles), vias as solid cylinders
    metal_props = {}
    for m in sc["metals"]:
        prop = metal_props[m["name"]] = csx.AddMetal(m["name"])
        z = float(m["z_mm"])
        for t in m["triangles"]:
            xs = [p[0] for p in t]
            ys = [p[1] for p in t]
            prop.AddPolygon([xs, ys], "z", z, priority=10)
    if sc.get("vias"):
        vprop = csx.AddMetal("vias")
        for v in sc["vias"]:
            z0, z1 = sorted((v["z0_mm"], v["z1_mm"]))
            vprop.AddCylinder([v["x"], v["y"], z0], [v["x"], v["y"], z1], v["diameter_mm"] / 2, priority=10)

    if sc.get("edges2grid"):  # optional: let CSXCAD add its 1/3-2/3 edge lines around copper
        for m in sc["metals"]:
            fd.AddEdges2Grid("xy", metal_props[m["name"]])

    # lumped port
    p = sc["port"]
    a, b = np.array(p["start"], float), np.array(p["end"], float)
    axis = port_axis(p, a, b)
    port = fd.AddLumpedPort(1, float(p.get("impedance_ohm", 50.0)), list(a), list(b), "xyz"[axis], 1.0, priority=5)

    # dumps ---------------------------------------------------------------
    gx, gy, gz = (np.asarray(sc["mesh"][k], float) for k in "xyz")
    dmp = sc.get("dumps", {})
    ext = dict(x=(gx[0], gx[-1]), y=(gy[0], gy[-1]), z=(gz[0], gz[-1]))
    meta = {"dumps": {}, "ext": ext}

    def box(name, dump_type, lo, hi, mode=2, freq=None, over=None, sub=None):
        d = csx.AddDump(name, dump_type=dump_type, dump_mode=mode, file_type=1,
                        **({"frequency": np.array([freq])} if freq else {}),
                        **({"sub_sampling": sub} if sub else {}),
                        **({"over_sampling": over} if over else {}))
        d.AddBox(list(lo), list(hi))
        return d

    # frequency-domain E on an xy plane and an xz plane
    fxy = dmp.get("field_xy", {})
    zxy = nearest(gz, float(fxy.get("z_mm", gz[-1] / 2)))
    box("E_xy", 10, [gx[0], gy[0], zxy], [gx[-1], gy[-1], zxy], freq=f0)
    meta["dumps"]["E_xy"] = {"z_mm": zxy}
    fxz = dmp.get("field_xz", {})
    yxz = nearest(gy, float(fxz.get("y_mm", 0.0)))
    box("E_xz", 10, [gx[0], yxz, gz[0]], [gx[-1], yxz, gz[-1]], freq=f0)
    meta["dumps"]["E_xz"] = {"y_mm": yxz}

    # surface current: H just above and just below the chosen copper layer
    cur = dmp.get("current", {})
    metals = sc["metals"]
    cm = next((m for m in metals if m["name"] == cur.get("metal")), None) or max(metals, key=lambda m: m["z_mm"])
    zc = float(cm["z_mm"])
    above = gz[gz > zc + 1e-6]
    below = gz[gz < zc - 1e-6]
    if not len(above) or not len(below):
        raise ValueError("current plane needs mesh lines above and below the copper layer")
    z_hi, z_lo = float(above[0]), float(below[-1])
    box("H_hi", 11, [gx[0], gy[0], z_hi], [gx[-1], gy[-1], z_hi], mode=1, freq=f0)
    box("H_lo", 11, [gx[0], gy[0], z_lo], [gx[-1], gy[-1], z_lo], mode=1, freq=f0)
    meta["dumps"]["current"] = {"metal": cm["name"], "z_mm": zc, "z_hi": z_hi, "z_lo": z_lo, "triangles": cm["triangles"]}

    # pulse: time-domain E on an xy plane, windowed and oversampled
    pul = dmp.get("pulse", {})
    zp = nearest(gz, float(pul.get("z_mm", zxy)))
    diag = math.hypot(ext["x"][1] - ext["x"][0], ext["y"][1] - ext["y"][0]) * unit
    exc_len = 2 * 9.0 / (2 * math.pi * fc)
    stop_t = exc_len + 1.2 * diag / C0
    t_nyq = 1.0 / (2.0 * (f0 + fc))
    dt_raw = max(stop_t / 240.0, 1e-13)
    over = max(1, int(math.ceil(t_nyq / dt_raw)))
    ss = max(1, int(min(len(gx), len(gy)) // 160))  # keep the raw time-domain dump small
    dpulse = box("E_t", 0, [gx[0], gy[0], zp], [gx[-1], gy[-1], zp], over=over,
                 sub=pul.get("sub_sampling", [ss, ss, 1]))
    meta["dumps"]["pulse"] = {"z_mm": zp, "stop_t_s": stop_t, "over_sampling": over,
                              "max_frames": int(pul.get("max_frames", 80))}

    nf = fd.CreateNF2FFBox()
    return fd, csx, port, nf, meta


# ----------------------------------------------------------------- HDF5 field
def read_dump(path, td=False, bounds=None):
    """Read an openEMS HDF5 dump.

    Returns (x_mm, y_mm, z_mm, frames); frames is a list of (label, array) with
    array shaped [3, nz, ny, nx] (complex for frequency-domain dumps). The file
    stores the mesh in metres and the data as [component, x, y, z]. Cell
    (dual-mesh) dumps carry one extra trailing point per axis outside the
    domain; points beyond ``bounds`` ((lo, hi) per axis, mm) are dropped.
    """
    import h5py

    with h5py.File(path, "r") as h:
        axes = [np.array(h["Mesh"][k], dtype=float) * 1e3 for k in "xyz"]
        keep = []
        for ax, bd in zip(axes, bounds or [None] * 3):
            k = np.ones(len(ax), bool)
            if bd is not None and len(ax) > 1:
                k = (ax >= bd[0] - 1e-6) & (ax <= bd[1] + 1e-6)
            keep.append(k)
        frames = []
        if td:
            grp = h["FieldData/TD"]
            for name in sorted(grp.keys()):
                d = grp[name]
                a = np.transpose(np.array(d), (0, 3, 2, 1))
                a = a[:, keep[2]][:, :, keep[1]][:, :, :, keep[0]]
                frames.append((float(np.ravel(d.attrs["time"])[0]), a))
        else:
            grp = h["FieldData/FD"]
            for name in sorted(grp.keys()):
                d = grp[name]
                a = np.transpose(np.array(d), (0, 3, 2, 1))
                a = a[:, keep[2]][:, :, keep[1]][:, :, :, keep[0]]
                frames.append((float(d.attrs.get("frequency", np.nan)), a))
    return axes[0][keep[0]], axes[1][keep[1]], axes[2][keep[2]], frames


def bnd(meta):
    e = meta["ext"]
    return [(e["x"][0], e["x"][1]), (e["y"][0], e["y"][1]), (e["z"][0], e["z"][1])]


def uniform_axes(lo0, lo1, hi0, hi1, max_px, native=0):
    """Uniform square-pixel axes covering the box, at most max_px along the long side
    and at most 2x the native sample count (no point upsampling a coarse mesh)."""
    if native:
        max_px = min(max_px, 2 * int(native))
    s = max(hi0 - lo0, hi1 - lo1) / max_px
    n0 = max(2, int(round((hi0 - lo0) / s)) + 1)
    n1 = max(2, int(round((hi1 - lo1) / s)) + 1)
    return np.linspace(lo0, hi0, n0), np.linspace(lo1, hi1, n1)


def resample(a, ax0, ax1, u0, u1):
    """a[ax1, ax0] on a rectilinear grid -> [len(u1), len(u0)] linear interpolation (real or complex)."""
    from scipy.interpolate import RegularGridInterpolator

    P1, P0 = np.meshgrid(u1, u0, indexing="ij")
    pts = np.stack([P1.ravel(), P0.ravel()], axis=-1)
    f = RegularGridInterpolator((ax1, ax0), a, bounds_error=False, fill_value=None)
    return f(pts).reshape(P1.shape)


# ----------------------------------------------------------------- processing
def process(sc, sim, out, fd, port, nf, meta, slog, prog, t_solve):
    f0 = float(sc["f0_hz"])
    lo, hi = sc["band_hz"]
    freqs = np.linspace(lo, hi, int(sc.get("n_freq", 401)))
    prog.emit(phase="post", step="ports")
    port.CalcPort(str(sim), freqs)
    s11 = port.uf_ref / port.uf_inc
    zin = port.uf_tot / port.if_tot
    s11_db = 20 * np.log10(np.maximum(np.abs(s11), 1e-12))
    p_acc = 0.5 * np.real(port.uf_tot * np.conj(port.if_tot))
    p_inc = 0.5 * np.abs(port.uf_inc) ** 2 / float(sc["port"].get("impedance_ohm", 50.0))
    i_res = int(np.argmin(s11_db))
    f_res = float(freqs[i_res])

    jdump(out / "s11.json", {"freq_hz": clean(freqs, 1), "s11_db": clean(s11_db, 4),
                              "s11_re": clean(s11.real, 6), "s11_im": clean(s11.imag, 6)})
    jdump(out / "zin.json", {"freq_hz": clean(freqs, 1), "z_re": clean(zin.real, 4),
                              "z_im": clean(zin.imag, 4),
                              "z0_ohm": float(sc["port"].get("impedance_ohm", 50.0))})

    # -10 dB bandwidth around the deepest point
    thr = -10.0
    bw = {"threshold_db": thr, "f_low_hz": None, "f_high_hz": None, "width_hz": 0.0}
    if s11_db[i_res] <= thr:
        a = i_res
        while a > 0 and s11_db[a - 1] <= thr:
            a -= 1
        b = i_res
        while b < len(freqs) - 1 and s11_db[b + 1] <= thr:
            b += 1
        bw.update(f_low_hz=float(freqs[a]), f_high_hz=float(freqs[b]), width_hz=float(freqs[b] - freqs[a]))

    # far field at the band centre f0, the frequency the engineer designs for (not at the deepest S11 dip, which can sit
    # far outside the band). Accepted power for the efficiency is taken at the same frequency. When f0 is outside the
    # sweep there is no accepted power to divide by there, so the deepest dip is used and f_hz says so.
    in_sweep = bool(freqs[0] <= f0 <= freqs[-1])
    f_pat = f0 if in_sweep else f_res
    p_acc_pat = float(np.interp(f_pat, freqs, p_acc))
    prog.emit(phase="post", step="nf2ff")
    t0 = time.time()
    th = np.arange(0, 181, 5.0)
    ph = np.arange(0, 361, 5.0)
    ctr = [v * 1e-3 * float(sc.get("unit_mm", 1)) for v in sc.get("pattern_center_mm", [0, 0, 0])]
    res = nf.CalcNF2FF(str(sim), f_pat, th, ph, center=ctr, verbose=0)
    nf_s = time.time() - t0
    prad = float(np.ravel(res.Prad)[0])
    dmax = float(np.ravel(res.Dmax)[0])
    D = 4 * math.pi * np.asarray(res.P_rad[0]) / prad
    D_dbi = 10 * np.log10(np.maximum(D, 1e-4))
    it, ip = np.unravel_index(int(np.argmax(D)), D.shape)
    eff = float(prad / p_acc_pat) if p_acc_pat > 0 else None
    jdump(out / "pattern.json", {
        "f_hz": f_pat, "theta_deg": th.tolist(), "phi_deg": ph.tolist(),
        "directivity_dbi": [clean(r, 3) for r in D_dbi],
        "layout": "directivity_dbi[theta_index][phi_index]",
        "dmax_dbi": round(10 * math.log10(dmax), 3), "dmax_theta_deg": float(th[it]),
        "dmax_phi_deg": float(ph[ip]), "floor_dbi": -40.0,
        "radiated_power_w_per_w_incident": None if eff is None else round(eff, 4),
        "efficiency_f_hz": f_pat, "f0_hz": f0, "f_res_hz": f_res,
        "note": "Computed at f_hz (the band centre f0 unless it lies outside the sweep). Theta from +z (up), phi from +x toward +y, in scenario axes."})

    # fields
    max_px = int(sc.get("dumps", {}).get("max_px", 256))
    prog.emit(phase="post", step="fields")
    write_fields(sc, sim, out, meta, max_px, f0)
    prog.emit(phase="post", step="current")
    write_current(sc, sim, out, meta, max_px, f0)
    prog.emit(phase="post", step="pulse")
    write_pulse(sc, sim, out, meta, max_px)

    # trust
    reasons = []
    ec = sc.get("end_criteria", -40.0)
    ec_db = ec if ec < 0 else 10 * math.log10(float(ec))
    decay = slog.last_energy_db
    if decay is None:
        reasons.append("solver energy was not reported")
    elif decay > ec_db + 0.5:
        reasons.append(f"energy only decayed to {decay:.1f} dB, target {ec_db:.0f} dB: run too short")
    s11_max_db = float(np.max(s11_db))
    passive = bool(s11_max_db <= 0.1)
    if not passive:
        reasons.append(f"S11 reaches {s11_max_db:.2f} dB (above 0 dB): bad setup")
    cpw = cells_per_wavelength(sc, hi)
    if cpw < 20.0:
        reasons.append(f"only {cpw:.1f} cells per wavelength inside the substrate (need 20)")
    mf = sc.get("min_feature_mm")
    if mf:
        local = max_cell_in_structure(sc)
        if mf / local < 2.0:
            reasons.append(f"narrowest copper feature {mf} mm is under 2 cells (cell {local:.2f} mm)")
    if not (point_on_metal(sc, *sc["port"]["start"]) or point_on_metal(sc, *sc["port"]["end"])):
        reasons.append("port is not on copper")
    elif not (point_on_metal(sc, *sc["port"]["start"]) and point_on_metal(sc, *sc["port"]["end"])):
        reasons.append("port has copper on only one end (no ground reference)")
    nx, ny, nz = (len(sc["mesh"][k]) for k in "xyz")
    cells = nx * ny * nz
    result = {
        "f_res": f_res, "s11_min_db": round(float(s11_db[i_res]), 3), "bandwidth": bw,
        "zin_at_res": {"re": round(float(zin[i_res].real), 3), "im": round(float(zin[i_res].imag), 3)},
        "dmax_dbi": round(10 * math.log10(dmax), 3),
        "efficiency_est": None if eff is None else round(eff, 4),
        "pattern_f_hz": f_pat,  # dmax_dbi and efficiency_est (radiated / accepted power) are both at this frequency, not at f_res
        "trust": {"energy_decay_db": decay, "passive": passive,
                  "cells_per_wavelength": round(cpw, 2),
                  "status": "ok" if not reasons else "fail", "reasons": reasons},
        "cells": cells, "timesteps": slog.last_ts, "solve_s": round(t_solve, 2),
        "nf2ff_s": round(nf_s, 2),
        "quality": sc.get("quality"), "f0_hz": f0,
        "note": "Simulated. Not a substitute for a real antenna measurement.",
    }
    return result


def structure_bbox(sc):
    xs, ys, zs = [], [], []
    for s in sc["substrates"]:
        x0, y0, x1, y1 = s["box"]
        xs += [x0, x1]; ys += [y0, y1]; zs += [s["zmin"], s["zmax"]]
    for m in sc["metals"]:
        for t in m["triangles"]:
            for p in t:
                xs.append(p[0]); ys.append(p[1])
        zs.append(m["z_mm"])
    return (min(xs), max(xs)), (min(ys), max(ys)), (min(zs), max(zs))


def max_cell_in_structure(sc):
    worst = 0.0
    for ax, (lo, hi) in zip("xyz", structure_bbox(sc)):
        g = np.asarray(sc["mesh"][ax], float)
        sel = g[(g >= lo - 1e-9) & (g <= hi + 1e-9)]
        if len(sel) > 1:
            worst = max(worst, float(np.max(np.diff(sel))))
    return worst or 1.0


def cells_per_wavelength(sc, fmax):
    er = max(float(s["epsilon_r"]) for s in sc["substrates"])
    lam_mm = C0 / (fmax * math.sqrt(er)) * 1e3 / float(sc.get("unit_mm", 1))
    # largest cell inside the dielectric volume
    worst = 0.0
    for s in sc["substrates"]:
        x0, y0, x1, y1 = s["box"]
        for ax, (lo, hi) in zip("xyz", ((x0, x1), (y0, y1), (s["zmin"], s["zmax"]))):
            g = np.asarray(sc["mesh"][ax], float)
            sel = g[(g >= lo - 1e-9) & (g <= hi + 1e-9)]
            if len(sel) > 1:
                worst = max(worst, float(np.max(np.diff(sel))))
    return lam_mm / (worst or 1.0)


def dominant_component(F, plane_axes_complex=True):
    """F [3, ...] -> index of the component with the most energy."""
    e = [float(np.sum(np.abs(F[c]) ** 2)) for c in range(3)]
    return int(np.argmax(e))


def write_fields(sc, sim, out, meta, max_px, f0):
    info = {"f_hz": f0, "normalization": "value / scale, scale = 99.5th percentile of |complex field|; "
            "arbitrary units (excitation amplitude is not calibrated)",
            "layout": "float32 little-endian, shape [2, rows, cols]: plane 0 = real, plane 1 = imag, "
                      "row-major; cols run along u axis (increasing), rows along v axis (increasing)"}
    # xy plane (z fixed): u = x, v = y
    x, y, z, fr = read_dump(sim / "E_xy.h5", bounds=bnd(meta))
    F = fr[0][1][:, 0, :, :]  # [3, ny, nx]
    c = dominant_component(F)
    u, v = uniform_axes(x[0], y[0], x[-1], y[-1], max_px, max(len(x), len(y)))
    A = resample(F[c], x, y, u, v)
    sc_val = float(np.percentile(np.abs(A), 99.5)) or 1.0
    arr = np.stack([A.real, A.imag]) / sc_val
    write_f32(out / "field_xy.bin", arr)
    info["xy"] = {"file": "field_xy.bin", "shape": list(arr.shape), "component": "xyz"[c],
                  "u_axis": "x", "v_axis": "y", "u_mm": [float(u[0]), float(u[-1])],
                  "v_mm": [float(v[0]), float(v[-1])], "plane_axis": "z",
                  "plane_mm": float(z[0]), "scale": sc_val}
    # xz plane (y fixed): u = x, v = z
    x, y, z, fr = read_dump(sim / "E_xz.h5", bounds=bnd(meta))
    F = fr[0][1][:, :, 0, :]  # [3, nz, nx]
    c = dominant_component(F)
    u, v = uniform_axes(x[0], z[0], x[-1], z[-1], max_px, max(len(x), len(z)))
    A = resample(F[c], x, z, u, v)
    sc_val = float(np.percentile(np.abs(A), 99.5)) or 1.0
    arr = np.stack([A.real, A.imag]) / sc_val
    write_f32(out / "field_xz.bin", arr)
    info["xz"] = {"file": "field_xz.bin", "shape": list(arr.shape), "component": "xyz"[c],
                  "u_axis": "x", "v_axis": "z", "u_mm": [float(u[0]), float(u[-1])],
                  "v_mm": [float(v[0]), float(v[-1])], "plane_axis": "y",
                  "plane_mm": float(y[0]), "scale": sc_val}
    jdump(out / "field.json", info)


def write_current(sc, sim, out, meta, max_px, f0):
    from scipy.ndimage import binary_dilation

    cm = meta["dumps"]["current"]
    x, y, z, fh = read_dump(sim / "H_hi.h5", bounds=bnd(meta))
    _, _, _, fl = read_dump(sim / "H_lo.h5", bounds=bnd(meta))
    Hh, Hl = fh[0][1][:, 0, :, :], fl[0][1][:, 0, :, :]
    dH = Hh - Hl
    # n x dH for n = +z: (-dHy, dHx); magnitude of the complex surface-current vector
    mag = np.sqrt(np.abs(dH[0]) ** 2 + np.abs(dH[1]) ** 2)
    u, v = uniform_axes(x[0], y[0], x[-1], y[-1], max_px, max(len(x), len(y)))
    A = resample(mag, x, y, u, v)
    mask = point_in_triangles(u, v, cm["triangles"])
    mask = binary_dilation(mask, iterations=1)
    A = np.where(mask, A, 0.0)
    nz = A[A > 0]
    scale = float(np.percentile(nz, 99.5)) if len(nz) else 1.0
    arr = A / scale
    write_f32(out / "current.bin", arr)
    jdump(out / "current.json", {
        "file": "current.bin", "shape": list(arr.shape), "layout": "float32 [rows(y), cols(x)], "
        "row-major, zero outside copper",
        "u_axis": "x", "v_axis": "y", "u_mm": [float(u[0]), float(u[-1])],
        "v_mm": [float(v[0]), float(v[-1])], "plane_mm": cm["z_mm"], "metal": cm["metal"],
        "f_hz": f0, "scale": scale,
        "normalization": "|Js| / scale, scale = 99.5th percentile over copper; values above 1 are edge peaks"})


def write_pulse(sc, sim, out, meta, max_px):
    pm = meta["dumps"]["pulse"]
    x, y, z, frames = read_dump(sim / "E_t.h5", td=True, bounds=bnd(meta))
    mags = []
    times = []
    for t, F in frames:
        if t > pm["stop_t_s"]:
            break
        F = F[:, 0, :, :]
        mags.append(np.sqrt(F[0] ** 2 + F[1] ** 2 + F[2] ** 2))
        times.append(t)
    mags = np.array(mags)
    peaks = mags.reshape(len(mags), -1).max(axis=1)
    gmax = float(peaks.max()) or 1.0
    live = np.nonzero(peaks > 0.01 * gmax)[0]
    first = int(live[0]) if len(live) else 0
    idx = np.arange(first, len(mags))
    nmax = pm["max_frames"]
    if len(idx) > nmax:
        idx = idx[np.unique(np.round(np.linspace(0, len(idx) - 1, nmax)).astype(int))]
    u, v = uniform_axes(x[0], y[0], x[-1], y[-1], min(max_px, 256), max(len(x), len(y)))
    stack = np.stack([resample(mags[i], x, y, u, v) for i in idx])
    stack = np.maximum(stack, 0.0)
    gmax = float(stack.max()) or 1.0
    stack = stack / gmax
    write_f32(out / "pulse.bin", stack)
    jdump(out / "pulse.json", {
        "file": "pulse.bin", "shape": list(stack.shape),
        "layout": "float32 [frames, rows(y), cols(x)], row-major, |E| vector magnitude",
        "u_axis": "x", "v_axis": "y", "u_mm": [float(u[0]), float(u[-1])],
        "v_mm": [float(v[0]), float(v[-1])], "plane_mm": float(z[0]),
        "times_s": [float(times[i]) for i in idx], "scale": gmax,
        "normalization": "|E| / scale, scale = global maximum over the stored frames; use a gamma near 0.5 to see the tail",
        "raw_frames": int(len(mags)), "first_raw_frame": first})


# ----------------------------------------------------------------------- main
def main(job: pathlib.Path) -> int:
    t_all = time.time()
    sc = json.loads((job / "scenario.json").read_text())
    prog = Progress(job / "progress.jsonl")
    prog.emit(phase="setup")
    sim = job / "_sim"
    post_only = os.environ.get("EMS_POST_ONLY") == "1"
    if sim.exists() and not post_only:
        shutil.rmtree(sim)
    sim.mkdir(exist_ok=True)
    out = job
    try:
        threads = int(sc.get("threads", 2))
        cap = int(os.environ.get("EMS_MAX_THREADS", "0") or 0)
        if cap:
            threads = min(threads, cap)
        fd, csx, port, nf, meta = build_model(sc, sim)
        slog = SolverLog(prog, job / "solver.log")
        t0 = time.time()
        if os.environ.get("EMS_POST_ONLY") == "1":  # development: reuse an earlier _sim
            for ln in re.split(r"[\r\n]", (job / "solver.log").read_text(errors="replace")):
                slog._line(ln)
            t_solve = float(os.environ.get("EMS_SOLVE_S", "0"))
        else:
            with slog:
                fd.Run(str(sim), verbose=3, cleanup=False, numThreads=threads)
            t_solve = time.time() - t0
        os.chdir(job)
        prog.emit(phase="solved", timestep=slog.last_ts, energy_db=slog.last_energy_db, solve_s=round(t_solve, 2))
        if not list(sim.glob("*.h5")):
            raise RuntimeError("solver produced no output files; see solver.log")
        result = process(sc, sim, out, fd, port, nf, meta, slog, prog, t_solve)
        result["total_s"] = round(time.time() - t_all, 2)
        jdump(out / "result.json", result)
        if os.environ.get("EMS_KEEP_RAW") != "1":
            shutil.rmtree(sim, ignore_errors=True)
            try:
                (job / "solver.log").unlink()
            except FileNotFoundError:
                pass
        size = sum(p.stat().st_size for p in job.iterdir() if p.is_file() and p.name != "scenario.json")
        if size > MAX_OUTPUT_BYTES:
            raise RuntimeError(f"processed output is {size/1e6:.1f} MB, over the 30 MB limit")
        prog.emit(phase="done", status=result["trust"]["status"], output_bytes=size)
        return 0
    except Exception as e:  # noqa: BLE001
        tb = traceback.format_exc()
        sys.stderr.write(tb)
        try:
            os.chdir(job)
        except Exception:
            pass
        prog.emit(phase="failed", error=f"{type(e).__name__}: {e}")
        jdump(job / "result.json", {"error": f"{type(e).__name__}: {e}", "trace": tb[-2000:],
                                    "trust": {"energy_decay_db": None, "passive": False,
                                              "cells_per_wavelength": None, "status": "fail",
                                              "reasons": [f"worker error: {e}"]}})
        if os.environ.get("EMS_KEEP_RAW") != "1":
            shutil.rmtree(sim, ignore_errors=True)
        return 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: run_scenario.py JOB_DIR")
    sys.exit(main(pathlib.Path(sys.argv[1]).resolve()))
