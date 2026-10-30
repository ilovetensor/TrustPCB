"""Stackup confirmation data and the assumptions list.

The goal form shows the stackup read from the board (or the default) and lets the user correct it.
Every value that was not read from the board or typed by the user is listed as an assumption, and
the report repeats that list. ``confirmation(board)`` is the payload for the form;
``apply_overrides`` returns a new stackup with the user's edits and ``assumptions`` rebuilds the list.
"""

from __future__ import annotations

import copy
from typing import Any

from ..pcb.model import Board

DEFAULT_EPSILON_R = 4.5
DEFAULT_LOSS_TANGENT = 0.02
DEFAULT_COPPER_MM = 0.035

#: shown beside the material field; a hint, never applied silently
MATERIAL_HINTS = {
    "FR4": {"epsilon_r": 4.5, "loss_tangent": 0.02},
    "Rogers RO4350B": {"epsilon_r": 3.48, "loss_tangent": 0.0037},
    "Rogers RO4003C": {"epsilon_r": 3.55, "loss_tangent": 0.0027},
    "Isola 370HR": {"epsilon_r": 4.04, "loss_tangent": 0.021},
}


def assumptions(board: Board, stackup: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """The assumed values behind the stackup, as ``{id, field, value, unit, source, text}``."""
    stack = stackup or board.stackup
    out: list[dict[str, Any]] = []
    source = stack.get("source", "default")
    if source == "default":
        out.append(
            {
                "id": "stackup.default",
                "field": "stackup",
                "value": f"{stack.get('total_thickness_mm', board.thickness)} mm {len(board.copper_layers())}-layer FR-4",
                "unit": None,
                "source": "default",
                "text": "The board file has no stackup, so a default FR-4 stackup was assumed.",
            }
        )
    for layer in stack.get("layers", []):
        if layer.get("kind") != "dielectric":
            continue
        name = layer["name"]
        if layer.get("epsilon_r") is None:
            out.append({"id": f"stackup.{name}.epsilon_r", "field": "epsilon_r", "value": DEFAULT_EPSILON_R, "unit": None, "source": "default", "text": f"Permittivity of {name} is not set; {DEFAULT_EPSILON_R} (FR-4) assumed."})
        elif layer.get("assumed_epsilon") or source == "default":
            out.append({"id": f"stackup.{name}.epsilon_r", "field": "epsilon_r", "value": layer["epsilon_r"], "unit": None, "source": "default", "text": f"Permittivity {layer['epsilon_r']} for {name} is the FR-4 default, not a measured value."})
        if layer.get("loss_tangent") is None:
            out.append({"id": f"stackup.{name}.loss_tangent", "field": "loss_tangent", "value": DEFAULT_LOSS_TANGENT, "unit": None, "source": "default", "text": f"Loss tangent of {name} is not set; {DEFAULT_LOSS_TANGENT} assumed."})
        elif source == "default":
            out.append({"id": f"stackup.{name}.loss_tangent", "field": "loss_tangent", "value": layer["loss_tangent"], "unit": None, "source": "default", "text": f"Loss tangent {layer['loss_tangent']} for {name} is the FR-4 default."})
        if layer.get("user_provided"):
            out = [a for a in out if not a["id"].startswith(f"stackup.{name}.")]
            out.append({"id": f"stackup.{name}.user", "field": "dielectric", "value": layer.get("material") or layer["epsilon_r"], "unit": None, "source": "user", "text": f"{name} values were provided by the user."})
    if source == "default":
        out.append({"id": "stackup.copper", "field": "copper_thickness", "value": DEFAULT_COPPER_MM, "unit": "mm", "source": "default", "text": "Copper thickness 35 um (1 oz) assumed on outer layers."})
    return out


def confirmation(board: Board) -> dict[str, Any]:
    """Data for the stackup step of the goal form."""
    stack = board.stackup
    copper = board.copper_layers()
    return {
        "source": stack["source"],
        "assumed": stack["assumed"],
        "copper_layers": len(copper),
        "total_thickness_mm": stack.get("total_thickness_mm", board.thickness),
        "layers": stack["layers"],
        "assumptions": assumptions(board),
        "needs_confirmation": bool(stack["assumed"]),
        "material_hints": MATERIAL_HINTS,
        "message": (
            "Stackup was read from the board file. Check it against your fabricator's."
            if not stack["assumed"]
            else "The board file has no stackup. Confirm the thickness and material, or the default 1.6 mm FR-4 will be used and listed as an assumption."
        ),
    }


def apply_overrides(stackup: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Return a stackup with user edits applied.

    ``overrides``: ``{"epsilon_r": 3.48, "loss_tangent": 0.0037, "material": "Rogers RO4350B",
    "total_thickness_mm": 0.8}``. The dielectric layers are scaled to a new total thickness and
    receive the new material values; the result is marked user-provided and no longer assumed.
    """
    out = copy.deepcopy(stackup)
    diel = [layer for layer in out["layers"] if layer["kind"] == "dielectric"]
    total = overrides.get("total_thickness_mm")
    if total and diel:
        copper_total = sum(layer.get("thickness_mm") or 0 for layer in out["layers"] if layer["kind"] == "copper")
        old = sum(layer.get("thickness_mm") or 0 for layer in diel) or 1.0
        scale = max(total - copper_total, 0.05) / old
        for layer in diel:
            layer["thickness_mm"] = round((layer.get("thickness_mm") or 0) * scale, 4)
        out["total_thickness_mm"] = total
    for layer in diel:
        for key in ("epsilon_r", "loss_tangent", "material"):
            if overrides.get(key) is not None:
                layer[key] = overrides[key]
                layer["user_provided"] = True
    if any(layer.get("user_provided") for layer in diel):
        out["source"] = "user"
        out["assumed"] = False
    return out
