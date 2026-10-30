"""Hand-built boards for the analyzer tests."""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parents[2]
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from trustpcb.pcb import geom  # noqa: E402
from trustpcb.pcb.model import Board, Footprint, Keepout, Layer, Net, Pad, Track, Via, Zone  # noqa: E402

SAMPLES = SERVER.parent / "samples"
NRF_PCB = SAMPLES / "nrf52840-node" / "nrf52840_rf_iot_node.kicad_pcb"
PATCH_PCB = SAMPLES / "patch-2g5" / "patch_antenna.kicad_pcb"

STACK = {
    "source": "board",
    "assumed": False,
    "total_thickness_mm": 1.6,
    "layers": [
        {"name": "F.Cu", "kind": "copper", "thickness_mm": 0.035, "epsilon_r": None, "loss_tangent": None, "material": "Copper"},
        {"name": "dielectric 1", "kind": "dielectric", "thickness_mm": 1.53, "epsilon_r": 4.5, "loss_tangent": 0.02, "material": "FR4"},
        {"name": "B.Cu", "kind": "copper", "thickness_mm": 0.035, "epsilon_r": None, "loss_tangent": None, "material": "Copper"},
    ],
}


class Builder:
    def __init__(self, w: float = 50.0, h: float = 40.0):
        self.w, self.h = w, h
        self.tracks: list[Track] = []
        self.pads: list[Pad] = []
        self.vias: list[Via] = []
        self.zones: list[Zone] = []
        self.keepouts: list[Keepout] = []
        self.fps: list[Footprint] = []
        self.nets: set[str] = set()
        self.stackup = STACK
        self.copper = ["F.Cu", "B.Cu"]

    def track(self, net, a, b, width=0.25, layer="F.Cu"):
        self.nets.add(net)
        i = len(self.tracks)
        self.tracks.append(Track(f"track:t{i:03d}", net, layer, width, list(a), list(b), None, math.dist(a, b)))
        return self.tracks[-1].id

    def poly(self, net, pts, width=0.25, layer="F.Cu"):
        return [self.track(net, pts[i], pts[i + 1], width, layer) for i in range(len(pts) - 1)]

    def part(self, ref, value, x, y, pads, lib="X", size=1.0, layer="F.Cu"):
        """pads: list of (number, net, dx, dy)."""
        ids = []
        for number, net, dx, dy in pads:
            if net:
                self.nets.add(net)
            ring = geom.rect_ring(x + dx, y + dy, 0.6, 0.6)
            p = Pad(f"pad:{ref}:{number}", ref, number, net, x + dx, y + dy, 0.6, 0.6, 0.0, "rect", [layer], None, ring)
            self.pads.append(p)
            ids.append(p.id)
        bbox = [x - size, y - size, x + size, y + size]
        self.fps.append(Footprint(f"ref:{ref}", ref, value, lib, x, y, 0.0, "top", bbox, geom.bbox_ring(bbox), ids))

    def via(self, net, x, y, d=0.6):
        self.nets.add(net)
        self.vias.append(Via(f"via:v{len(self.vias):03d}", net, x, y, d, 0.3, ["F.Cu", "B.Cu"]))
        return self.vias[-1].id

    def zone(self, net, layer, ring, holes=()):
        """A filled pour: ``ring`` with optional rectangular ``holes`` (x0, y0, x1, y1) cut as separate rings."""
        from shapely.geometry import Polygon, box

        shape = Polygon(ring)
        for h in holes:
            shape = shape.difference(box(*h))
        rings = []
        polys = [shape] if shape.geom_type == "Polygon" else list(shape.geoms)
        for p in polys:
            rings.append([[x, y] for x, y in list(p.exterior.coords)[:-1]])
        self.nets.add(net)
        self.zones.append(Zone(f"zone:z{len(self.zones):03d}", net, [layer], {layer: rings}, False, "", [list(p) for p in ring]))

    def keepout(self, ring, name="ANTENNA_KEEPOUT", layers=("F.Cu", "B.Cu")):
        rules = {"tracks": "not_allowed", "vias": "not_allowed", "pads": "not_allowed", "copperpour": "not_allowed", "footprints": "not_allowed"}
        self.keepouts.append(Keepout(f"keepout:k{len(self.keepouts):03d}", list(layers), ring, rules, name))

    def build(self) -> Board:
        outline = [[[0, 0], [self.w, 0], [self.w, self.h], [0, self.h]]]
        layers = [Layer("F.Cu", "copper", 0), Layer("B.Cu", "copper", 31)]
        return Board(
            "v-test", "test", outline, [0, 0, self.w, self.h], layers, self.stackup,
            [Net(f"net:{n}", n) for n in sorted(self.nets)],
            self.fps, self.pads, self.tracks, self.vias, self.zones, self.keepouts,
        )


@pytest.fixture()
def builder():
    return Builder


@pytest.fixture(scope="session")
def nrf_board():
    from trustpcb.pcb.model import parse_board

    return parse_board(NRF_PCB, "v-nrf")


@pytest.fixture(scope="session")
def patch_board():
    from trustpcb.pcb.model import parse_board

    return parse_board(PATCH_PCB, "v-patch")
