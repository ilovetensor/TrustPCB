from __future__ import annotations

import pytest
from shapely.geometry import Point, Polygon, box

from trustpcb.rf import geometry as geo


def test_triangulate_polygon_with_hole_conserves_area():
    p = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)], [[(3, 3), (3, 6), (6, 6), (6, 3)]])
    tris = geo.triangulate(p)
    assert geo.triangles_area(tris) == pytest.approx(p.area, rel=1e-3)
    # no triangle centre falls in the hole
    for t in tris:
        cx = sum(v[0] for v in t) / 3
        cy = sum(v[1] for v in t) / 3
        assert not (3 < cx < 6 and 3 < cy < 6)


def test_nrf_model_copper(nrf_board, nrf_setup):
    m = geo.build_model(nrf_board, nrf_setup)
    assert set(m.layers) == {"F.Cu", "B.Cu"}
    ant = m.antenna["F.Cu"]
    assert ant.area > 20  # the 31 mm, 1 mm wide meander
    # antenna copper is exact: its tracks are the buffered polylines
    tri = geo.triangulate(m.layers["F.Cu"])
    assert geo.triangles_area(tri) == pytest.approx(m.layers["F.Cu"].area, rel=2e-3)
    # keep-out: no copper over the antenna keep-out area except the antenna itself
    ko = box(19.5, 0, 44.5, 10)
    other = m.layers["F.Cu"].intersection(ko).difference(ant.buffer(0.01))
    assert other.area < 0.5
    # ground under the port on the ground layer
    assert m.layers["B.Cu"].contains(Point(*nrf_setup.port_xy))
    assert m.stats["vias_kept"] > 5


def test_far_copper_is_simplified(nrf_board, nrf_setup):
    m = geo.build_model(nrf_board, nrf_setup)
    fz = box(*m.fine_zone)
    far = m.layers["F.Cu"].difference(fz)
    # signal tracks are dropped far away, the pour stays; the triangle count stays small
    assert len(geo.triangulate(far)) < 2500


def test_patch_model_has_patch_and_feed(patch_board, patch_setup):
    m = geo.build_model(patch_board, patch_setup)
    assert m.layers["F.Cu"].area > 36.8 * 28.5 * 0.9
    assert m.layers["B.Cu"].area > 0.9 * 50 * 60 * 0.9
