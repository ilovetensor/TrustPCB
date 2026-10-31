from __future__ import annotations

import json
import shutil

from trustpcb.analyzers import ANALYZER_NAMES, AnalyzerContext, run_all, run_for_version

from .conftest import NRF_PCB


def _by_rule(result):
    out: dict[str, int] = {}
    for c in result["candidates"]:
        out[c["rule"]] = out.get(c["rule"], 0) + 1
    return out


def test_nrf_all_analyzers_run_fast_and_deterministic(nrf_board):
    a = run_all(nrf_board, AnalyzerContext())
    b = run_all(nrf_board, AnalyzerContext())
    assert set(a["status"]) == set(ANALYZER_NAMES)
    assert all(s["status"] == "ok" and s["seconds"] < 2 for s in a["status"].values()), a["status"]
    assert [c["id"] for c in a["candidates"]] == [c["id"] for c in b["candidates"]]
    assert json.dumps(a["candidates"], sort_keys=True) == json.dumps(b["candidates"], sort_keys=True)
    rules = _by_rule(a)
    for rule in ("decoupling", "track_detour", "rf_path", "impedance", "return_path", "zones"):
        assert rules.get(rule, 0) >= 1, rules
    ids = [c["id"] for c in a["candidates"]]
    assert len(ids) == len(set(ids))
    for c in a["candidates"]:
        assert c["source"] == "analyzer" and c["marks"] and c["targets"]
        for m in c["marks"]:
            assert m["resolved"], (c["id"], m)


def test_nrf_known_findings(nrf_board):
    r = run_all(nrf_board, AnalyzerContext())
    dec = {c["capacitor"] for c in r["candidates"] if c["rule"] == "decoupling"}
    assert {"C5", "C10", "C9"} <= dec
    rf = next(c for c in r["candidates"] if c["rule"] == "rf_path")
    assert rf["measurements"]["width_changes"]["value"] >= 1 and "L1" in rf["details"][0]
    # the antenna's own copper is not an intruder in its keepout
    assert _by_rule(r).get("antenna_keepout", 0) == 0
    assert r["overlays"]["impedance"]["tracks"]


def test_patch_board(patch_board):
    r = run_all(patch_board, AnalyzerContext())
    assert all(s["status"] == "ok" for s in r["status"].values())
    imp = {i["track"]: i for i in r["overlays"]["impedance"]["tracks"]}
    feed = imp["track:b919f2c4"]
    assert abs(feed["deviation_pct"]) < 10  # the 3.05 mm feed is a 50 ohm line
    # the feed ends in the patch pour: not an open stub
    rf = next(c for c in r["candidates"] if c["rule"] == "rf_path")
    assert rf["measurements"]["open_end_stubs"]["value"] == 0
    assert rf["measurements"]["dnp_stubs"]["value"] == 1  # C2 (DNP_SHUNT) is not fitted: its pad hangs on the line


def test_run_for_version_writes_file(tmp_path, nrf_board):
    art = tmp_path / "artifacts"
    art.mkdir()
    (art / "board.json").write_text(json.dumps(nrf_board.to_dict()))
    (tmp_path / "project").mkdir()  # the project file carries the net classes the netclass_width rule reads
    shutil.copy(NRF_PCB.with_suffix(".kicad_pro"), tmp_path / "project" / NRF_PCB.with_suffix(".kicad_pro").name)
    shutil.copy(NRF_PCB.with_suffix(".kicad_sch"), tmp_path / "project" / NRF_PCB.with_suffix(".kicad_sch").name)  # the per-pin decoupling check reads supply pins from the schematic
    res = run_for_version(tmp_path, AnalyzerContext(target_ohms=50.0))
    data = json.loads((art / "analyzers.json").read_text())
    assert set(data) >= {"candidates", "overlays", "status", "seconds", "registrations"}
    assert len(data["candidates"]) == len(res["candidates"]) > 0
    assert data["registrations"]["zone_roles"]
    # board.json round trip gives the same findings as the parsed board
    direct = run_all(nrf_board, AnalyzerContext())
    assert [c["id"] for c in data["candidates"]] == [c["id"] for c in direct["candidates"]]
    assert not list(art.glob("*.tmp"))


def test_run_for_version_parses_project_when_no_board_json(tmp_path, patch_board):
    import shutil

    from .conftest import PATCH_PCB

    (tmp_path / "project").mkdir()
    shutil.copy(PATCH_PCB, tmp_path / "project" / PATCH_PCB.name)
    res = run_for_version(tmp_path)
    assert (tmp_path / "artifacts" / "analyzers.json").is_file()
    assert res["status"]["impedance"]["status"] == "ok"


def test_nrf_antenna_keepout_is_exempt_from_the_transmission_line_rules(nrf_board):
    """A printed antenna must stay free of ground: no return-path, detour or impedance candidate may cover its keep-out."""
    from shapely.geometry import LineString, Point, box

    keepout = nrf_board.keepouts[0]
    zone = box(*[min(p[0] for p in keepout.ring), min(p[1] for p in keepout.ring), max(p[0] for p in keepout.ring), max(p[1] for p in keepout.ring)])
    tracks = {t.id: t for t in nrf_board.tracks}
    r = run_all(nrf_board, AnalyzerContext())
    for c in r["candidates"]:
        if c["rule"] not in ("return_path", "track_detour", "impedance"):
            continue
        if c.get("position"):
            assert not zone.contains(Point(c["position"])), (c["id"], c["title"])
        for tid in c["targets"]:
            if tid in tracks:
                mid = LineString(tracks[tid].points()).interpolate(0.5, normalized=True)
                assert not zone.contains(mid), (c["id"], tid, c["title"])
    titles = [c["title"] for c in r["candidates"] if c["rule"] in ("return_path", "track_detour")]
    assert not any("ANT_FEED" in t for t in titles), titles
