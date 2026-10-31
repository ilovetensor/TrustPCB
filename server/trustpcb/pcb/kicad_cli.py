"""Async wrappers around ``kicad-cli`` (KiCad is only ever run as a separate program).

Discovery order, version rule and the default DRC, ERC and export calls are KiCad's own;
no custom checks are included here.

Discovery order for the executable:

1. ``KICAD_CLI_PATH``. A setting is a choice: a wrong value is not passed over for another
   KiCad, it finds nothing. A bare name is looked up on ``PATH``.
2. Where KiCad's installers put it: on macOS the app bundle, then the two Homebrew prefixes;
   on Windows every ``<Program Files>\\KiCad\\<version>\\bin``, newest first.
3. ``PATH``.

KiCad needs version 9 or newer. Every function here must be handed paths inside a *copy* of
the project (``prepare_workdir`` makes one); a path inside an ``original`` directory is refused.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from .env import child_environment

ENVIRONMENT_VARIABLE = "KICAD_CLI_PATH"
DEFAULT_NAME = "kicad-cli"
MACOS_LOCATIONS = (
    "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
    "/opt/homebrew/bin/kicad-cli",
    "/usr/local/bin/kicad-cli",
)
MIN_MAJOR = 9

#: strict budget for fast structural commands, generous one for zone refills and 3D work
DEFAULT_TIMEOUT = 30.0
SLOW_TIMEOUT = 180.0

_VERSION_FOLDER = re.compile(r"^(\d+)\.(\d+)$")
_VERSION = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")


class KicadError(RuntimeError):
    """A kicad-cli call failed. ``code`` is machine readable; ``detail`` carries stderr."""

    def __init__(self, code: str, message: str, detail: str = ""):
        super().__init__(message)
        self.code = code
        self.detail = detail


class KicadUnavailable(KicadError):
    """No usable kicad-cli (missing, wrong version)."""


@dataclass
class CliResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    seconds: float


# ----------------------------------------------------------------------------- discovery


def windows_locations(environ: Mapping[str, str]) -> tuple[Path, ...]:
    roots: list[str] = []
    for variable in ("ProgramW6432", "ProgramFiles"):
        value = environ.get(variable, "").strip()
        if value and value not in roots:
            roots.append(value)
    if not roots:
        roots.append("C:\\Program Files")
    found: list[tuple[tuple[int, int], Path]] = []
    for root in roots:
        kicad = Path(root) / "KiCad"
        try:
            names = os.listdir(kicad)
        except OSError:
            continue
        for name in names:
            version = _VERSION_FOLDER.match(name)
            if version:
                found.append(((int(version.group(1)), int(version.group(2))), kicad / name / "bin" / "kicad-cli.exe"))
    found.sort(key=lambda item: item[0], reverse=True)
    return tuple(path for _, path in found)


def standard_locations(environ: Mapping[str, str], platform: str) -> tuple[Path, ...]:
    if platform == "win32":
        return windows_locations(environ)
    if platform == "darwin":
        return tuple(Path(p) for p in MACOS_LOCATIONS)
    return ()


def find_kicad_cli(
    *,
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> str | None:
    """The ``kicad-cli`` to run, or ``None`` when none can be found."""
    env = os.environ if environ is None else environ
    host = sys.platform if platform is None else platform
    configured = env.get(ENVIRONMENT_VARIABLE, "").strip()
    if configured:
        if Path(configured).is_absolute():
            return configured if Path(configured).is_file() else None
        return which(configured)
    for candidate in standard_locations(env, host):
        if candidate.is_file():
            return str(candidate)
    return which(DEFAULT_NAME)


def missing_sentence(environ: Mapping[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    configured = env.get(ENVIRONMENT_VARIABLE, "").strip()
    if configured:
        return (
            f"KiCad CLI is not installed or configured: {ENVIRONMENT_VARIABLE} is set to {configured}, "
            "which is not a program on this computer. Correct it, or unset it to use the KiCad "
            "installed in the usual place."
        )
    return "KiCad CLI is not installed or configured."


def parse_version(output: str) -> tuple[int, int, int]:
    match = _VERSION.search(output.strip())
    if not match:
        raise KicadUnavailable("bad_version", f"Unable to parse KiCad version: {output.strip() or '<empty>'}")
    return int(match.group(1)), int(match.group(2)), int(match.group(3) or 0)


def supports(version: tuple[int, int, int]) -> bool:
    return version[0] >= MIN_MAJOR


# --------------------------------------------------------------------------- execution


def _guard(path: str | Path) -> None:
    if "original" in Path(path).resolve().parts:
        raise ValueError(f"refusing to run KiCad on the preserved upload: {path}")


def _clean_stderr(text: str) -> str:
    # Fontconfig prints configuration warnings on some hosts; they bury the real message.
    return "\n".join(line for line in text.splitlines() if line.strip() and not line.startswith("Fontconfig"))


def kicad_environment() -> dict[str, str]:
    """What kicad-cli may see: the basics plus every ``KICAD*`` variable (library and settings locations).

    The server's own secrets (model API key, launch token) are not passed on to a program that parses uploads.
    """
    return child_environment(extra_prefixes=("KICAD",))


async def run(args: list[str], *, cwd: str | Path | None = None, timeout: float = DEFAULT_TIMEOUT) -> CliResult:
    """Run ``kicad-cli <args>``; kills the process and raises ``KicadError('timeout')`` on timeout."""
    exe = find_kicad_cli()
    if exe is None:
        raise KicadUnavailable("missing", missing_sentence())
    started = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        exe,
        *args,
        cwd=str(cwd) if cwd else None,
        env=kicad_environment(),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise KicadError("timeout", f"kicad-cli {' '.join(args[:3])} timed out after {timeout:.0f}s") from None
    except asyncio.CancelledError:
        proc.kill()
        await proc.wait()
        raise
    return CliResult(
        [exe, *args],
        proc.returncode if proc.returncode is not None else -1,
        out.decode("utf-8", "replace"),
        _clean_stderr(err.decode("utf-8", "replace")),
        time.monotonic() - started,
    )


def _check(result: CliResult, out: Path, what: str, ok_codes: tuple[int, ...] = (0,)) -> CliResult:
    if result.returncode not in ok_codes or not out.exists():
        raise KicadError(
            f"{what}_failed",
            f"KiCad could not complete {what} (exit {result.returncode}).",
            (result.stderr or result.stdout)[-2000:],
        )
    return result


# ------------------------------------------------------------------------- operations

_version_cache: tuple[int, int, int] | None = None


async def version(*, require_supported: bool = True) -> tuple[int, int, int]:
    """Installed KiCad version. Raises :class:`KicadUnavailable` when missing or older than 9."""
    global _version_cache
    if _version_cache is not None and not require_supported:
        return _version_cache
    result = await run(["version"], timeout=5)
    if result.returncode != 0:
        raise KicadUnavailable("version_failed", "Unable to determine KiCad version.", result.stderr)
    v = parse_version(result.stdout)
    if require_supported and not supports(v):
        raise KicadUnavailable("unsupported_version", f"KiCad {'.'.join(map(str, v))} is unsupported; TrustPCB needs KiCad {MIN_MAJOR} or newer.")
    _version_cache = v
    return v


def prepare_workdir(project_dir: str | Path, workdir: str | Path) -> Path:
    """Copy a project's files into ``workdir`` (replacing it) so KiCad never touches the original."""
    src, dst = Path(project_dir), Path(workdir)
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, symlinks=False)
    return dst


async def drc(
    pcb: str | Path,
    out_json: str | Path,
    *,
    refill_zones: bool = True,
    schematic_parity: bool | None = None,
    all_track_errors: bool = False,
    timeout: float = SLOW_TIMEOUT,
) -> CliResult:
    """``pcb drc``: JSON report with every severity. Exit codes 0 and 5 both mean a report exists.

    ``schematic_parity`` defaults to on when a ``.kicad_sch`` of the same name sits beside the board.
    Zones are refilled in memory first (the file is not changed: no ``--save-board``).
    ``all_track_errors`` reports every error per track, which makes the count repeatable; the
    default (KiCad's own) stops at the first per track and varies a little between runs.
    """
    pcb, out_json = Path(pcb), Path(out_json)
    _guard(pcb)
    if schematic_parity is None:
        schematic_parity = pcb.with_suffix(".kicad_sch").is_file()
    args = ["pcb", "drc", "--format", "json", "--severity-all", "--exit-code-violations"]
    if schematic_parity:
        args.append("--schematic-parity")
    if refill_zones:
        args.append("--refill-zones")
    if all_track_errors:
        args.append("--all-track-errors")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    result = await run([*args, "-o", str(out_json), str(pcb)], cwd=pcb.parent, timeout=timeout)
    return _check(result, out_json, "drc", (0, 5))


async def erc(sch: str | Path, out_json: str | Path, *, timeout: float = DEFAULT_TIMEOUT) -> CliResult:
    sch, out_json = Path(sch), Path(out_json)
    _guard(sch)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    args = ["sch", "erc", "--format", "json", "--severity-all", "--exit-code-violations", "-o", str(out_json), str(sch)]
    return _check(await run(args, cwd=sch.parent, timeout=timeout), out_json, "erc", (0, 5))


async def stats(pcb: str | Path, out_json: str | Path, *, timeout: float = DEFAULT_TIMEOUT) -> CliResult:
    pcb, out_json = Path(pcb), Path(out_json)
    _guard(pcb)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    args = ["pcb", "export", "stats", "--format", "json", "--units", "mm", "-o", str(out_json), str(pcb)]
    return _check(await run(args, cwd=pcb.parent, timeout=timeout), out_json, "stats")


async def export_glb(pcb: str | Path, out: str | Path, *, timeout: float = SLOW_TIMEOUT) -> CliResult:
    """Binary glTF of the board. Coordinates: x -> x, board y -> glTF z, metres (see baseline docs)."""
    pcb, out = Path(pcb), Path(out)
    _guard(pcb)
    out.parent.mkdir(parents=True, exist_ok=True)
    args = [
        "pcb", "export", "glb", "--force", "--include-tracks", "--include-pads", "--include-zones",
        "--include-silkscreen", "-o", str(out), str(pcb),
    ]
    return _check(await run(args, cwd=pcb.parent, timeout=timeout), out, "glb")


async def refill_zones(pcb: str | Path, workdir: str | Path, *, timeout: float = SLOW_TIMEOUT) -> Path:
    """Refill every zone on a *copy* of the board and return the refilled copy's path.

    Uses ``pcb drc --refill-zones --save-board`` inside ``workdir``. The board is copied there with
    its ``.kicad_pro`` and ``.kicad_dru`` so the fill honours the project clearances. Zone fill is
    parsed from the copy; KiCad mints new uuids when it saves, so match zones by order, not uuid.
    """
    pcb, workdir = Path(pcb), Path(workdir)
    _guard(pcb)
    if workdir.resolve() == pcb.parent.resolve():
        raise ValueError("refill_zones needs a separate work directory")
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)
    copy = workdir / pcb.name
    shutil.copyfile(pcb, copy)
    for beside in (pcb.with_suffix(".kicad_pro"), *sorted(pcb.parent.glob("*.kicad_dru"))):
        if beside.is_file():
            shutil.copyfile(beside, workdir / beside.name)
    report = workdir / "fill.json"
    args = ["pcb", "drc", "--format", "json", "--refill-zones", "--save-board", "-o", str(report), str(copy)]
    result = await run(args, cwd=workdir, timeout=timeout)
    _check(result, report, "refill", (0, 5))
    return copy
