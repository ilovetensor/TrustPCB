"""Safe intake of an uploaded KiCad project (spec section 5).

``ingest(source, version_dir)`` takes a zip file or a folder and lays out one version::

    version_dir/
      original/     the upload exactly as received (read-only): ``<name>.zip`` or ``tree/``
      project/      the unpacked, validated project files; the only thing the pipeline reads
      ingest.json   sha256 of the upload, file list, totals, warnings and skipped files

No user file is ever executed. KiCad is later run on a copy of ``project/``, never on either
directory here. Hostile input is rejected before anything is written to ``project/``.

Rules (each raises :class:`IntakeError` with a stable ``code``):

``too_large``       zip over 100 MB, more than 500 MB unpacked, or more than 5,000 files
``path_traversal``  absolute paths, ``..`` segments, drive letters, NUL bytes
``symlink``         symbolic links (in a zip or in a folder)
``unknown_type``    a file whose type is not on the allow list (see :data:`ALLOWED_SUFFIXES`)
``encrypted``       encrypted zip entries
``no_board``        no ``.kicad_pcb`` in the upload
``bad_archive``     not a readable zip

Editor and operating-system leftovers (``__MACOSX``, ``.DS_Store``, ``.git``, ``*-backups``, lock
and cache files) are skipped and listed in ``skipped`` rather than rejected.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import time
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

MAX_ZIP_BYTES = 100 * 1024 * 1024
MAX_UNPACKED_BYTES = 500 * 1024 * 1024
MAX_FILES = 5000

#: types that may appear in a project upload. Anything else is rejected.
ALLOWED_SUFFIXES = frozenset(
    {
        # KiCad
        ".kicad_pcb", ".kicad_pro", ".kicad_sch", ".kicad_sym", ".kicad_mod", ".kicad_dru",
        ".kicad_prl", ".kicad_wks", ".kicad_jobset", ".net", ".sch", ".lib", ".dcm", ".pretty",
        # fabrication and 3D (read as data only)
        ".gbr", ".gtl", ".gbl", ".gts", ".gbs", ".gto", ".gbo", ".gko", ".gm1", ".gtp", ".gbp", ".drl", ".gbrjob",
        ".step", ".stp", ".wrl", ".glb", ".gltf", ".stl",
        # documents and data
        ".pdf", ".txt", ".md", ".csv", ".json", ".png", ".jpg", ".jpeg", ".svg", ".xml", ".yaml", ".yml",
        ".html", ".rst", ".log", ".pos", ".rpt",
    }
)
#: files with no suffix that KiCad projects carry
ALLOWED_NAMES = frozenset({"fp-lib-table", "sym-lib-table", "license", "licence", "readme", "notice", "authors"})
#: extensions that are never accepted, even if someone adds them to an allow list by mistake
FORBIDDEN_SUFFIXES = frozenset({".exe", ".dll", ".so", ".dylib", ".sh", ".bat", ".cmd", ".ps1", ".py", ".js", ".mjs", ".jar", ".app", ".com", ".msi", ".scr", ".vbs", ".pl", ".rb"})
_JUNK_DIRS = frozenset({"__macosx", ".git", ".svn", ".hg", ".idea", ".vscode", "__pycache__", "node_modules"})
_JUNK_NAMES = frozenset({".ds_store", "thumbs.db", "desktop.ini", "fp-info-cache", "sym-info-cache"})


class IntakeError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class IngestResult:
    version_dir: Path
    project_dir: Path
    original_path: Path
    kind: str  # zip | dir
    sha256: str
    pcb: Path | None
    pro: Path | None
    sch: Path | None
    files: list[dict[str, Any]]
    total_bytes: int
    warnings: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    other_boards: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        rel = lambda p: str(p.relative_to(self.project_dir)) if p else None  # noqa: E731
        return {
            "kind": self.kind,
            "sha256": self.sha256,
            "original": str(self.original_path.relative_to(self.version_dir)),
            "pcb": rel(self.pcb),
            "pro": rel(self.pro),
            "sch": rel(self.sch),
            "has_project_file": self.pro is not None,
            "files": self.files,
            "file_count": len(self.files),
            "total_bytes": self.total_bytes,
            "warnings": self.warnings,
            "skipped": self.skipped,
            "other_boards": self.other_boards,
        }


# ------------------------------------------------------------------------------ rules


def _is_junk(parts: tuple[str, ...]) -> bool:
    if any(p.lower() in _JUNK_DIRS or p.lower().endswith("-backups") for p in parts[:-1]):
        return True
    name = parts[-1].lower()
    return name in _JUNK_NAMES or name.startswith("._") or name.endswith(("~", ".lck", ".bak", ".tmp")) or name.startswith("~")


def _check_type(parts: tuple[str, ...], original: str) -> None:
    name = parts[-1]
    suffix = Path(name).suffix.lower()
    if suffix in FORBIDDEN_SUFFIXES:
        raise IntakeError("unknown_type", f"executable or script file not accepted: {original}")
    if suffix:
        if suffix in ALLOWED_SUFFIXES:
            return
    elif name.lower() in ALLOWED_NAMES:
        return
    raise IntakeError("unknown_type", f"file type not accepted in a project upload: {original}")


def _normalize(name: str) -> tuple[str, ...]:
    """A safe relative path as parts, or raise ``path_traversal``."""
    if "\0" in name:
        raise IntakeError("path_traversal", f"NUL byte in path: {name!r}")
    cleaned = name.replace("\\", "/")
    if cleaned.startswith("/") or (len(cleaned) > 1 and cleaned[1] == ":"):
        raise IntakeError("path_traversal", f"absolute path in upload: {name}")
    parts = tuple(p for p in PurePosixPath(cleaned).parts if p not in ("", "."))
    if not parts:
        raise IntakeError("path_traversal", f"empty path: {name!r}")
    if ".." in parts:
        raise IntakeError("path_traversal", f"path escapes the project: {name}")
    return parts


def _strip_wrapper(entries: list[tuple[tuple[str, ...], Any]]) -> list[tuple[tuple[str, ...], Any]]:
    """If every file sits under one top-level folder, drop it (zips of a folder)."""
    tops = {parts[0] for parts, _ in entries if len(parts) > 1}
    if entries and all(len(parts) > 1 for parts, _ in entries) and len(tops) == 1:
        return [(parts[1:], item) for parts, item in entries]
    return entries


# ----------------------------------------------------------------------------- hashing


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _tree_hash(files: list[tuple[str, str]]) -> str:
    h = hashlib.sha256()
    for rel, digest in sorted(files):
        h.update(rel.encode())
        h.update(b"\0")
        h.update(digest.encode())
        h.update(b"\n")
    return h.hexdigest()


def _make_readonly(path: Path) -> None:
    for dirpath, dirnames, filenames in os.walk(path, topdown=False):
        for f in filenames:
            os.chmod(os.path.join(dirpath, f), stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        os.chmod(dirpath, stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)


def unlock(version_dir: str | Path) -> None:
    """Make an ingested version deletable again (``original/`` is read-only)."""
    for dirpath, dirnames, filenames in os.walk(version_dir):
        os.chmod(dirpath, 0o755)
        for f in filenames:
            try:
                os.chmod(os.path.join(dirpath, f), 0o644)
            except OSError:
                pass


# --------------------------------------------------------------------------- ingestion


def ingest(source: str | Path, version_dir: str | Path) -> IngestResult:
    """Validate and unpack ``source`` (a ``.zip`` file or a folder) into ``version_dir``."""
    source = Path(source)
    version_dir = Path(version_dir)
    if version_dir.exists() and any(version_dir.iterdir()):
        raise IntakeError("exists", f"version directory is not empty: {version_dir}")
    if source.is_symlink():
        raise IntakeError("symlink", "the upload itself is a symbolic link")
    if source.is_dir():
        kind = "dir"
    elif source.is_file():
        kind = "zip"
    else:
        raise IntakeError("bad_archive", f"no such upload: {source}")

    version_dir.mkdir(parents=True, exist_ok=True)
    project_dir = version_dir / "project"
    original_dir = version_dir / "original"
    try:
        if kind == "zip":
            result = _ingest_zip(source, version_dir, project_dir, original_dir)
        else:
            result = _ingest_dir(source, version_dir, project_dir, original_dir)
    except BaseException:
        unlock(version_dir)
        shutil.rmtree(version_dir, ignore_errors=True)
        raise
    (version_dir / "ingest.json").write_text(json.dumps({**result.to_dict(), "ingested_at": time.time()}, indent=2))
    return result


def _finish(result: IngestResult) -> IngestResult:
    boards = sorted(p for p in result.project_dir.rglob("*.kicad_pcb") if p.is_file())
    if not boards:
        raise IntakeError("no_board", "the upload contains no .kicad_pcb file")
    # Prefer the board that has a project file of the same name, then the shallowest.
    boards.sort(key=lambda p: (not p.with_suffix(".kicad_pro").is_file(), len(p.relative_to(result.project_dir).parts), str(p)))
    pcb = boards[0]
    result.pcb = pcb
    result.other_boards = [str(p.relative_to(result.project_dir)) for p in boards[1:]]
    if result.other_boards:
        result.warnings.append(f"upload has {len(boards)} boards; using {pcb.name}")
    pro = pcb.with_suffix(".kicad_pro")
    if not pro.is_file():
        pros = sorted(pcb.parent.glob("*.kicad_pro"))
        pro = pros[0] if len(pros) == 1 else None
    result.pro = pro if pro and pro.is_file() else None
    sch = pcb.with_suffix(".kicad_sch")
    if not sch.is_file():
        schs = sorted(pcb.parent.glob("*.kicad_sch"))
        sch = schs[0] if schs else None
    result.sch = sch if sch and sch.is_file() else None
    if result.pro is None:
        result.warnings.append("no .kicad_pro: KiCad runs with default rules, so rule results are less reliable")
    if result.sch is None:
        result.warnings.append("no .kicad_sch: no ERC, and part values come from the board only")
    return result


def _ingest_zip(source: Path, version_dir: Path, project_dir: Path, original_dir: Path) -> IngestResult:
    size = source.stat().st_size
    if size > MAX_ZIP_BYTES:
        raise IntakeError("too_large", f"zip is {size / 1e6:.0f} MB; the limit is {MAX_ZIP_BYTES // (1024 * 1024)} MB")
    try:
        archive = zipfile.ZipFile(source)
    except (zipfile.BadZipFile, OSError) as exc:
        raise IntakeError("bad_archive", f"not a readable zip file: {exc}") from exc
    skipped: list[str] = []
    with archive:
        entries: list[tuple[tuple[str, ...], zipfile.ZipInfo]] = []
        total = 0
        for info in archive.infolist():
            if info.is_dir():
                parts = _normalize(info.filename.rstrip("/") or ".") if info.filename.strip("/") else ()
                continue
            parts = _normalize(info.filename)
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise IntakeError("symlink", f"symbolic link in upload: {info.filename}")
            if info.flag_bits & 0x1:
                raise IntakeError("encrypted", f"encrypted entry in upload: {info.filename}")
            if mode and not (stat.S_ISREG(mode) or stat.S_IFMT(mode) == 0):
                raise IntakeError("unknown_type", f"special file in upload: {info.filename}")
            total += info.file_size
            if total > MAX_UNPACKED_BYTES:
                raise IntakeError("too_large", f"unpacked size exceeds {MAX_UNPACKED_BYTES // (1024 * 1024)} MB")
            if len(entries) + 1 > MAX_FILES:
                raise IntakeError("too_large", f"more than {MAX_FILES} files")
            entries.append((parts, info))
        entries = _strip_wrapper(entries)
        keep: list[tuple[tuple[str, ...], zipfile.ZipInfo]] = []
        for parts, info in entries:
            if _is_junk(parts):
                skipped.append("/".join(parts))
                continue
            _check_type(parts, info.filename)
            keep.append((parts, info))
        project_dir.mkdir(parents=True)
        files: list[dict[str, Any]] = []
        written = 0
        for parts, info in keep:
            dest = project_dir.joinpath(*parts)
            if project_dir.resolve() not in dest.resolve().parents:
                raise IntakeError("path_traversal", f"path escapes the project: {info.filename}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            h = hashlib.sha256()
            n = 0
            try:
                with archive.open(info) as src, dest.open("wb") as out:
                    for chunk in iter(lambda: src.read(1 << 20), b""):
                        n += len(chunk)
                        written += len(chunk)
                        # Declared sizes can lie; count what actually comes out.
                        if written > MAX_UNPACKED_BYTES or n > info.file_size + 1024:
                            raise IntakeError("too_large", "archive expands beyond its declared size")
                        h.update(chunk)
                        out.write(chunk)
            except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError, RuntimeError) as exc:
                raise IntakeError("bad_archive", f"corrupt or unsupported zip entry {info.filename}: {exc}") from exc
            files.append({"path": "/".join(parts), "size": n, "sha256": h.hexdigest()})
    original_dir.mkdir(parents=True)
    kept = original_dir / (source.name if source.suffix.lower() == ".zip" else source.name + ".zip")
    shutil.copyfile(source, kept)
    digest = _sha256_file(kept)
    _make_readonly(original_dir)
    result = IngestResult(version_dir, project_dir, kept, "zip", digest, None, None, None, files, written, skipped=skipped)
    return _finish(result)


def _ingest_dir(source: Path, version_dir: Path, project_dir: Path, original_dir: Path) -> IngestResult:
    entries: list[tuple[tuple[str, ...], Path]] = []
    total = 0
    for dirpath, dirnames, filenames in os.walk(source, followlinks=False):
        for d in list(dirnames):
            p = Path(dirpath, d)
            if p.is_symlink():
                raise IntakeError("symlink", f"symbolic link in upload: {p.relative_to(source)}")
        for f in filenames:
            p = Path(dirpath, f)
            rel = p.relative_to(source)
            if p.is_symlink():
                raise IntakeError("symlink", f"symbolic link in upload: {rel}")
            st = p.lstat()
            if not stat.S_ISREG(st.st_mode):
                raise IntakeError("unknown_type", f"special file in upload: {rel}")
            total += st.st_size
            if total > MAX_UNPACKED_BYTES:
                raise IntakeError("too_large", f"unpacked size exceeds {MAX_UNPACKED_BYTES // (1024 * 1024)} MB")
            entries.append((_normalize(str(rel)), p))
            if len(entries) > MAX_FILES:
                raise IntakeError("too_large", f"more than {MAX_FILES} files")
    skipped: list[str] = []
    keep = []
    for parts, p in entries:
        if _is_junk(parts):
            skipped.append("/".join(parts))
            continue
        _check_type(parts, "/".join(parts))
        keep.append((parts, p))
    tree = original_dir / "tree"
    project_dir.mkdir(parents=True)
    tree.mkdir(parents=True)
    files: list[dict[str, Any]] = []
    hashes: list[tuple[str, str]] = []
    written = 0
    for parts, p in keep:
        for base in (tree,):
            d = base.joinpath(*parts)
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(p, d)
        digest = _sha256_file(p)
        hashes.append(("/".join(parts), digest))
    wrapped = _strip_wrapper([(parts, p) for parts, p in keep])
    for parts, p in wrapped:
        dest = project_dir.joinpath(*parts)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(p, dest)
        size = dest.stat().st_size
        written += size
        files.append({"path": "/".join(parts), "size": size, "sha256": _sha256_file(dest)})
    digest = _tree_hash(hashes)
    _make_readonly(original_dir)
    result = IngestResult(version_dir, project_dir, tree, "dir", digest, None, None, None, files, written, skipped=skipped)
    return _finish(result)
