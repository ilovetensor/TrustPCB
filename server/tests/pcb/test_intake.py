from __future__ import annotations

import hashlib
import os
import stat
import zipfile
from pathlib import Path

import pytest

from trustpcb.intake import upload
from trustpcb.intake.upload import IntakeError, ingest, unlock

SAMPLES = Path(__file__).resolve().parents[3] / "samples"
NRF = SAMPLES / "nrf52840-node"
PCB_TEXT = "(kicad_pcb (version 20240108) (layers (0 \"F.Cu\" signal)))"


def make_zip(path: Path, entries: dict[str, bytes | str], *, symlink: str | None = None) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        for name, data in entries.items():
            z.writestr(name, data)
        if symlink:
            info = zipfile.ZipInfo(symlink)
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            z.writestr(info, "/etc/passwd")
    return path


def code_of(fn, *a, **k):
    with pytest.raises(IntakeError) as e:
        fn(*a, **k)
    return e.value.code


def test_zip_ingest_layout_hash_readonly(tmp_path):
    z = make_zip(tmp_path / "board.zip", {"p/a.kicad_pcb": PCB_TEXT, "p/a.kicad_pro": "{}", "p/a.kicad_sch": "(kicad_sch)", "p/fp-lib-table": "(fp_lib_table)"})
    r = ingest(z, tmp_path / "v1")
    assert r.kind == "zip" and r.pcb.name == "a.kicad_pcb" and r.pro and r.sch
    assert r.project_dir.joinpath("a.kicad_pcb").is_file()  # wrapper folder "p/" stripped
    assert r.sha256 == hashlib.sha256(z.read_bytes()).hexdigest()
    kept = r.original_path
    assert kept.read_bytes() == z.read_bytes()
    assert not os.access(kept, os.W_OK)
    assert (tmp_path / "v1" / "ingest.json").is_file()
    d = r.to_dict()
    assert d["has_project_file"] is True and d["warnings"] == []
    unlock(tmp_path / "v1")


def test_missing_pro_and_sch_warn(tmp_path):
    z = make_zip(tmp_path / "b.zip", {"a.kicad_pcb": PCB_TEXT})
    r = ingest(z, tmp_path / "v")
    assert r.pro is None and r.sch is None
    assert any(".kicad_pro" in w for w in r.warnings) and any(".kicad_sch" in w for w in r.warnings)
    assert r.to_dict()["has_project_file"] is False


@pytest.mark.parametrize("name", ["../evil.kicad_pcb", "a/../../evil.kicad_pcb", "/abs/evil.kicad_pcb", "C:/x/evil.kicad_pcb", "a\\..\\..\\evil.kicad_pcb"])
def test_path_traversal_rejected(tmp_path, name):
    z = make_zip(tmp_path / "t.zip", {"ok.kicad_pcb": PCB_TEXT, name: "x"})
    assert code_of(ingest, z, tmp_path / "v") == "path_traversal"
    assert not (tmp_path / "v").exists() and not (tmp_path / "evil.kicad_pcb").exists()


def test_symlink_in_zip_rejected(tmp_path):
    z = make_zip(tmp_path / "s.zip", {"a.kicad_pcb": PCB_TEXT}, symlink="link.kicad_sch")
    assert code_of(ingest, z, tmp_path / "v") == "symlink"


def test_symlink_in_folder_rejected(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.kicad_pcb").write_text(PCB_TEXT)
    os.symlink("/etc/passwd", src / "l.txt")
    assert code_of(ingest, src, tmp_path / "v") == "symlink"
    d = tmp_path / "src2"
    (d / "real").mkdir(parents=True)
    (d / "real" / "a.kicad_pcb").write_text(PCB_TEXT)
    os.symlink(d / "real", d / "alias")
    assert code_of(ingest, d, tmp_path / "v2") == "symlink"


@pytest.mark.parametrize("name", ["run.sh", "evil.exe", "x.py", "lib.dll", "weird.xyz", "noextension"])
def test_unknown_and_executable_types_rejected(tmp_path, name):
    z = make_zip(tmp_path / "u.zip", {"a.kicad_pcb": PCB_TEXT, name: "x"})
    assert code_of(ingest, z, tmp_path / "v") == "unknown_type"


def test_junk_is_skipped_not_rejected(tmp_path):
    z = make_zip(tmp_path / "j.zip", {"a.kicad_pcb": PCB_TEXT, "__MACOSX/._a": "x", ".DS_Store": "x", "a-backups/a-2020.zip": "x", "fp-info-cache": "x", "a.kicad_prl": "{}"})
    r = ingest(z, tmp_path / "v")
    assert {"__MACOSX/._a", ".DS_Store", "a-backups/a-2020.zip", "fp-info-cache"} <= set(r.skipped)
    assert sorted(f["path"] for f in r.files) == ["a.kicad_pcb", "a.kicad_prl"]


def test_limits(tmp_path, monkeypatch):
    z = make_zip(tmp_path / "l.zip", {"a.kicad_pcb": PCB_TEXT, "b.txt": "x" * 2000})
    monkeypatch.setattr(upload, "MAX_UNPACKED_BYTES", 1000)
    assert code_of(ingest, z, tmp_path / "v1") == "too_large"
    monkeypatch.setattr(upload, "MAX_UNPACKED_BYTES", 10**9)
    monkeypatch.setattr(upload, "MAX_FILES", 1)
    assert code_of(ingest, z, tmp_path / "v2") == "too_large"
    monkeypatch.setattr(upload, "MAX_FILES", 5000)
    monkeypatch.setattr(upload, "MAX_ZIP_BYTES", 10)
    assert code_of(ingest, z, tmp_path / "v3") == "too_large"


def test_lying_zip_header_is_caught(tmp_path, monkeypatch):
    z = make_zip(tmp_path / "bomb.zip", {"a.kicad_pcb": PCB_TEXT, "big.txt": "A" * 5_000_000})
    # zip headers carry the size; one that claims less than it really expands to must still stop
    real_infolist = zipfile.ZipFile.infolist

    def lying(self):
        items = real_infolist(self)
        for i in items:
            if i.filename == "big.txt":
                i.file_size = 10
        return items

    monkeypatch.setattr(zipfile.ZipFile, "infolist", lying)
    assert code_of(ingest, z, tmp_path / "v") in ("too_large", "bad_archive")


def test_not_a_zip_and_no_board(tmp_path):
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"not a zip")
    assert code_of(ingest, bad, tmp_path / "v") == "bad_archive"
    z = make_zip(tmp_path / "nb.zip", {"readme.txt": "hi"})
    assert code_of(ingest, z, tmp_path / "v2") == "no_board"
    assert code_of(ingest, tmp_path / "missing.zip", tmp_path / "v3") == "bad_archive"


def test_folder_ingest_keeps_original_readonly_and_hashed(tmp_path):
    r = ingest(NRF, tmp_path / "v")
    assert r.kind == "dir" and r.pcb.name == "nrf52840_rf_iot_node.kicad_pcb" and r.pro and r.sch
    assert len(r.sha256) == 64
    assert (r.original_path / "nrf52840_rf_iot_node.kicad_pcb").is_file()
    assert not os.access(r.original_path / "nrf52840_rf_iot_node.kicad_pcb", os.W_OK)
    assert any(f["path"].startswith("nrf52840_rf_iot_node.pretty/") for f in r.files)
    # same upload, same hash
    r2 = ingest(NRF, tmp_path / "v2")
    assert r2.sha256 == r.sha256
    unlock(tmp_path / "v")
    unlock(tmp_path / "v2")


def test_existing_version_dir_not_overwritten(tmp_path):
    z = make_zip(tmp_path / "a.zip", {"a.kicad_pcb": PCB_TEXT})
    ingest(z, tmp_path / "v")
    assert code_of(ingest, z, tmp_path / "v") == "exists"
