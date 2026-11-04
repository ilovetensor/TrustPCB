"""The Lab: everything the review needs from the PCB model, the analyzers and the RF model, behind one interface.

The review code never imports those packages. It receives a `Lab` and calls the async methods below;
tests inject a fake. `BaselineLab` implements what the PCB model alone can do (board, baseline
candidates, board images) and says plainly that the rest is unavailable, so a review on a machine
without the RF model still runs phases 0 to 3 and reports RF as `Not tested`.

Shapes (all JSON-friendly dicts, lengths in mm, frequencies in Hz):

  rf_status -> {simulatable: bool, reason: str, kind: printed|chip|module|none, antenna_id, antenna_bbox,
                region_bbox, band_hz: [lo, hi], target_s11_db}
  run_scenario -> {status: done|failed|canceled|timeout, job_id, cached, error, solve_s,
                   result: <worker result.json: f_res, s11_min_db, bandwidth{width_hz}, zin_at_res, dmax_dbi,
                            efficiency_est, trust{status ok|fail, reasons[]}, quality, ...>,
                   curve?: {freq_hz[], s11_db[]}}
      The RF summary shape is read too: `sim` (same keys as `result`), top-level `trust`, and `s11`
      with `as_built` / `antenna_only` band statistics.
      The Lab must not emit events (the review emits sim_queued, sim_progress and sim_done) and must stop
      the solver when the awaiting task is cancelled.
  view_board / view_result -> {mime, data_b64, caption}
  tune_matching -> {topology, components: [{name, kind, value, unit}], parts: [{ref, position, kind, value (SI), was, now}] (changed parts only),
                    predicted: {s11_min_db, f_min_hz, band_worst_db, band_best_db, band_mean_db}, dips?, first_series_resonance_hz?,
                    zin_band_centre?: {f_hz, re, im}, curve?: {freq_hz, s11_db}, best_single_change?, note}
      params: band_hz, target_db, and optionally `changes` [{ref, kind: L|C|None, value (SI)}] to apply explicit parts instead of searching.
      The review calls it for an experiment (screen runs, zero solver time) and again at confirm quality: parts locked, then free.
  compare_results -> {measurements?: {name: {value, unit}}, note}
  try_placement -> {ref, target, new_position, distances: [...], overlaps: [...], conflicts: [...], ghost: {...}, rough_length_change_mm}
  read_datasheet -> {names: [...]} without a name, otherwise {name, page, pages, text}
  sim_status / sim_inputs / run_board_sim / part_lookup / part_datasheet -> see lab_sims.py (optional: a Lab without them has no board simulations)
  candidates are the Candidate dicts described in CONTRIBUTING.md.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

Progress = Callable[[dict], Awaitable[None]]


logger = logging.getLogger('trustpcb')


class LabUnavailable(RuntimeError):
    """The capability is not installed on this machine. Reported to the agent as a plain message."""


class BadMatchingRequest(LabUnavailable):
    """The parts asked for cannot be applied (a part that is not in the network, a value that is not positive). The agent can correct it."""


class Lab(Protocol):
    async def ensure_baseline(self, *, version_id: str, progress: Progress | None = None) -> dict: ...
    async def load_board(self, *, version_id: str) -> Any: ...
    async def list_candidates(self, *, version_id: str, part_roles: dict | None = None) -> list[dict]: ...
    async def run_analyzer(self, *, version_id: str, rule: str, targets: list[str] | None = None, params: dict | None = None) -> list[dict]: ...
    async def view_board(self, *, version_id: str, view: str, region: list[float] | None = None, marks: list[dict] | None = None, width: int = 1200) -> dict: ...
    async def rf_status(self, *, version_id: str, goal: dict | None = None) -> dict: ...
    async def run_scenario(self, *, review_id: str, version_id: str, scenario: dict, scenario_hash: str, investigation_id: str | None,
                           max_seconds: float | None, on_progress: Progress | None) -> dict: ...
    async def tune_matching(self, *, review_id: str, version_id: str, result: dict, params: dict) -> dict: ...
    async def view_result(self, *, review_id: str, result: dict, kind: str, params: dict) -> dict: ...
    async def compare_results(self, *, a: dict, b: dict) -> dict: ...
    async def try_placement(self, *, version_id: str, ref: str, target: Any, rotation: float | None = None) -> dict: ...
    async def read_datasheet(self, *, version_id: str, name: str | None = None, page: int = 1) -> dict: ...


# ---- scenario identity ---------------------------------------------------------------------

def _normal(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, dict):
        return {str(k): _normal(v) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_normal(v) for v in value]
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(_normal(value), sort_keys=True, separators=(',', ':'))


def scenario_hash(scenario: dict) -> str:
    """Content hash of a scenario: the cache key."""
    return hashlib.sha256(canonical_json(scenario).encode()).hexdigest()[:16]


def scenario_key(scenario: dict) -> str:
    """The hash with setup repairs removed: two runs with the same key are the same scenario, repaired."""
    return scenario_hash({k: v for k, v in scenario.items() if k != 'setup'})


# ---- the PCB-only lab -----------------------------------------------------------------------

def _board_sim_lab():
    from .lab_sims import BoardSimLab
    return BoardSimLab


class BaselineLab(_board_sim_lab()):
    """Board, baseline candidates and board images from the PCB model, and the board simulations when they are installed. The RF lab is unavailable."""

    def __init__(self, store):
        self.store = store
        self._boards: dict[str, Any] = {}

    def _dir(self, version_id: str) -> Path:
        return self.store.version_dir(version_id)

    async def ensure_baseline(self, *, version_id, progress=None) -> dict:
        import asyncio
        path = self._dir(version_id) / 'artifacts' / 'baseline.json'
        if not path.is_file():
            from ..pcb.baseline import run_baseline
            return await run_baseline(self._dir(version_id), progress=progress)
        return await asyncio.to_thread(lambda: json.loads(path.read_text()))

    async def load_board(self, *, version_id):
        import asyncio
        if version_id not in self._boards:
            self._boards[version_id] = await asyncio.to_thread(load_board_from_disk, self._dir(version_id), version_id)
        return self._boards[version_id]

    async def list_candidates(self, *, version_id, part_roles=None) -> list[dict]:
        """KiCad results from the baseline plus the analyzers (when the analyzers are installed)."""
        path = self._dir(version_id) / 'artifacts' / 'candidates.json'
        found = json.loads(path.read_text()) if path.is_file() else []
        try:
            from ..analyzers import drc_summary
            drc_summary.annotate(found)       # older versions have no distinct counts: add them on read (pure, idempotent)
        except ImportError:
            pass
        await self._schematic_facts(version_id, found)
        return found + await self._analyze(version_id, part_roles)

    async def _schematic_facts(self, version_id, found: list[dict]) -> None:
        """Parity results get what the schematic already says (a no-connect marker, the do-not-populate parts), so no fix is recommended that the file already has."""
        if not any(str(c.get('rule', '')).startswith('parity_') for c in found):
            return
        try:
            from ..analyzers.base import AnalyzerContext
            from ..analyzers.schematic import annotate_parity
            board = await self.load_board(version_id=version_id)
            annotate_parity(found, board, AnalyzerContext(version_dir=str(self._dir(version_id))))
        except Exception:       # the facts are extra: a schematic that cannot be read must not stop the review
            logger.warning('Version %s: the schematic could not be read for parity facts', version_id, exc_info=True)

    async def _analyze(self, version_id, part_roles=None, only=None) -> list[dict]:
        import asyncio
        try:
            from ..analyzers.base import AnalyzerContext, run_all
        except ImportError:
            return []
        board = await self.load_board(version_id=version_id)
        context = AnalyzerContext(part_roles=dict(part_roles or {}), version_dir=str(self._dir(version_id)))
        return (await asyncio.to_thread(run_all, board, context, only))['candidates']

    async def run_analyzer(self, *, version_id, rule, targets=None, params=None):
        try:
            from ..analyzers.base import ANALYZER_NAMES
        except ImportError:
            raise LabUnavailable('Analyzers are not installed in this build.') from None
        if rule in ANALYZER_NAMES:
            found = await self._analyze(version_id, only=[rule])
        else:
            path = self._dir(version_id) / 'artifacts' / 'candidates.json'
            found = [c for c in (json.loads(path.read_text()) if path.is_file() else []) if c.get('rule') == rule]
        wanted = set(targets or [])
        return [c for c in found if not wanted or wanted & set(c.get('targets') or [])]

    async def view_board(self, *, version_id, view, region=None, marks=None, width=1200) -> dict:
        import asyncio
        from ..pcb import render_svg
        board = await self.load_board(version_id=version_id)

        def draw() -> bytes:
            opts = render_svg.RenderOptions(region=region) if region else render_svg.RenderOptions()
            svg = render_svg.layer_svg(board, view, opts)
            if marks:
                svg = render_svg.annotate(svg, marks, board)
            return render_svg.render_png(svg, width)

        png = await asyncio.to_thread(draw)
        return {'mime': 'image/png', 'data_b64': base64.b64encode(png).decode(), 'caption': f'{view} view' + (' (region)' if region else '')}

    async def rf_status(self, *, version_id, goal=None) -> dict:
        return {'simulatable': False, 'reason': 'The RF lab is not installed in this build.', 'kind': 'none'}

    async def run_scenario(self, **kwargs):
        raise LabUnavailable('The RF lab is not installed in this build.')

    async def tune_matching(self, **kwargs):
        raise LabUnavailable('The matching calculator is not installed in this build.')

    async def view_result(self, **kwargs):
        raise LabUnavailable('The RF lab is not installed in this build.')

    async def compare_results(self, **kwargs):
        raise LabUnavailable('The RF lab is not installed in this build.')

    async def try_placement(self, **kwargs):
        raise LabUnavailable('The placement what-if is not installed in this build.')

    async def read_datasheet(self, *, version_id, name=None, page=1) -> dict:
        return {'names': [], 'note': 'No datasheets were uploaded.'}


def load_board_from_disk(version_dir: Path, version_id: str):
    """Parse the version's board and register baseline candidates so `candidate:` targets resolve."""
    from ..pcb.model import parse_board
    root = version_dir / 'project' if (version_dir / 'project').is_dir() else version_dir
    pcbs = sorted(root.rglob('*.kicad_pcb'))
    if not pcbs:
        raise FileNotFoundError('No .kicad_pcb file in this version')
    board = parse_board(pcbs[0], version_id)
    candidates = version_dir / 'artifacts' / 'candidates.json'
    if candidates.is_file():
        for cand in json.loads(candidates.read_text()):
            board.register_candidate(cand['id'], cand.get('targets', []))
    return board
