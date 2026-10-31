"""netclass_width: tracks narrower than the track width of their own net class in the project.

The ``.kicad_pro`` net classes say how wide the designer wanted each class routed (the RF class may ask
for the width that gives 50 ohm, the POWER class for a current-carrying width). KiCad's design rule check
does not enforce that value, only the board minimum, so a narrower track is silent there. One candidate is
emitted per net that has tracks narrower than its class width, with the lengths by width. Tracks of a
printed antenna's radiating element (see ``radiating``) are left out: the element is meant to be that shape.

Needs the project file. Without one nothing is emitted.
"""

from __future__ import annotations

from typing import Any

from ..pcb.model import Board
from .base import AnalyzerContext, Emitter, mark, meas
from .classify import is_rf_net
from .impedance import estimate, stackup_assumptions, width_for_target
from .project import net_class_info, net_classes
from .radiating import radiating

RULE = "netclass_width"


def analyze(board: Board, ctx: AnalyzerContext) -> list[dict[str, Any]]:
    if not net_classes(board, ctx):
        return []
    tol = float(ctx.t("netclass_width_tolerance_mm"))
    rad = radiating(board, ctx)
    per_net: dict[str, dict[str, Any]] = {}
    for t in sorted(board.tracks, key=lambda t: t.id):
        if not t.net or t.id in rad.track_ids:
            continue
        info = net_class_info(board, ctx, t.net)
        if not info or "track_width_mm" not in info:
            continue
        cls_w = info["track_width_mm"]
        st = per_net.setdefault(t.net, {"info": info, "total": 0.0, "narrow": {}, "tracks": []})
        st["total"] += t.length
        if t.width < cls_w - tol:
            st["narrow"][round(t.width, 4)] = st["narrow"].get(round(t.width, 4), 0.0) + t.length
            st["tracks"].append(t.id)
    em = Emitter(RULE)
    for net in sorted(per_net):
        st = per_net[net]
        if not st["narrow"]:
            continue
        info = st["info"]
        cls_w = info["track_width_mm"]
        widths = sorted(st["narrow"])
        narrow_len = sum(st["narrow"].values())
        tracks = st["tracks"]
        by_width = [{"width_mm": w, "length_mm": round(st["narrow"][w], 2)} for w in widths]
        m: dict[str, Any] = {
            "class_track_width": meas(cls_w, "mm", 4),
            "narrowest_width": meas(widths[0], "mm", 4),
            "widest_narrow_width": meas(widths[-1], "mm", 4),
            "narrow_length": meas(narrow_len, "mm", 2),
            "net_length": meas(st["total"], "mm", 2),
        }
        first = board._track[tracks[0]]
        layer = first.layer
        extra: dict[str, Any] = {}
        if is_rf_net(net, ctx) or info["name"].upper() == "RF":
            # only a line that carries RF has an impedance target to compare the class width with
            z = estimate(board, layer, cls_w)
            if z is not None:
                m["class_width_z0_estimate"] = meas(z["z0"], "ohm", 1)
                m["target_impedance"] = meas(float(ctx.target_ohms), "ohm", 1)
                m["class_width_error_pct"] = meas(abs(z["z0"] - float(ctx.target_ohms)) / float(ctx.target_ohms) * 100.0, "%", 1)
                extra["stackup_assumptions"] = stackup_assumptions(board, z)
            need = width_for_target(board, layer, float(ctx.target_ohms))
            if need is not None:
                m["width_for_target"] = meas(need, "mm", 3)
        em.add(
            f"{net} is routed {', '.join(f'{w:g}' for w in widths)} mm wide; its net class {info['name']} asks {cls_w:g} mm",
            m,
            tracks,
            [mark("path", tracks, label=f"{info['name']} class asks {cls_w:g} mm")],
            "low",
            position=((first.start[0] + first.end[0]) / 2, (first.start[1] + first.end[1]) / 2),
            net=net,
            net_class=info,
            narrow_by_width=by_width,
            layer=layer,
            **extra,
            details=["Measured against the project's own net class track width. KiCad's design rule check does not enforce a class width."],
        )
    return em.items
