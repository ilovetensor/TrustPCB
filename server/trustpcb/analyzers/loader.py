"""Load a board for the analyzers: from ``artifacts/board.json``, or by parsing the project."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..intake import detect as detect_mod
from ..pcb.model import Board, Footprint, Keepout, Layer, Net, Pad, Track, Via, Zone, parse_board


def board_from_dict(d: dict[str, Any]) -> Board:
    """Rebuild a :class:`Board` from ``Board.to_dict()`` output (graphics and texts are not stored)."""
    layers = [Layer(**x) for x in d.get("layers", [])]
    board = Board(
        version_id=d.get("version_id", ""),
        title=d.get("title", ""),
        outline=d.get("outline", []),
        bbox=d.get("bbox", [0, 0, 0, 0]),
        layers=layers,
        stackup=d.get("stackup", {}),
        nets=[Net(**x) for x in d.get("nets", [])],
        footprints=[Footprint(**x) for x in d.get("footprints", [])],
        pads=[Pad(**x) for x in d.get("pads", [])],
        tracks=[Track(**x) for x in d.get("tracks", [])],
        vias=[Via(**x) for x in d.get("vias", [])],
        zones=[Zone(**x) for x in d.get("zones", [])],
        keepouts=[Keepout(**x) for x in d.get("keepouts", [])],
        warnings=list(d.get("warnings", [])),
        kicad_version=d.get("kicad_version"),
        thickness=float((d.get("stackup") or {}).get("total_thickness_mm") or 1.6),
    )
    return board


def attach_project(board: Board, version_dir: str | Path) -> Board:
    """Give a board rebuilt from ``board.json`` its project settings (net classes, design rules) when the project file is there."""
    from .project import read_project_file

    if not getattr(board, "project", None):
        board.project = read_project_file(version_dir)
    return board


def load_board(version_dir: str | Path) -> Board:
    """``artifacts/board.json`` if present (it carries refilled zones), else parse the project files."""
    version_dir = Path(version_dir)
    cached = version_dir / "artifacts" / "board.json"
    if cached.is_file():
        return attach_project(board_from_dict(json.loads(cached.read_text(encoding="utf-8"))), version_dir)
    project = version_dir / "project" if (version_dir / "project").is_dir() else version_dir
    info = detect_mod.find_project(project)
    if not info["pcb"]:
        raise FileNotFoundError(f"no .kicad_pcb under {project}")
    return parse_board(project / info["pcb"], version_dir.name)
