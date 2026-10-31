"""A function zone stays on the board even when a part overhangs the edge."""
from __future__ import annotations

from pathlib import Path

from shapely.geometry import Polygon

from trustpcb.analyzers import AnalyzerContext, zones
from trustpcb.pcb.model import parse_board
from trustpcb.rf.geometry import board_outline_polygon

LDO_PCB = Path(__file__).resolve().parents[3] / "samples" / "usb-ldo-led" / "usb_ldo_led_node.kicad_pcb"


def test_zones_stay_inside_the_board_outline():
    board = parse_board(LDO_PCB, "v-ldo")
    regions = zones.compute_regions(board, AnalyzerContext())
    outline = board_outline_polygon(board).buffer(1e-3)
    assert regions and all(outline.contains(Polygon(r["ring"])) for r in regions)
