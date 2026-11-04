"""Phase 1 baseline: KiCad checks, statistics, 3D model and layer images, with no AI.

``run_baseline(version_dir)`` expects the layout written by ``intake.upload.ingest``
(``project/`` holding the unpacked files; ``version_dir`` itself is used when there is no
``project/``). KiCad is run on a fresh copy in ``work/``; results go to ``artifacts/``::

    artifacts/drc.json  erc.json  stats.json  board.glb
    artifacts/scene/<layer>.svg  scene/scene.json     the viewer's layer SVGs and their viewBox
    artifacts/top.png  bottom.png                     our renderer rasterised with resvg
    artifacts/board.json  candidates.json  baseline.json

Steps run in parallel with bounded concurrency and fail independently: a failed step is recorded
in ``baseline.json`` and the rest still produce their results. DRC and ERC violations become
candidate dicts in the shape described in ``CONTRIBUTING.md`` with marks whose targets resolve on the board
(a KiCad item uuid maps to a track, via, pad, part or zone id; otherwise a ``point:x,y`` target).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..intake import detect as detect_mod
from ..intake import stackup as stackup_mod
from . import kicad_cli, render_svg
from .model import Board, parse_board, resolve_all

logger = logging.getLogger("trustpcb")

Progress = Callable[[str, str], Awaitable[None] | None]


@dataclass
class BaselineOptions:
    concurrency: int = 3
    drc_all_track_errors: bool = True
    glb: bool = True
    png_width: int = 1600
    drc_timeout: float = kicad_cli.SLOW_TIMEOUT


# ------------------------------------------------------------------------------ layout


def _locate(version_dir: Path) -> dict[str, Path | None]:
    project = version_dir / "project" if (version_dir / "project").is_dir() else version_dir
    info = detect_mod.find_project(project)
    if not info["pcb"]:
        raise FileNotFoundError(f"no .kicad_pcb under {project}")
    return {
        "project": project,
        "pcb": project / info["pcb"],
        "pro": project / info["pro"] if info["pro"] else None,
        "sch": project / info["sch"] if info["sch"] else None,
    }


# ----------------------------------------------------------------------- candidates

_SEVERITY_UP = {"error": "medium", "warning": "low", "exclusion": "info", "info": "info"}
_ESCALATE = {"shorting_items", "tracks_crossing", "unconnected_items", "short_circuit", "net_conflict_open"}
_DOWNGRADE = {"high": "medium", "medium": "low", "low": "info", "info": "info", "critical": "high"}
_ACTUAL = re.compile(r"(?P<name>[A-Za-z][A-Za-z ()'/_-]*?)\s+(?P<req>-?\d+(?:\.\d+)?)\s*(?P<u>mm|mils|in)\b[^;]*;\s*actual\s+(?P<act>-?\d+(?:\.\d+)?)\s*(?P=u)")


def _measurements(description: str) -> dict[str, dict[str, Any]]:
    m = _ACTUAL.search(description)
    if not m:
        return {}
    return {
        "required": {"value": float(m.group("req")), "unit": m.group("u")},
        "actual": {"value": float(m.group("act")), "unit": m.group("u")},
    }


def _severity(kind: str, rule: str, default_rules: bool) -> str:
    sev = _SEVERITY_UP.get(kind, "medium")
    if kind == "error" and rule in _ESCALATE:
        sev = "high"
    if kind == "warning" and rule.startswith("net_conflict"):
        sev = "info"
    if rule.startswith("footprint_symbol") or rule == "extra_footprint":
        sev = "info"
    if default_rules and rule not in ("unconnected_items",):
        sev = _DOWNGRADE.get(sev, sev)
    return sev


def candidates_from_report(
    board: Board,
    drc: dict | None,
    erc: dict | None,
    *,
    default_rules: bool,
) -> list[dict[str, Any]]:
    """Turn KiCad DRC and ERC report JSON into contract candidates (as dicts)."""
    out: list[dict[str, Any]] = []
    counters: dict[str, int] = {}

    def add(prefix: str, v: dict, schematic: bool, kind_hint: str | None = None) -> None:
        rtype = str(v.get("type") or kind_hint or "violation")
        rule = f"{prefix}_{rtype}"
        counters[rule] = counters.get(rule, 0) + 1
        cid = f"candidate:{rule}:{counters[rule]}"
        description = str(v.get("description") or rtype)
        items = v.get("items") or []
        targets: list[str] = []
        details: list[str] = []
        points: list[list[float]] = []
        for item in items:
            details.append(str(item.get("description", ""))[:300])
            pos = item.get("pos") or {}
            if isinstance(pos.get("x"), (int, float)) and isinstance(pos.get("y"), (int, float)):
                points.append([float(pos["x"]), float(pos["y"])])
            target = None if schematic else board.uuid_index.get(str(item.get("uuid") or ""))
            if target is None and not schematic and points:
                target = f"point:{points[-1][0]:.4f},{points[-1][1]:.4f}"
            if target and target not in targets:
                targets.append(target)
        marks: list[dict[str, Any]] = []
        if targets:
            marks.append({"type": "pin", "targets": [targets[0]], "label": description[:80]})
            real = [t for t in targets if not t.startswith("point:")]
            if real:
                marks.append({"type": "object", "targets": real, "label": description[:80]})
        sev_kind = str(v.get("severity", "error"))
        cand: dict[str, Any] = {
            "id": cid,
            "rule": rule,
            "title": description,
            "source": "kicad",
            "measurements": _measurements(description) if not schematic else {},
            "targets": targets,
            "marks": marks,
            "severity_hint": _severity(sev_kind, rtype, default_rules),
            "kicad_severity": sev_kind,
            "details": details,
        }
        if points:
            cand["position"] = points[0]
        if default_rules and not schematic:
            cand["downgraded"] = True
            cand["notes"] = ["checked with KiCad default rules: no .kicad_pro was uploaded"]
        out.append(cand)

    if drc:
        for key, prefix in (("violations", "drc"), ("unconnected_items", "drc"), ("schematic_parity", "parity")):
            for v in drc.get(key, []) or []:
                add(prefix, v, False, "unconnected_items" if key == "unconnected_items" else None)
    if erc:
        for sheet in erc.get("sheets", []) or []:
            for v in sheet.get("violations", []) or []:
                add("erc", v, True)
    return out


def _drc_summary(drc: dict | None) -> dict[str, Any] | None:
    if drc is None:
        return None
    by_type: dict[str, int] = {}
    for v in drc.get("violations", []) or []:
        by_type[v.get("type", "?")] = by_type.get(v.get("type", "?"), 0) + 1
    sev: dict[str, int] = {}
    for v in drc.get("violations", []) or []:
        sev[v.get("severity", "?")] = sev.get(v.get("severity", "?"), 0) + 1
    return {
        "violations": len(drc.get("violations", []) or []),
        "unconnected_items": len(drc.get("unconnected_items", []) or []),
        "schematic_parity": len(drc.get("schematic_parity", []) or []),
        "by_type": by_type,
        "by_severity": sev,
        "kicad_version": drc.get("kicad_version"),
    }


# ---------------------------------------------------------------------------- zones


def apply_zone_fill(board: Board, refilled: Board) -> int:
    """Copy filled polygons from a refilled copy into ``board``'s zones. Returns zones updated.

    KiCad mints new uuids on save, so zones are matched by order, falling back to name and net.
    """
    updated = 0
    if len(refilled.zones) == len(board.zones):
        pairs = list(zip(board.zones, refilled.zones))
    else:
        pairs = []
        for z in board.zones:
            match = next((r for r in refilled.zones if (r.name, r.net, tuple(r.layers)) == (z.name, z.net, tuple(z.layers))), None)
            if match is not None:
                pairs.append((z, match))
    for z, r in pairs:
        if r.filled:
            z.filled = r.filled
            updated += 1
    return updated


def _needs_fill(board: Board) -> bool:
    return any(z.net and not z.filled for z in board.zones)


# ---------------------------------------------------------------------------- steps


class _Step:
    def __init__(self, name: str):
        self.name = name
        self.status = "pending"
        self.seconds = 0.0
        self.error: str | None = None
        self.artifact: str | None = None
        self.extra: dict[str, Any] = {}

    def to_dict(self) -> dict[str, Any]:
        d = {"status": self.status, "seconds": round(self.seconds, 2), "artifact": self.artifact, "error": self.error}
        d.update(self.extra)
        return d


async def run_baseline(version_dir: str | Path, options: BaselineOptions | None = None, progress: Progress | None = None) -> dict[str, Any]:
    """Run phase 1 for one version. Returns the ``baseline.json`` dict (also written to disk)."""
    opts = options or BaselineOptions()
    version_dir = Path(version_dir)
    paths = _locate(version_dir)
    art = version_dir / "artifacts"
    art.mkdir(parents=True, exist_ok=True)
    (art / "scene").mkdir(exist_ok=True)
    warnings: list[str] = []
    started = time.monotonic()

    async def note(name: str, status: str) -> None:
        if progress is not None:
            r = progress(name, status)
            if asyncio.iscoroutine(r):
                await r

    steps = {n: _Step(n) for n in ("kicad_version", "parse", "drc", "erc", "stats", "glb", "scene", "images")}
    sem = asyncio.Semaphore(max(1, min(opts.concurrency, 4)))
    has_pro = paths["pro"] is not None
    if not has_pro:
        warnings.append("Default rules: no .kicad_pro was uploaded, so KiCad checks ran with default design rules and their results are downgraded.")

    async def guarded(step: _Step, coro_fn: Callable[[], Awaitable[Any]]) -> Any:
        t0 = time.monotonic()
        await note(step.name, "start")
        try:
            async with sem:
                result = await coro_fn()
            step.status = "ok"
            return result
        except kicad_cli.KicadError as exc:
            step.status, step.error = "failed", f"{exc} {exc.detail[-400:]}".strip()
        except Exception as exc:  # a step must never take the baseline down
            logger.exception("Baseline step %s failed", step.name)
            step.status, step.error = "failed", f"{type(exc).__name__}: {exc}"
        finally:
            step.seconds = time.monotonic() - t0
            await note(step.name, step.status)
        return None

    # 1. board model from the project copy (pure Python, fast)
    t0 = time.monotonic()
    try:
        board = await asyncio.to_thread(parse_board, paths["pcb"], version_dir.name)
        steps["parse"].status = "ok"
    except Exception as exc:
        logger.exception("The board could not be parsed")
        steps["parse"].status, steps["parse"].error = "failed", f"{type(exc).__name__}: {exc}"
        result = _finish_failed(version_dir, art, steps, warnings, f"board could not be parsed: {exc}")
        return result
    steps["parse"].seconds = time.monotonic() - t0
    warnings.extend(board.warnings)

    # 2. KiCad works on a copy; the unpacked project is never touched
    work = version_dir / "work"
    await asyncio.to_thread(kicad_cli.prepare_workdir, paths["project"], work)
    rel = lambda p: work / p.relative_to(paths["project"])  # noqa: E731
    w_pcb = rel(paths["pcb"])
    w_sch = rel(paths["sch"]) if paths["sch"] else None

    kicad_ok = True
    try:
        v = await kicad_cli.version()
        steps["kicad_version"].status = "ok"
        steps["kicad_version"].extra["version"] = ".".join(map(str, v))
    except kicad_cli.KicadError as exc:
        logger.warning("KiCad is unavailable, so its checks are skipped: %s", exc)
        kicad_ok = False
        steps["kicad_version"].status, steps["kicad_version"].error = "failed", str(exc)
        warnings.append(f"KiCad unavailable: {exc}. Checks, statistics and the 3D model were skipped.")

    drc_data: dict | None = None
    erc_data: dict | None = None
    stats_data: dict | None = None

    async def do_drc() -> None:
        nonlocal drc_data
        await kicad_cli.drc(w_pcb, art / "drc.json", all_track_errors=opts.drc_all_track_errors, timeout=opts.drc_timeout)
        drc_data = json.loads((art / "drc.json").read_text())
        steps["drc"].artifact = "drc.json"

    async def do_erc() -> None:
        nonlocal erc_data
        await kicad_cli.erc(w_sch, art / "erc.json")
        erc_data = json.loads((art / "erc.json").read_text())
        steps["erc"].artifact = "erc.json"

    async def do_stats() -> None:
        nonlocal stats_data
        await kicad_cli.stats(w_pcb, art / "stats.json")
        stats_data = json.loads((art / "stats.json").read_text())
        steps["stats"].artifact = "stats.json"

    async def do_glb() -> None:
        await kicad_cli.export_glb(w_pcb, art / "board.glb")
        steps["glb"].artifact = "board.glb"
        steps["glb"].extra["bytes"] = (art / "board.glb").stat().st_size

    async def do_zone_fill() -> None:
        refilled = await kicad_cli.refill_zones(w_pcb, version_dir / "work_fill")
        n = apply_zone_fill(board, await asyncio.to_thread(parse_board, refilled))
        steps["scene"].extra["zones_refilled"] = n

    jobs: list[Awaitable[Any]] = []
    if kicad_ok:
        jobs.append(guarded(steps["drc"], do_drc))
        if w_sch is not None:
            jobs.append(guarded(steps["erc"], do_erc))
        else:
            steps["erc"].status, steps["erc"].error = "skipped", "no .kicad_sch uploaded"
        jobs.append(guarded(steps["stats"], do_stats))
        if opts.glb:
            jobs.append(guarded(steps["glb"], do_glb))
        else:
            steps["glb"].status = "skipped"
        if _needs_fill(board):
            steps["zone_fill"] = _Step("zone_fill")
            jobs.append(guarded(steps["zone_fill"], do_zone_fill))
    else:
        for n in ("drc", "erc", "stats", "glb"):
            steps[n].status, steps[n].error = "skipped", "KiCad unavailable"
    await asyncio.gather(*jobs)

    # 3. layer images (pure Python; after any zone refill so pours show)
    async def do_scene() -> None:
        scene = await asyncio.to_thread(render_svg.board_scene, board)
        for name, svg in scene.items():
            (art / "scene" / f"{name}.svg").write_text(svg, encoding="utf-8")
        meta = {
            "view_box": render_svg.view_box(board),
            "units": "mm",
            "layers": list(scene),
            "board_bbox": board.bbox,
            "px_per_mm": render_svg.RenderOptions().px_per_mm,
        }
        (art / "scene" / "scene.json").write_text(json.dumps(meta, indent=1))
        steps["scene"].artifact = "scene/scene.json"

    async def do_images() -> None:
        def make() -> None:
            for side in ("top", "bottom"):
                svg = render_svg.layer_svg(board, side)
                render_svg.render_png(svg, opts.png_width, art / f"{side}.png")

        await asyncio.to_thread(make)
        steps["images"].artifact = "top.png"

    await asyncio.gather(guarded(steps["scene"], do_scene), guarded(steps["images"], do_images))

    # 4. candidates, detection, stackup
    candidates = candidates_from_report(board, drc_data, erc_data, default_rules=not has_pro)
    for cand in candidates:
        board.register_candidate(cand["id"], cand["targets"])
        for mark in cand["marks"]:
            mark["resolved"] = resolve_all(board, mark["targets"])
    detection = detect_mod.detect(board, paths["sch"]).to_dict()
    stack = stackup_mod.confirmation(board)

    (art / "board.json").write_text(json.dumps(board.to_dict(), separators=(",", ":")))
    (art / "candidates.json").write_text(json.dumps(candidates, indent=1))
    baseline: dict[str, Any] = {
        "version_dir": str(version_dir),
        "version_id": version_dir.name,
        "kicad_version": steps["kicad_version"].extra.get("version"),
        "files": {k: (str(p.relative_to(paths["project"])) if p else None) for k, p in (("pcb", paths["pcb"]), ("pro", paths["pro"]), ("sch", paths["sch"]))},
        "has_project_file": has_pro,
        "default_rules": not has_pro,
        "board": board.summary(),
        "drc": _drc_summary(drc_data),
        "erc": {"violations": sum(len(s.get("violations", [])) for s in (erc_data or {}).get("sheets", []))} if erc_data is not None else None,
        "stats": stats_data,
        "detection": detection,
        "stackup": stack,
        "candidates": {"count": len(candidates), "by_rule": _count_by(candidates, "rule"), "by_severity": _count_by(candidates, "severity_hint"), "path": "candidates.json"},
        "steps": {n: s.to_dict() for n, s in steps.items()},
        "artifacts": sorted(str(p.relative_to(art)) for p in art.rglob("*") if p.is_file()),
        "warnings": warnings,
        "seconds": round(time.monotonic() - started, 2),
        "notes": [
            "KiCad's default DRC stops at the first error per track and runs in parallel, so the violation count can vary by a few between runs of the same board.",
        ],
    }
    (art / "baseline.json").write_text(json.dumps(baseline, indent=1))
    baseline["artifacts"] = sorted(str(p.relative_to(art)) for p in art.rglob("*") if p.is_file())
    return baseline


def _count_by(items: list[dict[str, Any]], key: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for item in items:
        out[item[key]] = out.get(item[key], 0) + 1
    return out


def _finish_failed(version_dir: Path, art: Path, steps: dict[str, _Step], warnings: list[str], message: str) -> dict[str, Any]:
    baseline = {"version_dir": str(version_dir), "failed": True, "error": message, "steps": {n: s.to_dict() for n, s in steps.items()}, "warnings": warnings}
    (art / "baseline.json").write_text(json.dumps(baseline, indent=1))
    return baseline
