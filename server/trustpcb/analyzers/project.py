"""The project's own settings: net classes and design rules from the ``.kicad_pro`` file.

A board may carry the project dictionary (``board.project``, attached by the loader from the version's
project files). Failing that, a board parsed from a file has its ``source`` path and the project file is read
from beside it, and last the project files under ``ctx.version_dir`` are used. Without a project file every
function here returns empty data and the callers say nothing about the project.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..pcb.model import Board
from .base import AnalyzerContext


def read_project_file(version_dir: str | Path | None) -> dict[str, Any]:
    """The ``.kicad_pro`` JSON under a version directory (``project/`` or the directory itself), or {}."""
    if not version_dir:
        return {}
    root = Path(version_dir)
    root = root / "project" if (root / "project").is_dir() else root
    try:
        pros = sorted(root.rglob("*.kicad_pro"), key=lambda p: (len(p.parts), str(p)))
        # prefer the project that sits beside the board file
        pcbs = sorted(root.rglob("*.kicad_pcb"), key=lambda p: (not p.with_suffix(".kicad_pro").is_file(), len(p.parts), str(p)))
        pro = pcbs[0].with_suffix(".kicad_pro") if pcbs and pcbs[0].with_suffix(".kicad_pro").is_file() else (pros[0] if pros else None)
        return json.loads(pro.read_text(encoding="utf-8")) if pro else {}
    except (OSError, ValueError):
        return {}


def project_settings(board: Board, ctx: AnalyzerContext | None = None) -> dict[str, Any]:
    cache = ctx.cache if ctx is not None else None
    if cache is not None and cache.get("project_board") is board:
        return cache["project"]
    data = getattr(board, "project", None)
    if not data and getattr(board, "source", None):
        # a board parsed from a file keeps its path; the project file sits beside it
        pro = Path(board.source).with_suffix(".kicad_pro")
        try:
            data = json.loads(pro.read_text(encoding="utf-8")) if pro.is_file() else None
        except (OSError, ValueError):
            data = None
    if not data and ctx is not None:
        data = read_project_file(ctx.version_dir)
    data = data if isinstance(data, dict) else {}
    if cache is not None:
        cache["project"], cache["project_board"] = data, board
    return data


def net_classes(board: Board, ctx: AnalyzerContext | None = None) -> dict[str, dict[str, Any]]:
    """Net classes by name: {name: {track_width, clearance, via_diameter, via_drill, diff_pair_width ...}}."""
    classes = (project_settings(board, ctx).get("net_settings") or {}).get("classes") or []
    return {c["name"]: c for c in classes if isinstance(c, dict) and c.get("name")}


def net_class_of(board: Board, net: str | None) -> str:
    n = board._net.get(net) if net else None
    return n.net_class if n is not None else "Default"


def net_class_info(board: Board, ctx: AnalyzerContext | None, net: str | None) -> dict[str, Any] | None:
    """``{name, track_width_mm, clearance_mm, source}`` for the net's class, or None without a project file."""
    classes = net_classes(board, ctx)
    name = net_class_of(board, net)
    c = classes.get(name)
    if c is None:
        return None
    out: dict[str, Any] = {"name": name, "source": "the project's own net class settings (.kicad_pro)"}
    for key, field in (("track_width", "track_width_mm"), ("clearance", "clearance_mm"), ("via_diameter", "via_diameter_mm"), ("via_drill", "via_drill_mm")):
        if isinstance(c.get(key), (int, float)):
            out[field] = float(c[key])
    return out


def design_rules(board: Board, ctx: AnalyzerContext | None = None) -> dict[str, Any]:
    """The project's board design-rule minimums (``board.design_settings.rules``), or {}."""
    return ((project_settings(board, ctx).get("board") or {}).get("design_settings") or {}).get("rules") or {}
