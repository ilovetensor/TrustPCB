"""Debug picture of the simulation model: copper per layer, port, mesh lines, region."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw


COLORS = {0: (200, 60, 50, 150), 1: (50, 90, 200, 130), 2: (60, 160, 70, 120), 3: (200, 160, 40, 120)}


def render_debug_png(model, setup, mesh=None, path: str | Path = "model.png", scale: float | None = None, crop: list[float] | None = None) -> Path:
    x0, y0, x1, y1 = crop or model.region
    w, h = x1 - x0, y1 - y0
    scale = scale or min(2400 / w, 1800 / h)
    W, H = int(w * scale) + 20, int(h * scale) + 20

    def P(x, y):
        return (10 + (x - x0) * scale, 10 + (y - y0) * scale)

    img = Image.new("RGBA", (W, H), (255, 255, 255, 255))
    d = ImageDraw.Draw(img, "RGBA")
    if mesh is not None:
        for xv in mesh.x:
            if x0 <= xv <= x1:
                d.line([P(xv, y0), P(xv, y1)], fill=(0, 0, 0, 22), width=1)
        for yv in mesh.y:
            if y0 <= -yv <= y1:
                d.line([P(x0, -yv), P(x1, -yv)], fill=(0, 0, 0, 22), width=1)
    layers = list(model.layers.items())
    # draw ground-ish (bottom) first
    order = sorted(layers, key=lambda kv: setup.stackup["layer_z"].get(kv[0], 0))
    for k, (name, g) in enumerate(order):
        col = COLORS.get(k, (120, 120, 120, 120))
        for poly in getattr(g, "geoms", [g]):
            if poly.is_empty:
                continue
            d.polygon([P(*p) for p in poly.exterior.coords], fill=col)
            for hole in poly.interiors:
                d.polygon([P(*p) for p in hole.coords], fill=(255, 255, 255, 255))
    for name, g in order:
        a = model.antenna.get(name)
        if a is not None and not a.is_empty:
            for poly in getattr(a, "geoms", [a]):
                d.line([P(*p) for p in poly.exterior.coords], fill=(0, 0, 0, 255), width=1)
    for v in model.vias:
        px, py = P(v["x"], v["y"])
        r = max(1.5, v["diameter"] / 2 * scale)
        d.ellipse([px - r, py - r, px + r, py + r], outline=(0, 120, 0, 255))
    for box_, col in ((model.region, (0, 0, 0, 255)), (model.fine_zone, (230, 120, 0, 255))):
        d.rectangle([P(box_[0], box_[1]), P(box_[2], box_[3])], outline=col, width=2)
    px, py = P(*setup.port_xy)
    d.ellipse([px - 6, py - 6, px + 6, py + 6], outline=(255, 0, 255, 255), width=3)
    if setup.open_end:
        ox, oy = P(*setup.open_end)
        d.rectangle([ox - 4, oy - 4, ox + 4, oy + 4], outline=(0, 160, 160, 255), width=2)
    out = Path(path)
    img.convert("RGB").save(out)
    return out
