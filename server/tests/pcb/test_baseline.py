from __future__ import annotations

import asyncio
import hashlib
import json
import math
import shutil
import struct
from pathlib import Path

import numpy as np
import pytest

from trustpcb.intake.upload import ingest, unlock
from trustpcb.pcb import kicad_cli, baseline
from trustpcb.pcb.model import parse_board

SAMPLES = Path(__file__).resolve().parents[3] / "samples"
pytestmark = pytest.mark.skipif(kicad_cli.find_kicad_cli() is None, reason="kicad-cli not installed")


def read_glb(path: Path):
    data = path.read_bytes()
    magic, version, length = struct.unpack("<III", data[:12])
    assert magic == 0x46546C67 and length == len(data)
    off, chunks = 12, []
    while off < len(data):
        cl, _ = struct.unpack("<II", data[off : off + 8])
        chunks.append(data[off + 8 : off + 8 + cl])
        off += 8 + cl
    return json.loads(chunks[0]), chunks[1]


def mesh_vertices(gltf, blob, name_suffix):
    idx = next(i for i, m in enumerate(gltf["meshes"]) if m.get("name", "").endswith(name_suffix))
    out = []
    for prim in gltf["meshes"][idx]["primitives"]:
        acc = gltf["accessors"][prim["attributes"]["POSITION"]]
        bv = gltf["bufferViews"][acc["bufferView"]]
        off = bv.get("byteOffset", 0) + acc.get("byteOffset", 0)
        out.append(np.frombuffer(blob, dtype="<f4", count=acc["count"] * 3, offset=off).reshape(-1, 3))
    return np.concatenate(out) * 1000.0  # metres -> mm


@pytest.fixture(scope="module")
def nrf_version(tmp_path_factory):
    root = tmp_path_factory.mktemp("nrf")
    vdir = root / "v1"
    before = {p.relative_to(SAMPLES): hashlib.sha256(p.read_bytes()).hexdigest() for p in (SAMPLES / "nrf52840-node").rglob("*") if p.is_file()}
    result = ingest(SAMPLES / "nrf52840-node", vdir)
    base = asyncio.run(baseline.run_baseline(vdir))
    yield vdir, result, base, before
    unlock(vdir)


def test_baseline_reports_drc_violations_with_project_file(nrf_version):
    vdir, result, base, _ = nrf_version
    assert base["has_project_file"] and not base["default_rules"]
    # all-track-errors mode gives the same count on every run; the plain mode varies from 45 to 56
    assert base["drc"]["violations"] == 111
    assert base["drc"]["schematic_parity"] > 0 and base["erc"]["violations"] == 0
    assert not base["warnings"]
    for step in ("kicad_version", "parse", "drc", "erc", "stats", "glb", "scene", "images"):
        assert base["steps"][step]["status"] == "ok", (step, base["steps"][step])
    for name in ("drc.json", "erc.json", "stats.json", "board.glb", "board.json", "candidates.json", "baseline.json", "top.png", "bottom.png", "scene/scene.json", "scene/F.Cu.svg"):
        assert (vdir / "artifacts" / name).is_file(), name
    assert json.loads((vdir / "artifacts" / "baseline.json").read_text())["drc"] == base["drc"]
    assert base["board"]["footprints"] == 50 and base["board"]["tracks"] == 234 and base["board"]["vias"] == 102
    assert base["detection"]["wireless"] and base["stackup"]["needs_confirmation"]


def test_candidates_have_contract_shape_and_resolving_marks(nrf_version):
    vdir, _, base, _ = nrf_version
    cands = json.loads((vdir / "artifacts" / "candidates.json").read_text())
    board = parse_board(vdir / "project" / "nrf52840_rf_iot_node.kicad_pcb")
    drc = [c for c in cands if c["rule"].startswith("drc_")]
    assert len(drc) == base["drc"]["violations"]
    ids = [c["id"] for c in cands]
    assert len(ids) == len(set(ids)) and all(i.startswith("candidate:") for i in ids)
    for c in cands:
        assert {"id", "rule", "title", "source", "measurements", "targets", "marks", "severity_hint"} <= set(c)
        assert c["source"] == "kicad" and c["severity_hint"] in ("high", "medium", "low", "info")
    for c in drc:
        assert c["targets"], c["id"]
        for t in c["targets"]:
            board.resolve(t)  # every DRC target resolves; no unknown ids
        assert c["marks"][0]["type"] == "pin" and c["marks"][0]["resolved"]
    # KiCad item uuids map to real tracks, vias and pads rather than points
    kinds = {t.split(":")[0] for c in drc for t in c["targets"]}
    assert kinds <= {"track", "via", "pad", "ref", "zone", "point"} and "track" in kinds
    clearance = next(c for c in drc if c["rule"] == "drc_clearance")
    assert clearance["measurements"]["actual"]["value"] < clearance["measurements"]["required"]["value"]


def test_sample_files_unchanged_and_original_readonly(nrf_version):
    vdir, result, _, before = nrf_version
    after = {p.relative_to(SAMPLES): hashlib.sha256(p.read_bytes()).hexdigest() for p in (SAMPLES / "nrf52840-node").rglob("*") if p.is_file()}
    assert before == after  # nothing was written next to the sample, KiCad ran on a copy
    kept = {p.relative_to(vdir / "original" / "tree"): hashlib.sha256(p.read_bytes()).hexdigest() for p in (vdir / "original" / "tree").rglob("*") if p.is_file()}
    assert {k: v for k, v in kept.items()} == {k.relative_to("nrf52840-node"): v for k, v in before.items()}
    # the unpacked project copy has no KiCad droppings either
    assert not list((vdir / "project").glob("*.kicad_prl"))
    assert (vdir / "work").is_dir()


def test_glb_coordinate_mapping(nrf_version):
    """GLB x = board x, GLB z = board y (both metres), GLB y = height; checked against pads."""
    vdir, _, _, _ = nrf_version
    gltf, blob = read_glb(vdir / "artifacts" / "board.glb")
    board = parse_board(vdir / "project" / "nrf52840_rf_iot_node.kicad_pcb")
    # footprint nodes are named by reference; translation = (x/1000, top height, y/1000)
    by_name = {n.get("name"): n for n in gltf["nodes"]}
    for f in board.footprints:
        node = by_name.get(f.ref)
        if node is None or "translation" not in node:
            continue
        t = node["translation"]
        assert t[0] * 1000 == pytest.approx(f.x, abs=1e-3) and t[2] * 1000 == pytest.approx(f.y, abs=1e-3), f.ref
    # the board body spans the outline in x and z
    body = mesh_vertices(gltf, blob, "_PCB")
    assert (body[:, 0].min(), body[:, 0].max()) == pytest.approx((board.bbox[0], board.bbox[2]), abs=1e-3)
    assert (body[:, 2].min(), body[:, 2].max()) == pytest.approx((board.bbox[1], board.bbox[3]), abs=1e-3)
    assert body[:, 1].min() == pytest.approx(0.0, abs=1e-3) and 1.4 < body[:, 1].max() < 1.7
    # every top-side SMD pad outline is found in the pad mesh at the same (x, z)
    pads = mesh_vertices(gltf, blob, "_pad")
    top = pads[pads[:, 1] > 1.0]
    matched = 0
    for p in board.pads:
        if "F.Cu" not in p.layers or p.type != "smd":
            continue
        xs = [q[0] for q in p.ring]
        ys = [q[1] for q in p.ring]
        e = 0.004
        sel = top[(top[:, 0] >= min(xs) - e) & (top[:, 0] <= max(xs) + e) & (top[:, 2] >= min(ys) - e) & (top[:, 2] <= max(ys) + e)]
        assert len(sel) >= 4, p.id
        assert abs(sel[:, 0].min() - min(xs)) < 0.01 and abs(sel[:, 0].max() - max(xs)) < 0.01, p.id
        assert abs(sel[:, 2].min() - min(ys)) < 0.01 and abs(sel[:, 2].max() - max(ys)) < 0.01, p.id
        matched += 1
    assert matched > 150
    # a footprint rotated 90 degrees (R5) turns about +Y by +90 degrees
    q = by_name["R5"]["rotation"]
    assert q == pytest.approx([0, math.sin(math.radians(45)), 0, math.cos(math.radians(45))], abs=1e-6)


def test_scene_files_match_viewbox(nrf_version):
    vdir, _, _, _ = nrf_version
    meta = json.loads((vdir / "artifacts" / "scene" / "scene.json").read_text())
    assert meta["view_box"] == pytest.approx([-2, -2, 70, 57]) and meta["units"] == "mm"
    assert "F.Cu" in meta["layers"] and "outline" in meta["layers"]


async def test_missing_project_file_warns_and_downgrades(tmp_path):
    src = tmp_path / "src"
    shutil.copytree(SAMPLES / "nrf52840-node", src)
    (src / "nrf52840_rf_iot_node.kicad_pro").unlink()
    vdir = tmp_path / "v"
    ingest(src, vdir)
    base = await baseline.run_baseline(vdir)
    unlock(vdir)
    assert base["default_rules"] and any("Default rules" in w for w in base["warnings"])
    assert base["drc"]["violations"] > 100  # default rules flag far more than the project's own rules
    cands = json.loads((vdir / "artifacts" / "candidates.json").read_text())
    assert all(c.get("downgraded") for c in cands if c["rule"].startswith("drc_"))
    assert not any(c["severity_hint"] == "high" for c in cands if c["rule"].startswith("drc_"))


async def test_patch_sample_without_schematic(tmp_path):
    vdir = tmp_path / "v"
    ingest(SAMPLES / "patch-2g5", vdir)
    base = await baseline.run_baseline(vdir, baseline.BaselineOptions(glb=False))
    unlock(vdir)
    assert base["steps"]["erc"]["status"] == "skipped" and base["steps"]["glb"]["status"] == "skipped"
    assert base["steps"]["drc"]["status"] == "ok" and base["detection"]["antenna"]["type"] == "patch"
    assert base["default_rules"]


async def test_failed_step_does_not_stop_the_rest(tmp_path, monkeypatch):
    vdir = tmp_path / "v"
    ingest(SAMPLES / "patch-2g5", vdir)

    async def boom(*a, **k):
        raise kicad_cli.KicadError("stats_failed", "KiCad could not complete stats", "stderr text")

    monkeypatch.setattr(kicad_cli, "stats", boom)
    base = await baseline.run_baseline(vdir, baseline.BaselineOptions(glb=False))
    unlock(vdir)
    assert base["steps"]["stats"]["status"] == "failed" and "stderr text" in base["steps"]["stats"]["error"]
    assert base["steps"]["drc"]["status"] == "ok" and base["steps"]["scene"]["status"] == "ok"


async def test_kicad_missing_still_gives_scene_and_candidates_empty(tmp_path, monkeypatch):
    vdir = tmp_path / "v"
    ingest(SAMPLES / "patch-2g5", vdir)
    monkeypatch.setenv("KICAD_CLI_PATH", str(tmp_path / "nokicad"))
    monkeypatch.setattr(kicad_cli, "_version_cache", None)
    base = await baseline.run_baseline(vdir)
    unlock(vdir)
    assert base["steps"]["kicad_version"]["status"] == "failed"
    assert base["steps"]["drc"]["status"] == "skipped" and base["steps"]["scene"]["status"] == "ok"
    assert any("KiCad unavailable" in w for w in base["warnings"]) and base["candidates"]["count"] == 0


def test_measurement_parser():
    m = baseline._measurements("Clearance violation (netclass 'Default' clearance 0.2000 mm; actual 0.1561 mm)")
    assert m["required"]["value"] == 0.2 and m["actual"]["value"] == 0.1561
    assert baseline._measurements("Items not allowed") == {}
