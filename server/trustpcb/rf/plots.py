"""PNG charts and images from a finished job directory: S11, Smith chart, radiation pattern, field and surface current.

Vector charts are drawn as SVG and rasterised with ``resvg-py`` (already a dependency for board views);
the field and current images are heat maps built with numpy and Pillow and embedded in the SVG, so one
renderer handles every image. No plotting package is needed.

Orientation. Field and current sidecars are in scenario millimetres, where scenario y is minus board y
(``meta.json`` carries the convention). The images here are drawn the way the board is drawn: board x to the
right, board y downward, so row 0 of the picture is the smallest board y. Axis labels are board millimetres.
"""

from __future__ import annotations

import base64
import io
import json
import math
from pathlib import Path
from typing import Any, Iterable

import numpy as np

FONT = "Inter, Helvetica, Arial, sans-serif"
INK, MUTED, GRID = "#1f2933", "#6b7785", "#e3e7ec"
BLUE, ORANGE, GREEN, RED = "#1d6fb8", "#d9822b", "#2f9e6e", "#c8372d"
NOTE = "Simulated. Not a substitute for a real antenna measurement."
# anchors of a perceptual, dark-to-bright colour ramp
_RAMP = [(0.0, (8, 6, 28)), (0.2, (60, 18, 100)), (0.4, (135, 35, 108)), (0.6, (205, 65, 80)), (0.8, (248, 140, 40)), (1.0, (252, 245, 160))]


class PlotError(ValueError):
    """The data needed for this image is not in the job directory."""


def _n(v: float) -> str:
    s = f"{v:.2f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def _text(x, y, s, size=13, fill=INK, anchor="start", weight=400, extra="") -> str:
    s = str(s).replace("&", "&amp;").replace("<", "&lt;")
    return f'<text x="{_n(x)}" y="{_n(y)}" font-family="{FONT}" font-size="{size}" fill="{fill}" text-anchor="{anchor}" font-weight="{weight}" {extra}>{s}</text>'


def to_png(svg: str, width: int) -> bytes:
    from ..pcb import render_svg

    return render_svg.render_png(svg, width, background="#ffffff")


def _read(job: Path, name: str) -> Any:
    p = Path(job) / name
    if not p.is_file():
        return None
    return json.loads(p.read_text())


def nice_ticks(lo: float, hi: float, target: int = 6) -> list[float]:
    span = hi - lo
    if span <= 0:
        return [lo]
    raw = span / max(target, 1)
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    first = math.ceil(lo / step - 1e-9) * step
    out, v = [], first
    while v <= hi + 1e-9:
        out.append(round(v, 10))
        v += step
    return out


def _svg(w: int, h: int, body: str) -> str:
    return f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="{w}" height="{h}" viewBox="0 0 {w} {h}"><rect width="{w}" height="{h}" fill="#ffffff"/>{body}</svg>'


# ------------------------------------------------------------------------------------------------ S11


def s11_png(curve: dict, *, band_hz: Iterable[float] | None = None, target_db: float | None = None, as_built: dict | None = None,
            dips: list[dict] | None = None, title: str = "S11 (antenna alone)", width: int = 1100) -> bytes:
    """Return loss against frequency. ``curve`` is ``{freq_hz[], s11_db[]}``; ``as_built`` the same with the matching network."""
    f = np.asarray(curve["freq_hz"], float) / 1e9
    y = np.asarray(curve["s11_db"], float)
    if f.size < 2:
        raise PlotError("the S11 curve has fewer than two points")
    W, H, L, R, T, B = 1100, 620, 78, 30, 60, 76
    series = [("antenna alone", f, y, BLUE)]
    if as_built and as_built.get("s11_db"):
        series.append(("as built (with the board's matching parts)", np.asarray(as_built.get("freq_hz", curve["freq_hz"]), float) / 1e9, np.asarray(as_built["s11_db"], float), ORANGE))
    lo_db = min(float(np.nanmin(s[2])) for s in series)
    ymin = min(-20.0, math.floor((lo_db - 2) / 5) * 5)
    ymax = 0.0
    x0, x1 = float(f.min()), float(f.max())
    px = lambda v: L + (v - x0) / (x1 - x0) * (W - L - R)  # noqa: E731
    py = lambda v: T + (ymax - max(min(v, ymax), ymin)) / (ymax - ymin) * (H - T - B)  # noqa: E731
    out = [_text(L, 30, title, 20, weight=600), _text(W - R, 30, NOTE, 11, MUTED, "end")]
    if band_hz:
        b0, b1 = (v / 1e9 for v in band_hz)
        out.append(f'<rect x="{_n(px(max(b0, x0)))}" y="{T}" width="{_n(px(min(b1, x1)) - px(max(b0, x0)))}" height="{H - T - B}" fill="{GREEN}" opacity="0.12"/>')
        out.append(_text((px(max(b0, x0)) + px(min(b1, x1))) / 2, T + 16, "band", 12, GREEN, "middle", 600))
    for v in nice_ticks(ymin, ymax, 6):
        out.append(f'<line x1="{L}" x2="{W - R}" y1="{_n(py(v))}" y2="{_n(py(v))}" stroke="{GRID}"/>' + _text(L - 8, py(v) + 4, _n(v), 12, MUTED, "end"))
    for v in nice_ticks(x0, x1, 8):
        out.append(f'<line y1="{T}" y2="{H - B}" x1="{_n(px(v))}" x2="{_n(px(v))}" stroke="{GRID}"/>' + _text(px(v), H - B + 20, _n(v), 12, MUTED, "middle"))
    out.append(_text((L + W - R) / 2, H - 24, "frequency (GHz)", 13, INK, "middle"))
    out.append(_text(22, (T + H - B) / 2, "S11 (dB)", 13, INK, "middle", extra=f'transform="rotate(-90 22 {(T + H - B) / 2})"'))
    if target_db is not None and ymin < target_db < ymax:
        out.append(f'<line x1="{L}" x2="{W - R}" y1="{_n(py(target_db))}" y2="{_n(py(target_db))}" stroke="{RED}" stroke-dasharray="6 4"/>' + _text(W - R - 4, py(target_db) - 6, f"target {_n(target_db)} dB", 12, RED, "end"))
    for name, fx, fy, colour in series:
        pts = " ".join(f"{_n(px(a))},{_n(py(b))}" for a, b in zip(fx, fy))
        out.append(f'<polyline points="{pts}" fill="none" stroke="{colour}" stroke-width="2.4" stroke-linejoin="round"/>')
    for d in (dips or [])[:6]:
        gx = d["f_hz"] / 1e9
        if x0 <= gx <= x1:
            out.append(f'<circle cx="{_n(px(gx))}" cy="{_n(py(d["s11_db"]))}" r="4.5" fill="#fff" stroke="{BLUE}" stroke-width="2"/>' + _text(px(gx), py(d["s11_db"]) + 20, f'{gx:.3f} GHz, {d["s11_db"]:.1f} dB', 12, INK, "middle"))
    ly = H - B + 48
    lx = L
    for name, _, _, colour in series:
        out.append(f'<line x1="{lx}" x2="{lx + 26}" y1="{ly - 4}" y2="{ly - 4}" stroke="{colour}" stroke-width="3"/>' + _text(lx + 34, ly, name, 12))
        lx += 60 + 7 * len(name)
    return to_png(_svg(W, H, "".join(out)), width)


# ------------------------------------------------------------------------------------------------ Smith


def smith_png(s11: dict, *, band_hz: Iterable[float] | None = None, as_built: dict | None = None, title: str = "Smith chart (reference 50 ohm)", width: int = 900) -> bytes:
    f = np.asarray(s11["freq_hz"], float)
    g = np.asarray(s11["s11_re"], float) + 1j * np.asarray(s11["s11_im"], float)
    S = 330.0
    cx, cy = 380.0, 395.0
    W, H = 760, 800
    X = lambda v: cx + v * S  # noqa: E731
    Y = lambda v: cy - v * S  # noqa: E731
    out = [_text(30, 34, title, 20, weight=600), _text(W - 24, 34, NOTE, 10, MUTED, "end")]
    out.append(f'<defs><clipPath id="u"><circle cx="{cx}" cy="{cy}" r="{S}"/></clipPath></defs>')
    out.append(f'<circle cx="{cx}" cy="{cy}" r="{S}" fill="#fafbfc" stroke="{MUTED}" stroke-width="1.6"/>')
    out.append(f'<line x1="{cx - S}" x2="{cx + S}" y1="{cy}" y2="{cy}" stroke="{GRID}" stroke-width="1.4"/>')
    body = []
    for r in (0.2, 0.5, 1.0, 2.0, 5.0):
        c, rad = r / (1 + r), 1 / (1 + r)
        body.append(f'<circle cx="{_n(X(c))}" cy="{cy}" r="{_n(rad * S)}" fill="none" stroke="{GRID}" stroke-width="1.2"/>')
    for x in (0.2, 0.5, 1.0, 2.0, 5.0):
        for sign in (1, -1):
            body.append(f'<circle cx="{_n(X(1))}" cy="{_n(Y(sign / x))}" r="{_n(S / x)}" fill="none" stroke="{GRID}" stroke-width="1.2"/>')
    out.append(f'<g clip-path="url(#u)">{"".join(body)}</g>')
    for r in (0.2, 0.5, 1.0, 2.0, 5.0):
        out.append(_text(X((r - 1) / (r + 1)), cy + 14, _n(r), 10, MUTED, "middle"))
    lo, hi = (min(band_hz), max(band_hz)) if band_hz else (f.min(), f.max())

    def trace(gamma: np.ndarray, colour: str, name: str) -> str:
        pts = " ".join(f"{_n(X(v.real))},{_n(Y(v.imag))}" for v in gamma)
        s = f'<polyline points="{pts}" fill="none" stroke="{colour}" stroke-width="1.8" opacity="0.55"/>'
        m = (f >= lo) & (f <= hi)
        if m.any():
            pb = " ".join(f"{_n(X(v.real))},{_n(Y(v.imag))}" for v in gamma[m])
            s += f'<polyline points="{pb}" fill="none" stroke="{colour}" stroke-width="3.6"/>'
            i0, i1 = np.nonzero(m)[0][[0, -1]]
            for i in (i0, i1):
                s += f'<circle cx="{_n(X(gamma[i].real))}" cy="{_n(Y(gamma[i].imag))}" r="4.5" fill="{colour}"/>' + _text(X(gamma[i].real) + 8, Y(gamma[i].imag) - 6, f"{f[i] / 1e9:.3f}", 11, INK)
        return s

    out.append(trace(g, BLUE, "antenna alone"))
    legend = [("antenna alone", BLUE)]
    if as_built and as_built.get("s11_re"):
        ga = np.asarray(as_built["s11_re"], float) + 1j * np.asarray(as_built["s11_im"], float)
        out.append(trace(ga, ORANGE, "as built"))
        legend.append(("as built", ORANGE))
    lx = 30
    for name, colour in legend:
        out.append(f'<line x1="{lx}" x2="{lx + 26}" y1="{H - 30}" y2="{H - 30}" stroke="{colour}" stroke-width="3.6"/>' + _text(lx + 34, H - 26, name + " (thick = band, GHz at its ends)", 12))
        lx += 330
    return to_png(_svg(W, H, "".join(out)), width)


# ------------------------------------------------------------------------------------------------ pattern


def _cut(dbi: np.ndarray, phi_a: int, phi_b: int) -> list[tuple[float, float]]:
    """Directivity along a great circle: angle from +z (0 to 360 degrees) and dBi. The grid is 5 degrees."""
    pts = []
    for a in range(0, 361, 5):
        if a <= 180:
            t, p = a, phi_a
        else:
            t, p = 360 - a, phi_b
        pts.append((float(a), float(dbi[t // 5, (p // 5) % dbi.shape[1]])))
    return pts


def pattern_png(pattern: dict, *, f_hz: float | None = None, width: int = 1200) -> bytes:
    dbi = np.asarray(pattern["directivity_dbi"], float)
    if dbi.ndim != 2 or dbi.shape[0] < 37:
        raise PlotError("pattern.json has no 5 degree grid")
    dmax = float(pattern["dmax_dbi"])
    floor = dmax - 30.0
    W, H = 1240, 640
    out = [_text(30, 34, "Radiation pattern (directivity, dBi)", 20, weight=600), _text(W - 24, 34, NOTE, 10, MUTED, "end")]
    freq = f_hz or pattern.get("f_hz")
    sub = f"at {freq / 1e9:.3f} GHz, peak {dmax:.1f} dBi toward theta {pattern.get('dmax_theta_deg')} deg, phi {pattern.get('dmax_phi_deg')} deg" if freq else f"peak {dmax:.1f} dBi"
    out.append(_text(30, 58, sub, 13, MUTED))
    R = 225.0
    panels = [(300, 350, 0, 180, "x-z plane (phi 0 / 180)", "+x", "-x"), (930, 350, 90, 270, "y-z plane (phi 90 / 270)", "+y", "-y")]
    rings = [dmax - k for k in (0, 10, 20, 30)]
    for cx, cy, pa, pb, name, right, left in panels:
        for k, lvl in enumerate(rings):
            rr = (lvl - floor) / (dmax - floor) * R
            out.append(f'<circle cx="{cx}" cy="{cy}" r="{_n(rr)}" fill="none" stroke="{GRID}" stroke-width="1.2"/>' + _text(cx + 4, cy - rr - 3, f"{lvl:.0f}", 10, MUTED))
        out.append(f'<line x1="{cx - R}" x2="{cx + R}" y1="{cy}" y2="{cy}" stroke="{GRID}"/><line x1="{cx}" x2="{cx}" y1="{cy - R}" y2="{cy + R}" stroke="{GRID}"/>')
        out.append(_text(cx, cy - R - 16, "+z (up)", 12, INK, "middle", 600) + _text(cx + R + 8, cy + 4, right, 12, INK) + _text(cx - R - 8, cy + 4, left, 12, INK, "end"))
        pts = []
        for a, v in _cut(dbi, pa, pb):
            rr = max(0.0, (v - floor) / (dmax - floor)) * R
            ang = math.radians(a)
            # angle from +z, swept toward the first axis on the right
            pts.append(f"{_n(cx + rr * math.sin(ang))},{_n(cy - rr * math.cos(ang))}")
        out.append(f'<polygon points="{" ".join(pts)}" fill="{BLUE}" fill-opacity="0.18" stroke="{BLUE}" stroke-width="2.4" stroke-linejoin="round"/>')
        out.append(_text(cx, cy + R + 34, name, 13, INK, "middle", 600))
    out.append(_text(W / 2, H - 14, "Axes are the solver's: z up from the board, scenario y points toward the top edge of the board picture (scenario y = minus board y).", 11, MUTED, "middle"))
    return to_png(_svg(W, H, "".join(out)), width)


# ------------------------------------------------------------------------------------------------ heat maps


def colourise(a: np.ndarray, gamma: float = 0.6) -> np.ndarray:
    """Map values in 0..1 to uint8 RGB with the ramp above."""
    a = np.clip(np.nan_to_num(a), 0.0, 1.0) ** gamma
    xs = np.array([p for p, _ in _RAMP])
    out = np.zeros(a.shape + (3,), np.uint8)
    for ch in range(3):
        out[..., ch] = np.interp(a, xs, np.array([c[ch] for _, c in _RAMP])).astype(np.uint8)
    return out


def _png_data_uri(rgb: np.ndarray, scale: int = 1) -> str:
    from PIL import Image

    im = Image.fromarray(rgb, "RGB")
    if scale > 1:
        im = im.resize((im.width * scale, im.height * scale), Image.BILINEAR)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _load_plane(job: Path, sidecar_name: str, key: str | None, file: str | None = None) -> tuple[np.ndarray, dict]:
    side = _read(job, sidecar_name)
    if side is None:
        raise PlotError(f"{sidecar_name} is missing from the job")
    info = side[key] if key else side
    path = Path(job) / (file or info["file"])
    if not path.is_file():
        raise PlotError(f"{path.name} is missing from the job")
    shape = tuple(info["shape"])
    data = np.fromfile(path, "<f4")
    if data.size != int(np.prod(shape)):
        raise PlotError(f"{path.name} does not match its sidecar shape {shape}")
    return data.reshape(shape), info


def _panel(x: float, y: float, w: float, h: float, rgb: np.ndarray, u_mm: list[float], v_lo_label: float, v_hi_label: float, *, flip: bool, title: str,
           u_label: str, v_label: str, overlay: str = "", clip_id: str = "p", v_up: bool = False) -> str:
    """One heat map in a box with axes. ``flip`` puts the last row at the top (rows increase upward).
    The vertical labels run ``v_lo_label`` to ``v_hi_label`` from top to bottom, or from bottom to top when ``v_up``."""
    img = np.flipud(rgb) if flip else rgb
    uri = _png_data_uri(img, 2)
    parts = [_text(x, y - 12, title, 14, INK, weight=600)]
    parts.append(f'<rect x="{_n(x)}" y="{_n(y)}" width="{_n(w)}" height="{_n(h)}" fill="#000"/>')
    parts.append(f'<image x="{_n(x)}" y="{_n(y)}" width="{_n(w)}" height="{_n(h)}" preserveAspectRatio="none" xlink:href="{uri}"/>')
    if overlay:
        parts.append(f'<g clip-path="url(#{clip_id})">{overlay}</g>')
    for v in nice_ticks(u_mm[0], u_mm[1], 7):
        px = x + (v - u_mm[0]) / (u_mm[1] - u_mm[0]) * w
        parts.append(f'<line x1="{_n(px)}" x2="{_n(px)}" y1="{_n(y + h)}" y2="{_n(y + h + 5)}" stroke="{MUTED}"/>' + _text(px, y + h + 20, _n(v), 11, MUTED, "middle"))
    for v in nice_ticks(v_lo_label, v_hi_label, 6):
        py = y + ((v_hi_label - v) if v_up else (v - v_lo_label)) / (v_hi_label - v_lo_label) * h
        parts.append(f'<line x1="{_n(x - 5)}" x2="{_n(x)}" y1="{_n(py)}" y2="{_n(py)}" stroke="{MUTED}"/>' + _text(x - 8, py + 4, _n(v), 11, MUTED, "end"))
    parts.append(_text(x + w / 2, y + h + 40, u_label, 12, INK, "middle"))
    parts.append(_text(x - 46, y + h / 2, v_label, 12, INK, "middle", extra=f'transform="rotate(-90 {_n(x - 46)} {_n(y + h / 2)})"'))
    return "".join(parts)


def _board_overlay(job: Path, outline: list | None, x: float, y: float, w: float, h: float, u_mm: list[float], vb_lo: float, vb_hi: float) -> str:
    """Outline, antenna box and port, in board millimetres, drawn over a plan view whose vertical axis is board y (vb_lo at the top)."""
    mx = lambda v: x + (v - u_mm[0]) / (u_mm[1] - u_mm[0]) * w  # noqa: E731
    my = lambda v: y + (v - vb_lo) / (vb_hi - vb_lo) * h  # noqa: E731
    parts = []
    for ring in outline or []:
        pts = " ".join(f"{_n(mx(px))},{_n(my(py))}" for px, py in ring)
        parts.append(f'<polygon points="{pts}" fill="none" stroke="#ffffff" stroke-width="1.4" opacity="0.8"/>')
    meta = _read(job, "meta.json") or {}
    setup = meta.get("setup") or {}
    if setup.get("antenna_bbox"):
        a = setup["antenna_bbox"]
        parts.append(f'<rect x="{_n(mx(a[0]))}" y="{_n(my(a[1]))}" width="{_n(mx(a[2]) - mx(a[0]))}" height="{_n(my(a[3]) - my(a[1]))}" fill="none" stroke="#7fe0ff" stroke-width="1.4" stroke-dasharray="5 3"/>')
    if setup.get("port_xy"):
        px, py = setup["port_xy"]
        parts.append(f'<circle cx="{_n(mx(px))}" cy="{_n(my(py))}" r="4" fill="#7fe0ff" stroke="#fff" stroke-width="1"/>')
    return "".join(parts)


def _plan(job: Path, data: np.ndarray, info: dict, title: str, outline: list | None, width: int, note: str) -> bytes:
    rows, cols = data.shape
    u = info["u_mm"]
    v = info["v_mm"]
    bo_lo, bo_hi = -v[1], -v[0]          # board y of the top and bottom edges of the picture
    aspect = (u[1] - u[0]) / max(bo_hi - bo_lo, 1e-6)
    pw = 900.0
    ph = pw / aspect
    if ph > 700:
        ph = 700.0
        pw = ph * aspect
    import textwrap
    lines = textwrap.wrap(note, max(40, int(pw / 6.2))) or ['']
    x0, y0 = 90.0, 76.0 + 16.0 * (len(lines) - 1)
    W, H = int(pw + x0 + 40), int(ph + y0 + 74)
    overlay = _board_overlay(job, outline, x0, y0, pw, ph, u, bo_lo, bo_hi)
    clip = f'<defs><clipPath id="p"><rect x="{_n(x0)}" y="{_n(y0)}" width="{_n(pw)}" height="{_n(ph)}"/></clipPath></defs>'
    body = _text(30, 32, title, 20, weight=600) + _text(W - 24, 32, NOTE, 10, MUTED, "end") + "".join(_text(30, 52 + 16 * i, line, 12, MUTED) for i, line in enumerate(lines)) + clip
    body += _panel(x0, y0, pw, ph, colourise(data), u, bo_lo, bo_hi, flip=True, title="", u_label="board x (mm)", v_label="board y (mm, downward)", overlay=overlay)
    return to_png(_svg(W, H, body), width)


def current_png(job: Path, *, outline: list | None = None, width: int = 1200) -> bytes:
    data, info = _load_plane(Path(job), "current.json", None)
    f = info.get("f_hz")
    note = f"|Js| on the antenna layer at {f / 1e9:.3f} GHz" if f else "|Js| on the antenna layer"
    note += ", brightest = strongest current (1.0 = 99.5th percentile over copper); black = no copper. Dashed box: antenna; dot: feed port."
    return _plan(Path(job), data, info, "Surface current on the antenna layer", outline, width, note)


def field_png(job: Path, *, outline: list | None = None, width: int = 1300) -> bytes:
    """Field magnitude at the target frequency: plan view above the board and a side view through the feed."""
    job = Path(job)
    xy, ixy = _load_plane(job, "field.json", "xy")
    xz, ixz = _load_plane(job, "field.json", "xz")
    mag_xy = np.hypot(xy[0], xy[1])
    mag_xz = np.hypot(xz[0], xz[1])
    mag_xy, mag_xz = (m / max(float(np.percentile(m, 99.5)), 1e-12) for m in (mag_xy, mag_xz))
    f = (_read(job, "field.json") or {}).get("f_hz")
    u1, v1 = ixy["u_mm"], ixy["v_mm"]
    u2, v2 = ixz["u_mm"], ixz["v_mm"]
    bo_lo, bo_hi = -v1[1], -v1[0]
    pw = 1000.0
    ph1 = min(470.0, pw / ((u1[1] - u1[0]) / (bo_hi - bo_lo)))
    pw1 = ph1 * (u1[1] - u1[0]) / (bo_hi - bo_lo)
    ph2 = min(250.0, pw / ((u2[1] - u2[0]) / (v2[1] - v2[0])))
    pw2 = ph2 * (u2[1] - u2[0]) / (v2[1] - v2[0])
    x0 = 110.0
    W = int(max(pw1, pw2) + x0 + 40)
    y1, y2 = 90.0, 90.0 + ph1 + 110.0
    clip1 = f'<defs><clipPath id="p"><rect x="{_n(x0)}" y="{_n(y1)}" width="{_n(pw1)}" height="{_n(ph1)}"/></clipPath></defs>'
    body = _text(30, 32, "Electric field magnitude", 20, weight=600) + _text(W - 24, 32, NOTE, 10, MUTED, "end")
    sub = f"at {f / 1e9:.3f} GHz, " if f else ""
    body += _text(30, 52, f"{sub}arbitrary units (scaled to the 99.5th percentile). Brighter = stronger field.", 12, MUTED) + clip1
    body += _panel(x0, y1, pw1, ph1, colourise(mag_xy), u1, bo_lo, bo_hi, flip=True, title=f"Plan view {ixy.get('plane_mm', 0):.1f} mm above the stackup bottom",
                   u_label="board x (mm)", v_label="board y (mm, downward)", overlay=_board_overlay(job, outline, x0, y1, pw1, ph1, u1, bo_lo, bo_hi))
    body += _panel(x0, y2, pw2, ph2, colourise(mag_xz), u2, v2[0], v2[1], flip=True, title=f"Side view through the feed (board y = {-ixz.get('plane_mm', 0):.1f} mm)",
                   u_label="board x (mm)", v_label="height z (mm)", clip_id="none", v_up=True)
    return to_png(_svg(W, int(y2 + ph2 + 70), body), width)
