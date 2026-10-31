"""decoupling_pin: supply pins of active parts with no fitted capacitor within the decoupling limit.

The per-capacitor distances are the ``decoupling`` rule. This rule starts from the pin: for each supply pin of an
active part (measured in ``decoupling.measure``) the nearest fitted capacitor to ground on its net is found, pad centre to
pad centre. A pin with none within ``decoupling_max_mm`` is a candidate, even when every capacitor on the net is well placed for
another pin: "pin 11 has no capacitor within 3 mm".
"""

from __future__ import annotations

from typing import Any

from ..pcb.model import Board
from .base import AnalyzerContext, Emitter, mark, meas
from .decoupling import measure, pin_class

RULE = "decoupling_pin"


def analyze(board: Board, ctx: AnalyzerContext) -> list[dict[str, Any]]:
    max_mm = float(ctx.t("decoupling_max_mm"))
    data = measure(board, ctx)
    em = Emitter(RULE)
    for pr in data["pins"]:
        if pr["within"]:
            continue
        pin, near = pr["pin"], pr["nearest"]
        name = pr["pin_name"]
        label = f"{pin.ref} pin {pin.number}{f' {name}' if name and name != pr['net'] else ''}"
        m = {"limit": meas(max_mm, "mm", 2), "capacitors_within_limit": meas(0, "count"), "capacitors_on_net": meas(pr["on_net"], "count")}
        targets = [pin.id, f"net:{pr['net']}"]
        marks = [mark("object", [pin.id], label=label)]
        if near:
            fp, cp, d = near
            m["nearest_distance"] = meas(d, "mm", 2)
            targets.append(fp.id)
            marks.append(mark("link", [pin.id, cp.id], label=f"nearest: {fp.ref}", value=meas(d, "mm", 2)))
            title = f"{label} ({pr['net']}) has no capacitor within {max_mm:g} mm; the nearest, {fp.ref}, is {d:.1f} mm away"
        else:
            title = f"{label} ({pr['net']}) has no decoupling capacitor on its net"
        em.add(
            title,
            m,
            targets,
            marks,
            "medium" if near is None or near[2] > 2 * max_mm else "low",
            position=(pin.x, pin.y),
            net=pr["net"],
            ic=pin.ref,
            ic_pin=pin.number,
            ic_pin_name=name,
            nearest_capacitor=near[0].ref if near else None,
            pin_class=pin_class(pr["net"]),
            details=[f"Distances are pad centre to pad centre to fitted capacitors with one pad on {pr['net']} and the other on ground; {pr['on_net']} such capacitor(s) are on the net."],
        )
    return em.items
