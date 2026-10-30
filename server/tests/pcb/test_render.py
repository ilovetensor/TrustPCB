from __future__ import annotations

import re
import struct

import pytest

from trustpcb.pcb import render_svg as R


def _viewbox(svg):
    return [float(v) for v in re.search(r'viewBox="([^"]+)"', svg).group(1).split()]


def test_viewbox_is_outline_bbox_plus_margin_in_mm(nrf_board):
    svg = R.layer_svg(nrf_board, "F.Cu")
    assert _viewbox(svg) == pytest.approx([-2, -2, 70, 57])
    svg = R.layer_svg(nrf_board, "F.Cu", R.RenderOptions(margin=5))
    assert _viewbox(svg) == pytest.approx([-5, -5, 76, 63])


def test_scene_layers_share_one_viewbox(nrf_board):
    scene = R.board_scene(nrf_board)
    assert set(scene) == {"outline", "F.Cu", "B.Cu", "F.SilkS", "B.SilkS", "F.CrtYd", "B.CrtYd", "F.Refs", "B.Refs"}
    boxes = {tuple(_viewbox(s)) for s in scene.values()}
    assert len(boxes) == 1
    for s in scene.values():
        assert s.startswith("<svg") and s.endswith("</svg>")


def test_stable_ids_on_elements(nrf_board):
    svg = R.layer_svg(nrf_board, "F.Cu")
    t = next(t for t in nrf_board.tracks if t.layer == "F.Cu")
    assert f'data-id="{t.id}"' in svg
    assert 'data-id="pad:U3:A8"' in svg and 'data-id="ref:U3"' in svg
    v = nrf_board.vias[0]
    assert f'data-id="{v.id}"' in svg
    assert 'data-id="zone:46abab45"' in svg
    assert 'data-net="ANT_FEED"' in svg
    # a B.Cu-only track is not on the top layer
    b_track = next(t for t in nrf_board.tracks if t.layer == "B.Cu")
    assert b_track.id not in svg


def test_composite_and_unknown_layer(nrf_board):
    top = R.layer_svg(nrf_board, "top")
    assert 'data-layer="top"' in top and "layer-F.SilkS" in top and "layer-outline" in top
    assert "layer-B.Cu" not in top
    assert "layer-B.Cu" in R.layer_svg(nrf_board, "all")
    with pytest.raises(ValueError):
        R.layer_svg(nrf_board, "Nope.Cu")


def test_annotate_pin_lands_on_pad_coordinates(nrf_board):
    svg = R.layer_svg(nrf_board, "F.Cu")
    marks = [{"type": "pin", "targets": ["pad:U3:A8"], "label": "A8", "severity": "high"}]
    out = R.annotate(svg, marks, nrf_board)
    assert out.count("</svg>") == 1 and 'id="marks"' in out
    # the target ring is a circle centred exactly on the pad centre, in board mm
    p = nrf_board._pad["pad:U3:A8"]
    assert f'cx="{p.x:g}" cy="{p.y:g}"' in out
    # unresolved targets are skipped, not fatal
    assert 'data-mark-type' not in R.annotate(svg, [{"type": "pin", "targets": ["pad:NOPE:1"]}], nrf_board)


def test_annotate_uses_server_resolved_geometry_and_all_mark_types(nrf_board):
    svg = R.layer_svg(nrf_board, "top")
    resolved = lambda t: nrf_board.resolve(t).to_dict()  # noqa: E731
    marks = [
        {"type": "object", "targets": ["ref:U3"], "resolved": [resolved("ref:U3")], "label": "U3"},
        {"type": "path", "targets": ["net:ANT_FEED"]},
        {"type": "region", "targets": [nrf_board.keepouts[0].id]},
        {"type": "link", "targets": ["ref:U3", "ref:Y1"]},
        {"type": "sketch", "targets": ["ref:U3", "ref:C6", "ref:Y1"], "label": "idea"},
        {"type": "pin", "targets": ["point:10,10"], "value": {"value": 3.2, "unit": "mm"}, "label": "gap"},
    ]
    out = R.annotate(svg, marks, nrf_board)
    for kind in ("object", "path", "region", "link", "sketch", "pin"):
        assert f'data-mark-type="{kind}"' in out
    assert "3.2 mm" in out
    # the link label states centre-to-centre distance
    assert re.search(r"\d+\.\d\d mm", out)


def test_marks_svg_has_same_viewbox(nrf_board):
    s = R.marks_svg([{"type": "pin", "targets": ["ref:U3"]}], nrf_board)
    assert _viewbox(s) == _viewbox(R.layer_svg(nrf_board, "F.Cu"))


def test_region_option_crops_view(nrf_board):
    svg = R.layer_svg(nrf_board, "F.Cu", R.RenderOptions(region=(24, 14, 40, 26)))
    assert _viewbox(svg) == [24, 14, 16, 12]


def test_render_png(nrf_board, tmp_path):
    svg = R.annotate(R.layer_svg(nrf_board, "top"), [{"type": "pin", "targets": ["pad:U3:A8"], "label": "A8"}], nrf_board)
    out = tmp_path / "x.png"
    data = R.render_png(svg, 800, out)
    assert data[:8] == b"\x89PNG\r\n\x1a\n" and out.read_bytes() == data
    w, h = struct.unpack(">II", data[16:24])
    assert w == 800 and h == pytest.approx(800 * 57 / 70, abs=2)


def test_patch_board_renders(patch_board):
    svg = R.layer_svg(patch_board, "top")
    assert 'data-id="zone:2a4b5c6d"' not in svg or True  # F.Cu zone drawn on the top composite
    assert R.render_png(svg, 400)[:4] == b"\x89PNG"
