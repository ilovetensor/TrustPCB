"""The one board renderer (spec section 12).

Every picture of a board comes from here: the browser's 2D viewer (layer SVGs with marks in a
separate SVG on top), the images shown to the agent, and the marked-up report images. They
share one coordinate frame: the SVG ``viewBox`` is in board millimetres, x right and y down,
equal to the outline bounding box plus a margin, so a point at ``(x, y)`` on the board is at
``(x, y)`` in every layer SVG and in the overlay produced by :func:`annotate`.

Layers
------
``layer_svg(board, layer, opts)`` accepts a board layer name (``F.Cu``, ``B.Cu``, ``In1.Cu``,
``F.SilkS``, ``B.SilkS``, ``F.CrtYd``, ``B.CrtYd``) or one of:

``outline``      board shape and edge line
``F.Refs``/``B.Refs``  reference designators as text
``top``/``bottom``     a composite picture (outline, copper, silk, labels) for one side
                       (not mirrored: the bottom view keeps board coordinates so marks align)
``all``          every layer stacked, bottom side first

Stable element ids
------------------
``data-id`` carries the target id on tracks (``track:..``), pads (``pad:U1:3``), vias, zones and
on a ``<g>`` per footprint (``ref:U1``); ``data-net`` carries the net name.

Text is drawn with ``<text>``; rasterising needs any system sans-serif font. Pass
``font_dirs`` to :func:`render_png` on a host without fonts.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape, quoteattr

from . import geom
from .model import Board, Geometry, UnknownTarget

FONT = "Inter, Helvetica, Arial, sans-serif"

THEMES: dict[str, dict[str, str]] = {
    "dark": {
        "bg": "#0e1115",
        "board": "#1d2a24",
        "edge": "#e6c84f",
        "F.Cu": "#d0463c",
        "B.Cu": "#3f7fd0",
        "inner": "#3bb27a",
        "F.SilkS": "#f4f1c4",
        "B.SilkS": "#e9b8ad",
        "F.CrtYd": "#d36bd3",
        "B.CrtYd": "#8f6bd3",
        "hole": "#0e1115",
        "ref": "#ffffff",
        "ref_halo": "#0e1115",
    },
    "light": {
        "bg": "#ffffff",
        "board": "#e9efe9",
        "edge": "#8a7300",
        "F.Cu": "#c0392b",
        "B.Cu": "#2f6fc0",
        "inner": "#2a9d68",
        "F.SilkS": "#4d4a1f",
        "B.SilkS": "#7a4a40",
        "F.CrtYd": "#a03aa0",
        "B.CrtYd": "#6a3aa0",
        "hole": "#ffffff",
        "ref": "#1a1a1a",
        "ref_halo": "#ffffff",
    },
}

SEVERITY_COLORS = {
    "critical": "#ff2d55",
    "high": "#ff4d4f",
    "medium": "#ff9f1a",
    "low": "#f2d024",
    "info": "#4da3ff",
}
ACCENT = "#22d3ee"


@dataclass
class RenderOptions:
    margin: float = 2.0
    theme: str = "dark"
    px_per_mm: float = 12.0
    background: bool | None = None  # None: only the composite views draw a background
    labels: bool = True
    #: ``[x0, y0, x1, y1]`` in board mm: render only this window (for zoomed views); no margin added
    region: tuple[float, float, float, float] | list[float] | None = None

    @property
    def colors(self) -> dict[str, str]:
        return THEMES.get(self.theme, THEMES["dark"])


def _n(v: float) -> str:
    s = f"{v:.3f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def _pts(points) -> str:
    return " ".join(f"{_n(p[0])},{_n(p[1])}" for p in points)


def _path(rings) -> str:
    out = []
    for r in rings:
        if len(r) < 2:
            continue
        out.append("M" + " L".join(f"{_n(p[0])} {_n(p[1])}" for p in r) + " Z")
    return "".join(out)


def view_box(board: Board, opts: RenderOptions | None = None) -> list[float]:
    """``[x, y, w, h]`` in board millimetres: the outline bounding box plus the margin."""
    opts = opts or RenderOptions()
    if opts.region is not None:
        rx0, ry0, rx1, ry1 = opts.region
        return [rx0, ry0, rx1 - rx0, ry1 - ry0]
    x0, y0, x1, y1 = board.bbox
    m = opts.margin
    return [x0 - m, y0 - m, (x1 - x0) + 2 * m, (y1 - y0) + 2 * m]


def _header(board: Board, opts: RenderOptions, layer: str, background: bool) -> list[str]:
    vx, vy, vw, vh = view_box(board, opts)
    pw, ph = vw * opts.px_per_mm, vh * opts.px_per_mm
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{_n(vx)} {_n(vy)} {_n(vw)} {_n(vh)}" '
        f'width="{_n(pw)}" height="{_n(ph)}" preserveAspectRatio="xMidYMid meet" '
        f'data-layer={quoteattr(layer)} data-units="mm">'
    ]
    if background:
        out.append(f'<rect x="{_n(vx)}" y="{_n(vy)}" width="{_n(vw)}" height="{_n(vh)}" fill="{opts.colors["bg"]}"/>')
    return out


def _layer_color(board: Board, layer: str, c: dict[str, str]) -> str:
    if layer in c:
        return c[layer]
    return c["inner"]


def _via_on_layer(board: Board, via, layer: str) -> bool:
    if layer in via.layers:
        return True
    copper = board.copper_layers()
    ordered = sorted(copper, key=lambda n: (0 if n == "F.Cu" else 2 if n == "B.Cu" else 1, n))
    idx = {n: i for i, n in enumerate(ordered)}
    ends = [idx[n] for n in via.layers if n in idx]
    return bool(ends) and layer in idx and min(ends) <= idx[layer] <= max(ends)


def _copper_body(board: Board, layer: str, opts: RenderOptions) -> list[str]:
    c = opts.colors
    color = _layer_color(board, layer, c)
    out: list[str] = [f'<g id="layer-{escape(layer)}" data-role="copper">']
    # zones first so tracks and pads sit on top of the pour
    for z in sorted(board.zones, key=lambda z: z.priority):
        rings = z.filled.get(layer)
        if rings:
            out.append(
                f'<path data-id={quoteattr(z.id)} data-net={quoteattr(z.net or "")} d="{_path(rings)}" '
                f'fill="{color}" fill-opacity="0.38" stroke="{color}" stroke-opacity="0.55" stroke-width="0.05" fill-rule="nonzero"/>'
            )
        elif layer in z.layers and z.net and not z.filled and z.outline:
            out.append(
                f'<path data-id={quoteattr(z.id)} data-net={quoteattr(z.net)} data-unfilled="1" d="{_path([z.outline])}" '
                f'fill="none" stroke="{color}" stroke-opacity="0.6" stroke-width="0.1" stroke-dasharray="0.6 0.4"/>'
            )
    for k in board.keepouts:
        if layer in k.layers and k.ring:
            out.append(
                f'<path data-id={quoteattr(k.id)} data-keepout="1" d="{_path([k.ring])}" fill="#ffffff" fill-opacity="0.05" '
                f'stroke="#ffffff" stroke-opacity="0.55" stroke-width="0.12" stroke-dasharray="0.8 0.5"/>'
            )
    out.append(f'<g fill="none" stroke="{color}" stroke-linecap="round" stroke-linejoin="round">')
    for t in board.tracks:
        if t.layer != layer:
            continue
        attrs = f'data-id={quoteattr(t.id)} data-net={quoteattr(t.net or "")} stroke-width="{_n(t.width)}"'
        if t.mid is None:
            out.append(f'<line {attrs} x1="{_n(t.start[0])}" y1="{_n(t.start[1])}" x2="{_n(t.end[0])}" y2="{_n(t.end[1])}"/>')
        else:
            out.append(f'<polyline {attrs} points="{_pts(t.points())}"/>')
    out.append("</g>")
    fp_pads: dict[str, list[str]] = {}
    holes: list[str] = []
    for p in board.pads:
        if layer not in p.layers:
            continue
        el = (
            f'<path data-id={quoteattr(p.id)} data-net={quoteattr(p.net or "")} d="{_path([p.ring])}"/>'
        )
        fp_pads.setdefault(p.ref, []).append(el)
        if p.drill:
            holes.append(f'<circle cx="{_n(p.x)}" cy="{_n(p.y)}" r="{_n(p.drill / 2)}"/>')
    out.append(f'<g fill="{color}">')
    for ref, els in fp_pads.items():
        out.append(f'<g data-id={quoteattr("ref:" + ref)}>' + "".join(els) + "</g>")
    out.append("</g>")
    vias = [v for v in board.vias if _via_on_layer(board, v, layer)]
    if vias:
        out.append(f'<g fill="{color}">')
        for v in vias:
            out.append(f'<circle data-id={quoteattr(v.id)} data-net={quoteattr(v.net or "")} cx="{_n(v.x)}" cy="{_n(v.y)}" r="{_n(v.diameter / 2)}"/>')
        out.append("</g>")
        holes.extend(f'<circle cx="{_n(v.x)}" cy="{_n(v.y)}" r="{_n(v.drill / 2)}"/>' for v in vias)
    if holes:
        out.append(f'<g fill="{c["hole"]}">' + "".join(holes) + "</g>")
    out.append("</g>")
    return out


def _outline_body(board: Board, opts: RenderOptions, fill: bool = True) -> list[str]:
    c = opts.colors
    d = _path(board.outline)
    return [
        f'<g id="layer-outline" data-role="outline"><path data-id="outline" d="{d}" fill="{c["board"] if fill else "none"}" '
        f'fill-rule="evenodd" stroke="{c["edge"]}" stroke-width="0.15" stroke-linejoin="round"/></g>'
    ]


def _silk_body(board: Board, layer: str, opts: RenderOptions) -> list[str]:
    c = opts.colors
    color = c.get(layer, c["F.SilkS"])
    out = [f'<g id="layer-{escape(layer)}" data-role="silk" stroke="{color}" fill="none" stroke-linecap="round" stroke-linejoin="round">']
    for g in board.graphics:
        if g.layer != layer:
            continue
        w = _n(max(g.width, 0.05))
        owner = f" data-owner={quoteattr(g.owner)}" if g.owner else ""
        if g.kind == "line":
            out.append(f'<polyline{owner} points="{_pts(g.points)}" stroke-width="{w}"/>')
        else:
            fill = f' fill="{color}"' if g.fill else ""
            out.append(f'<path{owner} d="{_path([g.points])}" stroke-width="{w}"{fill}/>')
    for t in board.texts:
        if t.layer != layer or t.owner is not None:
            continue
        out.append(_text_el(t.text, t.x, t.y, t.size, color, t.rotation, None, "400", t.justify))
    out.append("</g>")
    return out


def _text_el(text: str, x: float, y: float, size: float, fill: str, rotation: float = 0.0, halo: str | None = None, weight: str = "400", justify: str = "center") -> str:
    anchor = {"left": "start", "right": "end"}.get(justify, "middle")
    tr = f' transform="rotate({_n(-rotation)} {_n(x)} {_n(y)})"' if rotation else ""
    halo_attrs = f' stroke="{halo}" stroke-width="{_n(size * 0.22)}" paint-order="stroke" stroke-linejoin="round"' if halo else ' stroke="none"'
    return (
        f'<text x="{_n(x)}" y="{_n(y)}" font-family="{FONT}" font-size="{_n(size)}" font-weight="{weight}" '
        f'text-anchor="{anchor}" dominant-baseline="central" fill="{fill}"{halo_attrs}{tr}>{escape(text)}</text>'
    )


def _crtyd_body(board: Board, layer: str, opts: RenderOptions) -> list[str]:
    color = opts.colors[layer]
    out = [f'<g id="layer-{escape(layer)}" data-role="courtyard" stroke="{color}" fill="none" stroke-width="0.06" stroke-dasharray="0.4 0.25">']
    for f in board.footprints:
        if f.courtyard and ((layer == "F.CrtYd") == (f.side == "top")):
            out.append(f'<path data-id={quoteattr(f.id)} d="{_path([f.courtyard])}"/>')
    out.append("</g>")
    return out


def _refs_body(board: Board, layer: str, opts: RenderOptions) -> list[str]:
    c = opts.colors
    side = "top" if layer == "F.Refs" else "bottom"
    out = [f'<g id="layer-{escape(layer)}" data-role="refs">']
    for f in board.footprints:
        if f.side != side:
            continue
        w, h = f.bbox[2] - f.bbox[0], f.bbox[3] - f.bbox[1]
        size = max(0.45, min(1.0, min(w, h) * 0.45))
        out.append(_text_el(f.ref, (f.bbox[0] + f.bbox[2]) / 2, (f.bbox[1] + f.bbox[3]) / 2, size, c["ref"], 0.0, c["ref_halo"], "600"))
    out.append("</g>")
    return out


COMPOSITES = {
    "top": ["outline", "F.Cu", "F.SilkS", "F.Refs"],
    "bottom": ["outline", "B.Cu", "B.SilkS", "B.Refs"],
}


def _body(board: Board, layer: str, opts: RenderOptions) -> list[str]:
    if layer == "outline":
        return _outline_body(board, opts)
    if layer in ("F.SilkS", "B.SilkS"):
        return _silk_body(board, layer, opts)
    if layer in ("F.CrtYd", "B.CrtYd"):
        return _crtyd_body(board, layer, opts)
    if layer in ("F.Refs", "B.Refs"):
        return _refs_body(board, layer, opts)
    if layer in board.copper_layers():
        return _copper_body(board, layer, opts)
    raise ValueError(f"unknown layer {layer!r}; available: {scene_layer_names(board)}")


def scene_layer_names(board: Board) -> list[str]:
    return ["outline", *board.copper_layers(), "F.SilkS", "B.SilkS", "F.CrtYd", "B.CrtYd", "F.Refs", "B.Refs"]


def layer_svg(board: Board, layer: str, opts: RenderOptions | None = None) -> str:
    """One layer (or composite view) as a complete SVG string. See the module docstring."""
    opts = opts or RenderOptions()
    if layer in COMPOSITES or layer == "all":
        if layer == "all":
            names = ["outline", *reversed(board.copper_layers()), "B.SilkS", "F.SilkS", "F.Refs", "B.Refs"]
        else:
            names = list(COMPOSITES[layer])
            if layer == "top" and not opts.labels:
                names.remove("F.Refs")
            if layer == "bottom" and not opts.labels:
                names.remove("B.Refs")
        parts = _header(board, opts, layer, True if opts.background is None else opts.background)
        for n in names:
            parts.extend(_body(board, n, opts))
        parts.append("</svg>")
        return "".join(parts)
    parts = _header(board, opts, layer, bool(opts.background))
    parts.extend(_body(board, layer, opts))
    parts.append("</svg>")
    return "".join(parts)


def board_scene(board: Board, opts: RenderOptions | None = None) -> dict[str, str]:
    """``{layer name: svg}`` for every layer the viewer can toggle; all share one viewBox."""
    opts = opts or RenderOptions()
    return {name: layer_svg(board, name, opts) for name in scene_layer_names(board)}


# ------------------------------------------------------------------------------ PNG


def render_png(svg: str, width: int = 1600, out: str | Path | None = None, background: str | None = None, font_dirs: list[str] | None = None) -> bytes:
    """Rasterise an SVG with resvg (``resvg-py``). Returns PNG bytes and writes ``out`` if given."""
    try:
        import resvg_py
    except ImportError as exc:  # pragma: no cover - environment problem
        raise RuntimeError("render_png needs the 'resvg-py' package (MIT; bundles resvg, MPL-2.0)") from exc
    kwargs: dict[str, Any] = {"svg_string": svg, "width": int(width)}
    if background:
        kwargs["background"] = background
    if font_dirs:
        kwargs["font_dirs"] = font_dirs
    data = bytes(resvg_py.svg_to_bytes(**kwargs))
    if out is not None:
        Path(out).write_bytes(data)
    return data


# -------------------------------------------------------------------------- marks

_VB = re.compile(r'viewBox="([-\d.eE+ ]+)"')


def _geo_of(mark_target: str, board: Board | None, resolved_by_id: dict[str, dict]) -> dict | None:
    if mark_target in resolved_by_id:
        return resolved_by_id[mark_target]
    if board is None:
        return None
    try:
        return board.resolve(mark_target).to_dict()
    except UnknownTarget:
        return None


def _mark_geometries(mark: dict, board: Board | None) -> list[dict]:
    """The resolved geometry of each target of a mark, from ``resolved`` or from the board."""
    resolved = mark.get("resolved")
    by_id: dict[str, dict] = {}
    out: list[dict] = []
    if isinstance(resolved, dict):
        resolved = [resolved]
    if isinstance(resolved, list):
        for g in resolved:
            if isinstance(g, Geometry):
                g = g.to_dict()
            if isinstance(g, dict):
                if g.get("target"):
                    by_id[g["target"]] = g
                out.append(g)
    targets = mark.get("targets") or ([mark["target"]] if mark.get("target") else [])
    if targets:
        collected = []
        for t in targets:
            g = _geo_of(t, board, by_id)
            if g is not None:
                collected.append(g)
        if collected:
            return collected
    return out


def _flatten(g: dict) -> list[dict]:
    if g.get("kind") == "group":
        out: list[dict] = []
        for part in g.get("parts") or []:
            out.extend(_flatten(part))
        return out
    return [g]


def _outline_geo(g: dict, color: str, u: float, fill_opacity: float, width_scale: float = 1.0, dashed: bool = False) -> str:
    kind = g.get("kind")
    dash = f' stroke-dasharray="{_n(u * 0.5)} {_n(u * 0.35)}"' if dashed else ""
    if kind == "polygon":
        d = _path(g.get("rings") or [])
        return (
            f'<path d="{d}" fill="{color}" fill-opacity="{fill_opacity}" stroke="{color}" stroke-width="{_n(u * 0.22 * width_scale)}"{dash} stroke-linejoin="round"/>'
        )
    if kind == "polyline":
        pts = g.get("points") or []
        w = (g.get("width") or 0.2) + u * 0.5 * width_scale
        return (
            f'<polyline points="{_pts(pts)}" fill="none" stroke="{color}" stroke-opacity="0.45" stroke-width="{_n(w)}" stroke-linecap="round" stroke-linejoin="round"/>'
            f'<polyline points="{_pts(pts)}" fill="none" stroke="{color}" stroke-width="{_n(max(g.get("width") or 0.2, u * 0.12))}" stroke-linecap="round" stroke-linejoin="round"{dash}/>'
        )
    if kind == "point":
        cx, cy = g["points"][0]
        r = (g.get("width") or 0.6) / 2 + u * 0.3
        return f'<circle cx="{_n(cx)}" cy="{_n(cy)}" r="{_n(r)}" fill="{color}" fill-opacity="{fill_opacity}" stroke="{color}" stroke-width="{_n(u * 0.2)}"/>'
    return ""


def annotate(svg: str, marks: list[dict], board: Board | None = None, *, labels: bool = True) -> str:
    """Overlay ``marks`` on a layer SVG and return the combined SVG string.

    Each mark is a dict ``{type, targets, label?, value?, resolved?, number?, severity?, color?}``
    where ``resolved`` is the server-added geometry (``Geometry.to_dict()`` for each target).
    Without ``resolved`` the targets are resolved against ``board``. Marks that cannot be
    resolved are skipped. The overlay is one ``<g id="marks">`` placed last, so the same group
    can be used on its own in a separate SVG over the viewer (use :func:`marks_svg`).
    """
    m = _VB.search(svg)
    if not m:
        raise ValueError("annotate needs an SVG with a viewBox")
    vx, vy, vw, vh = (float(v) for v in m.group(1).split())
    overlay = _marks_group(marks, board, vw, vh, labels)
    idx = svg.rfind("</svg>")
    if idx < 0:
        raise ValueError("annotate needs a complete SVG")
    return svg[:idx] + overlay + svg[idx:]


def marks_svg(marks: list[dict], board: Board, opts: RenderOptions | None = None, *, labels: bool = True) -> str:
    """A transparent SVG holding only the marks, with the same viewBox as the layer SVGs."""
    opts = opts or RenderOptions()
    vx, vy, vw, vh = view_box(board, opts)
    head = _header(board, opts, "marks", False)
    return "".join(head) + _marks_group(marks, board, vw, vh, labels) + "</svg>"


def _marks_group(marks: list[dict], board: Board | None, vw: float, vh: float, labels: bool) -> str:
    u = max(vw, vh) / 100.0
    u = max(0.35, min(u, 2.5))
    out = ['<g id="marks" data-role="marks">']
    for i, mark in enumerate(marks, start=1):
        geos = _mark_geometries(mark, board)
        if not geos:
            continue
        color = mark.get("color") or SEVERITY_COLORS.get(str(mark.get("severity", "")).lower(), ACCENT)
        number = mark.get("number", i)
        typ = mark.get("type", "pin")
        label = mark.get("label")
        value = mark.get("value")
        if isinstance(value, dict) and value.get("value") is not None:
            vtxt = f"{value['value']} {value.get('unit', '')}".strip()
            label = f"{label}: {vtxt}" if label else vtxt
        flat = [x for g in geos for x in _flatten(g)]
        tids = " ".join(str(t) for t in (mark.get("targets") or []))
        out.append(f'<g data-mark-type={quoteattr(str(typ))} data-targets={quoteattr(tids)}>')
        anchor = _anchor(geos)
        if typ == "pin":
            out.append(_pin(anchor, number, color, u, label if labels else None, flat))
        elif typ == "object":
            for g in flat:
                out.append(_outline_geo(g, color, u, 0.18))
            out.append(_pin(anchor, number, color, u, label if labels else None, [], leader=True))
        elif typ == "path":
            for g in flat:
                out.append(_outline_geo(g, color, u, 0.0, width_scale=1.3))
            out.append(_pin(anchor, number, color, u, label if labels else None, [], leader=True))
        elif typ == "region":
            for g in flat:
                out.append(_outline_geo(g, color, u, 0.14, dashed=True))
            out.append(_pin(anchor, number, color, u, label if labels else None, [], leader=True))
        elif typ == "link":
            out.append(_link(geos, color, u, label if labels else None, value))
        elif typ == "sketch":
            out.append(_sketch(geos, color, u, label if labels else None))
        else:
            out.append(_pin(anchor, number, color, u, label if labels else None, flat))
        out.append("</g>")
    out.append("</g>")
    return "".join(out)


def _anchor(geos: list[dict]) -> tuple[float, float]:
    centers = [g["center"] for g in geos if g.get("center")]
    if not centers:
        b = geom.union_bbox(g["bbox"] for g in geos if g.get("bbox")) or [0, 0, 0, 0]
        return (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
    if len(geos) == 1:
        return centers[0][0], centers[0][1]
    return sum(c[0] for c in centers) / len(centers), sum(c[1] for c in centers) / len(centers)


def _pin(anchor: tuple[float, float], number: Any, color: str, u: float, label: str | None, flat: list[dict], leader: bool = False) -> str:
    ax, ay = anchor
    r = 1.15 * u
    out: list[str] = []
    for g in flat:  # a pin on an object also outlines the object so alignment is visible
        out.append(_outline_geo(g, color, u * 1.4, 0.35, 0.8))
    bx, by = ax + r * 1.5, ay - r * 1.5
    out.append(f'<line x1="{_n(ax)}" y1="{_n(ay)}" x2="{_n(bx)}" y2="{_n(by)}" stroke="{color}" stroke-width="{_n(u * 0.12)}"/>')
    out.append(f'<circle cx="{_n(ax)}" cy="{_n(ay)}" r="{_n(u * 0.28)}" fill="none" stroke="{color}" stroke-width="{_n(u * 0.12)}"/>')
    out.append(f'<circle cx="{_n(bx)}" cy="{_n(by)}" r="{_n(r)}" fill="{color}" stroke="#ffffff" stroke-width="{_n(u * 0.12)}"/>')
    out.append(_text_el(str(number), bx, by, r * 1.25, "#101418", 0.0, None, "700"))
    if label:
        tx = bx + r * 1.3
        out.append(
            f'<text x="{_n(tx)}" y="{_n(by)}" font-family="{FONT}" font-size="{_n(u * 1.25)}" font-weight="600" dominant-baseline="central" '
            f'fill="#ffffff" stroke="#101418" stroke-width="{_n(u * 0.3)}" paint-order="stroke" stroke-linejoin="round">{escape(str(label))}</text>'
        )
    return "".join(out)


def _link(geos: list[dict], color: str, u: float, label: str | None, value: Any) -> str:
    centers = [g["center"] for g in geos if g.get("center")]
    if len(centers) < 2:
        return _pin(_anchor(geos), "", color, u, label, [])
    (x0, y0), (x1, y1) = centers[0], centers[1]
    dist = math.hypot(x1 - x0, y1 - y0)
    text = label or ""
    if not (isinstance(value, dict) and value.get("value") is not None):
        text = f"{text} {dist:.2f} mm".strip()
    out = [
        f'<line x1="{_n(x0)}" y1="{_n(y0)}" x2="{_n(x1)}" y2="{_n(y1)}" stroke="{color}" stroke-width="{_n(u * 0.16)}" stroke-dasharray="{_n(u * 0.6)} {_n(u * 0.35)}" stroke-linecap="round"/>',
        f'<circle cx="{_n(x0)}" cy="{_n(y0)}" r="{_n(u * 0.3)}" fill="{color}"/>',
        f'<circle cx="{_n(x1)}" cy="{_n(y1)}" r="{_n(u * 0.3)}" fill="{color}"/>',
    ]
    for g in geos[:2]:
        out.append(_outline_geo(g, color, u * 0.7, 0.1, 0.7))
    if text:
        out.append(
            f'<text x="{_n((x0 + x1) / 2)}" y="{_n((y0 + y1) / 2 - u * 0.9)}" font-family="{FONT}" font-size="{_n(u * 1.25)}" font-weight="600" text-anchor="middle" '
            f'fill="#ffffff" stroke="#101418" stroke-width="{_n(u * 0.3)}" paint-order="stroke" stroke-linejoin="round">{escape(text)}</text>'
        )
    return "".join(out)


def _sketch(geos: list[dict], color: str, u: float, label: str | None) -> str:
    centers = [g["center"] for g in geos if g.get("center")]
    if len(centers) < 2:
        return ""
    pts = _pts(centers)
    out = [f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="{_n(u * 0.2)}" stroke-dasharray="{_n(u * 0.7)} {_n(u * 0.5)}" stroke-linecap="round" stroke-opacity="0.9"/>']
    if label:
        mx, my = centers[len(centers) // 2]
        out.append(
            f'<text x="{_n(mx)}" y="{_n(my - u)}" font-family="{FONT}" font-size="{_n(u * 1.1)}" text-anchor="middle" fill="#ffffff" '
            f'stroke="#101418" stroke-width="{_n(u * 0.28)}" paint-order="stroke">{escape(label)} (sketch)</text>'
        )
    return "".join(out)
