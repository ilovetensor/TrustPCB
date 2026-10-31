from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from trustpcb.pcb import kicad_cli as K
from trustpcb.pcb.model import parse_board

needs_kicad = pytest.mark.skipif(K.find_kicad_cli() is None, reason="kicad-cli not installed")


def fake_exe(path: Path, body: str = "echo 10.0.5") -> Path:
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_discovery_order_configured_wins_and_wrong_setting_finds_nothing(tmp_path):
    exe = fake_exe(tmp_path / "mykicad")
    other = fake_exe(tmp_path / "kicad-cli")
    which = lambda name: str(other) if name == "kicad-cli" else None  # noqa: E731
    assert K.find_kicad_cli(environ={"KICAD_CLI_PATH": str(exe)}, platform="linux", which=which) == str(exe)
    # a wrong KICAD_CLI_PATH is a choice: it is not passed over for the one on PATH
    assert K.find_kicad_cli(environ={"KICAD_CLI_PATH": str(tmp_path / "nope")}, platform="linux", which=which) is None
    # a bare name is looked up on PATH
    assert K.find_kicad_cli(environ={"KICAD_CLI_PATH": "kicad-cli"}, platform="linux", which=which) == str(other)
    assert K.find_kicad_cli(environ={}, platform="linux", which=which) == str(other)
    assert K.find_kicad_cli(environ={}, platform="linux", which=lambda n: None) is None


def test_standard_locations_by_platform(tmp_path):
    assert [str(p) for p in K.standard_locations({}, "darwin")] == list(K.MACOS_LOCATIONS)
    assert K.standard_locations({}, "linux") == ()
    pf = tmp_path / "Program Files" / "KiCad"
    for v in ("8.0", "10.0", "9.0", "notaversion"):
        (pf / v / "bin").mkdir(parents=True)
    got = K.windows_locations({"ProgramFiles": str(tmp_path / "Program Files")})
    assert [p.parts[-3] for p in got] == ["10.0", "9.0", "8.0"]  # newest first


def test_version_parse_and_support():
    assert K.parse_version("10.0.5\n") == (10, 0, 5)
    assert K.parse_version("9.0") == (9, 0, 0)
    assert K.supports((9, 0, 1)) and not K.supports((8, 0, 9))
    with pytest.raises(K.KicadUnavailable):
        K.parse_version("garbage")


async def test_version_old_and_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(K, "_version_cache", None)
    monkeypatch.setenv("KICAD_CLI_PATH", str(fake_exe(tmp_path / "old", "echo 8.0.4")))
    with pytest.raises(K.KicadUnavailable) as e:
        await K.version()
    assert e.value.code == "unsupported_version"
    monkeypatch.setenv("KICAD_CLI_PATH", str(tmp_path / "missing"))
    with pytest.raises(K.KicadUnavailable) as e:
        await K.version()
    assert e.value.code == "missing" and "KICAD_CLI_PATH" in str(e.value)


async def test_timeout_kills_process(tmp_path, monkeypatch):
    monkeypatch.setenv("KICAD_CLI_PATH", str(fake_exe(tmp_path / "slow", "sleep 30")))
    with pytest.raises(K.KicadError) as e:
        await K.run(["version"], timeout=0.3)
    assert e.value.code == "timeout"


async def test_kicad_does_not_inherit_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("KICAD_CLI_PATH", str(fake_exe(tmp_path / "envdump", "env")))
    monkeypatch.setenv("MODEL_API_KEY", "sk-secret-value")
    monkeypatch.setenv("TRUSTPCB_TOKEN", "launch-token-value")
    monkeypatch.setenv("SOME_OTHER_SECRET", "other-secret-value")
    monkeypatch.setenv("KICAD10_3DMODEL_DIR", "/opt/models")
    monkeypatch.setenv("LC_ALL", "C")
    result = await K.run(["version"])
    names = {line.split("=", 1)[0] for line in result.stdout.splitlines() if "=" in line}
    assert not {"MODEL_API_KEY", "TRUSTPCB_TOKEN", "SOME_OTHER_SECRET"} & names
    assert "secret" not in result.stdout and "launch-token" not in result.stdout
    assert {"PATH", "KICAD10_3DMODEL_DIR", "KICAD_CLI_PATH", "LC_ALL"} <= names


def test_child_environment_allow_list():
    from trustpcb.pcb.env import child_environment

    source = {"PATH": "/bin", "HOME": "/h", "XDG_CONFIG_HOME": "/c", "KICAD_X": "1", "DOCKER_HOST": "unix:///d", "API_KEY": "k", "TOKEN": "t"}
    assert child_environment(environ=source) == {"PATH": "/bin", "HOME": "/h", "XDG_CONFIG_HOME": "/c"}
    assert child_environment(extra_prefixes=("KICAD", "DOCKER"), extra_names=("TOKEN",), environ=source) == {
        "PATH": "/bin", "HOME": "/h", "XDG_CONFIG_HOME": "/c", "KICAD_X": "1", "DOCKER_HOST": "unix:///d", "TOKEN": "t"}


async def test_failed_command_raises_with_detail(tmp_path, monkeypatch):
    monkeypatch.setenv("KICAD_CLI_PATH", str(fake_exe(tmp_path / "bad", "echo boom >&2; exit 3")))
    pcb = tmp_path / "w" / "a.kicad_pcb"
    pcb.parent.mkdir()
    pcb.write_text("x")
    with pytest.raises(K.KicadError) as e:
        await K.drc(pcb, tmp_path / "o" / "drc.json")
    assert e.value.code == "drc_failed" and "boom" in e.value.detail


async def test_refuses_the_preserved_original(tmp_path):
    pcb = tmp_path / "original" / "a.kicad_pcb"
    pcb.parent.mkdir()
    pcb.write_text("x")
    with pytest.raises(ValueError):
        await K.drc(pcb, tmp_path / "drc.json")
    with pytest.raises(ValueError):
        await K.export_glb(pcb, tmp_path / "x.glb")


@needs_kicad
async def test_real_version_drc_erc_stats_netlist(nrf_copy, tmp_path):
    v = await K.version()
    assert v[0] >= 9
    pcb = nrf_copy / "nrf52840_rf_iot_node.kicad_pcb"
    sch = nrf_copy / "nrf52840_rf_iot_node.kicad_sch"
    r = await K.drc(pcb, tmp_path / "drc.json")
    assert r.returncode in (0, 5)
    d = json.loads((tmp_path / "drc.json").read_text())
    # KiCad's default DRC varies a little between runs of the same board (parallel checks)
    assert 40 <= len(d["violations"]) <= 60
    assert len(d["schematic_parity"]) > 0  # a .kicad_sch sits beside the board, so parity ran
    await K.erc(sch, tmp_path / "erc.json")
    assert "sheets" in json.loads((tmp_path / "erc.json").read_text())
    await K.stats(pcb, tmp_path / "stats.json")
    assert json.loads((tmp_path / "stats.json").read_text())


@needs_kicad
async def test_all_track_errors_is_repeatable(nrf_copy, tmp_path):
    counts = set()
    for i in range(2):
        await K.drc(nrf_copy / "nrf52840_rf_iot_node.kicad_pcb", tmp_path / f"d{i}.json", all_track_errors=True)
        counts.add(len(json.loads((tmp_path / f"d{i}.json").read_text())["violations"]))
    assert len(counts) == 1 and counts.pop() > 48


@needs_kicad
async def test_refill_zones_on_a_copy_only(nrf_copy, tmp_path):
    pcb = nrf_copy / "nrf52840_rf_iot_node.kicad_pcb"
    text = pcb.read_text()
    # drop every stored fill so the board is as the compiler would leave it
    out, i = [], 0
    while True:
        j = text.find("(filled_polygon", i)
        if j < 0:
            out.append(text[i:])
            break
        out.append(text[i:j])
        depth, k = 0, j
        while True:
            depth += (text[k] == "(") - (text[k] == ")")
            k += 1
            if depth == 0:
                break
        i = k
    pcb.write_text("".join(out))
    before = pcb.read_bytes()
    assert not any(z.filled for z in parse_board(pcb).zones)
    refilled = await K.refill_zones(pcb, tmp_path / "fill")
    assert refilled.parent == tmp_path / "fill" and pcb.read_bytes() == before  # the input is untouched
    b = parse_board(refilled)
    assert all(z.filled for z in b.zones)
    # and the pipeline merges that fill into the original model by order
    from trustpcb.pcb.baseline import apply_zone_fill

    board = parse_board(pcb)
    assert apply_zone_fill(board, b) == 2 and all(z.filled for z in board.zones)


@needs_kicad
async def test_glb_export(nrf_copy, tmp_path):
    pcb = nrf_copy / "nrf52840_rf_iot_node.kicad_pcb"
    await K.export_glb(pcb, tmp_path / "b.glb")
    assert (tmp_path / "b.glb").read_bytes()[:4] == b"glTF"


@needs_kicad
async def test_original_files_untouched_by_kicad(nrf_copy, tmp_path):
    before = {p: p.read_bytes() for p in nrf_copy.rglob("*") if p.is_file()}
    work = K.prepare_workdir(nrf_copy, tmp_path / "work")
    await K.drc(work / "nrf52840_rf_iot_node.kicad_pcb", tmp_path / "d.json")
    after = {p: p.read_bytes() for p in nrf_copy.rglob("*") if p.is_file()}
    assert before == after
    assert os.path.isdir(work)
