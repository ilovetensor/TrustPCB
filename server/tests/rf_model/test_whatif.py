from __future__ import annotations

import pytest
from shapely.geometry import box

from trustpcb.rf import geometry as geo
from trustpcb.rf import whatif as W


def ok(board, setup, *ops):
    W.validate_ops(list(ops), board, setup)


def bad(board, setup, code, *ops):
    with pytest.raises(W.WhatIfError) as e:
        W.validate_ops(list(ops), board, setup)
    assert e.value.code == code


def test_length_limits(nrf_board, nrf_setup):
    ok(nrf_board, nrf_setup, W.AdjustAntennaLength(6.0), )
    ok(nrf_board, nrf_setup, W.AdjustAntennaLength(-6.0))
    bad(nrf_board, nrf_setup, "out_of_limits", W.AdjustAntennaLength(6.5))
    bad(nrf_board, nrf_setup, "out_of_limits", W.AdjustAntennaLength(-7))
    bad(nrf_board, nrf_setup, "duplicate_operation", W.AdjustAntennaLength(1), W.AdjustAntennaLength(2))


def test_material_limits(nrf_board, nrf_setup):
    ok(nrf_board, nrf_setup, W.SetMaterial(3.8), W.SetMaterial(4.8, 0.02))
    bad(nrf_board, nrf_setup, "out_of_limits", W.SetMaterial(3.7))
    bad(nrf_board, nrf_setup, "out_of_limits", W.SetMaterial(4.9))


def test_clear_region_limits(nrf_board, nrf_setup):
    ok(nrf_board, nrf_setup, W.ClearCopperRegion(bbox=(36, 17, 44, 22)))
    bad(nrf_board, nrf_setup, "out_of_limits", W.ClearCopperRegion(bbox=(30, 14, 60, 30)))
    bad(nrf_board, nrf_setup, "overlaps_antenna", W.ClearCopperRegion(bbox=(22, 1, 26, 3)))
    bad(nrf_board, nrf_setup, "not_near_antenna", W.ClearCopperRegion(bbox=(2, 40, 8, 42)))
    bad(nrf_board, nrf_setup, "overlaps_port", W.ClearCopperRegion(bbox=(32, 13.5, 36, 16)))


def test_remove_part_limits(nrf_board, nrf_setup):
    bad(nrf_board, nrf_setup, "unknown_part", W.RemoveNearbyPart("R999"))
    bad(nrf_board, nrf_setup, "matching_part", W.RemoveNearbyPart("L1"))


def test_dicts_roundtrip_and_unknown():
    op = W.op_from_dict({"op": "clear_copper_region", "bbox": [1, 2, 3, 4]})
    assert W.op_from_dict(W.op_to_dict(op)) == op
    with pytest.raises(W.WhatIfError):
        W.op_from_dict({"op": "write_kicad_file"})


def _track_length(items):
    import math

    return sum(math.dist(i.points[0], i.points[-1]) if len(i.points) == 2 else 0 for i in items if i.kind == "track" and i.antenna)


def test_shorten_reduces_centreline_length(nrf_board, nrf_setup):
    items = geo.collect_items(nrf_board, nrf_setup)
    out, note = W.apply_to_items(list(items), [W.AdjustAntennaLength(-4.0)], nrf_board, nrf_setup)
    assert _track_length(items) - _track_length(out) == pytest.approx(4.0, abs=0.05) and "shortened" in note[0]
    with pytest.raises(W.WhatIfError):
        W.apply_to_items(list(items), [W.AdjustAntennaLength(-40.0)], nrf_board, nrf_setup)  # not validated here: longer than the antenna


def test_extend_goes_somewhere_free_or_is_refused_with_a_typed_error(nrf_board, nrf_setup):
    items = geo.collect_items(nrf_board, nrf_setup)
    try:
        out, note = W.apply_to_items(list(items), [W.AdjustAntennaLength(6.0)], nrf_board, nrf_setup)
    except W.WhatIfError as e:
        assert e.code == "blocked"
        return
    assert _track_length(out) - _track_length(items) == pytest.approx(6.0, abs=0.05) and "lengthened" in note[0]


def test_clear_region_removes_copper(nrf_board, nrf_setup):
    r = (36, 17, 44, 22)
    m0 = geo.build_model(nrf_board, nrf_setup)
    m1 = geo.build_model(nrf_board, nrf_setup, ops=[W.ClearCopperRegion(bbox=r)])
    assert m1.layers["F.Cu"].intersection(box(*r)).area < 1e-6 < m0.layers["F.Cu"].intersection(box(*r)).area


def test_remove_part_drops_pads(nrf_board, nrf_setup):
    ref = "C1"
    fp = nrf_board.footprint(ref)
    if fp is None or fp.x < nrf_setup.region[0]:
        pytest.skip("no suitable part")
    items = geo.collect_items(nrf_board, nrf_setup)
    out, _ = W.apply_to_items(items, [W.RemoveNearbyPart(ref)], nrf_board, nrf_setup)
    assert not [i for i in out if i.ref == ref] and [i for i in items if i.ref == ref]
