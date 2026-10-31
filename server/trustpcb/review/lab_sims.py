"""What a Lab does for the board simulations (power, thermal, signal) and for part lookup. Mixed into `BaselineLab`.

The simulators live in `trustpcb.sims` and the supplier layer in `trustpcb.parts`; both are optional. A build without them answers
that nothing is available, so the review still runs. Simulations are CPU work in numpy and scipy, so they run in a worker thread and never
hold the event loop. A run is stored under `<data>/sims/<hash>/` by `sims.run_cached`; an identical run is read back instead of solved.

Shapes (JSON-friendly dicts):

  sim_status -> {kind: {available: bool, reason: str, label: str, whatif_ops: [str]}}   every simulator that is installed
  sim_inputs -> [dict]                                                       what the simulator needs, from its `inputs_needed` (see siminputs.normalize)
  run_board_sim -> {result: <SimResult dict>, run_hash, folder, cached}      `quality` is 'screen' or 'confirm' (a finer mesh); with `cache_only`
                   a run that is not stored gives result None
  part_lookup / part_datasheet                                               optional; they raise LabUnavailable without supplier keys
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger('trustpcb')


def LabUnavailable(message: str):
    """The Lab's own 'not installed' error (imported late: lab.py imports this module)."""
    from .lab import LabUnavailable as error
    return error(message)


def analyzer_context(store, version_id: str, part_roles: dict | None = None):
    """What the simulators receive as `ctx`: the analyzers' own context (part roles from the brief, the version folder for the schematic)."""
    from ..analyzers.base import AnalyzerContext
    return AnalyzerContext(part_roles=dict(part_roles or {}), version_dir=str(store.version_dir(version_id)))


def confirm_profile(kind: str, board) -> dict:
    """The inputs that make a run finer. Each simulator refines its mesh its own way; a confirm run asks for about three times the work of a screen run."""
    if kind == 'signal':
        return {'quality': 'confirm'}
    if kind == 'power':
        return {'max_cells': 900_000, 'convergence_tol': 0.02}
    if kind == 'thermal':
        try:
            from ..sims.thermal import default_pitch
            return {'pitch_mm': round(default_pitch(board, max(2, len(board.copper_layers()))) * 0.6, 3)}
        except Exception:
            return {}
    return {}


def registry() -> dict:
    """The installed simulators by kind, or {} when the package is not in this build."""
    try:
        from .. import sims
    except ImportError:
        return {}
    return dict(getattr(sims, 'SIMULATORS', {}) or {})


class BoardSimLab:
    """Methods `BaselineLab` gains for the board simulations. Needs `self.store` and `self.load_board`."""

    async def sim_status(self, *, version_id: str, goal: dict | None = None) -> dict:
        sims = registry()
        if not sims:
            return {}
        board = await self.load_board(version_id=version_id)
        ctx = analyzer_context(self.store, version_id)

        def check() -> dict:
            out = {}
            for kind, sim in sims.items():
                try:
                    ok, why = sim.available(board, ctx)
                except Exception as exc:        # a simulator that cannot even say is not available
                    ok, why = False, f'{type(exc).__name__}: {str(exc)[:160]}'
                out[kind] = {'available': bool(ok), 'reason': '' if ok else str(why or ''), 'label': getattr(sim, 'label', kind) or kind,
                             'whatif_ops': list(getattr(sim, 'whatif_ops', ()) or ())}
            return out

        return await asyncio.to_thread(check)

    async def sim_inputs(self, *, version_id: str, kind: str, goal: dict | None = None, part_roles: dict | None = None) -> list[dict]:
        sim = registry().get(kind)
        if sim is None:
            raise LabUnavailable(f'The {kind} simulation is not installed in this build.')
        board = await self.load_board(version_id=version_id)
        ctx = analyzer_context(self.store, version_id, part_roles)
        return await asyncio.to_thread(lambda: [dict(item) for item in sim.inputs_needed(board, ctx)])

    async def run_board_sim(self, *, version_id: str, kind: str, goal: dict | None, inputs: dict, whatif: list[dict], quality: str = 'screen', cache_only: bool = False,
                            part_roles: dict | None = None) -> dict:
        sim = registry().get(kind)
        if sim is None:
            raise LabUnavailable(f'The {kind} simulation is not installed in this build.')
        from .. import sims
        board = await self.load_board(version_id=version_id)
        ctx = analyzer_context(self.store, version_id, part_roles)
        run_inputs = dict(inputs or {})
        if quality == 'confirm':
            run_inputs.update(confirm_profile(kind, board))
        if cache_only:      # the budget for new runs is used: a stored identical run is still free
            stored = await asyncio.to_thread(sims.load_cached, sims.run_dir(self.store.data_dir, kind, board, run_inputs, list(whatif or [])))
            if stored is None:
                return {'result': None, 'run_hash': None, 'folder': None, 'cached': False, 'inputs': run_inputs}
            folder = sims.run_dir(self.store.data_dir, kind, board, run_inputs, list(whatif or []))
            return {'result': stored.to_dict(), 'run_hash': Path(folder).name, 'folder': str(folder), 'cached': True, 'inputs': run_inputs}
        result, folder, cached = await asyncio.to_thread(sims.run_cached, sim, board, ctx, run_inputs, list(whatif or []), self.store.data_dir)
        return {'result': result.to_dict(), 'run_hash': Path(folder).name, 'folder': str(folder), 'cached': bool(cached), 'inputs': run_inputs}

    async def part_lookup(self, *, mpn: str | None = None, value: str | None = None, footprint: str | None = None, maker: str | None = None) -> dict:
        try:
            from .. import parts
        except ImportError:
            raise LabUnavailable('Part lookup is not installed in this build.') from None
        if not parts.available():
            raise LabUnavailable('No DigiKey or Mouser key is set, so parts cannot be looked up. Add one in settings.')
        found = await parts.lookup(mpn, value=value, footprint=footprint, maker=maker)
        return found.to_dict() if hasattr(found, 'to_dict') else dict(found)

    async def part_datasheet(self, *, mpn: str, question: str) -> dict:
        """Passages of the part's datasheet that bear on the question (each with its page and the document URL) and the typical inputs the text *states*. Never a server path."""
        try:
            from .. import parts
        except ImportError:
            raise LabUnavailable('Datasheet reading is not installed in this build.') from None
        if not parts.available():
            raise LabUnavailable('No DigiKey or Mouser key is set, so datasheets cannot be fetched. Add one in settings.')
        read = await parts.read_datasheet(mpn, question)
        out = {k: read.get(k) for k in ('status', 'mpn', 'url', 'pages', 'passages', 'reason', 'note', 'hint', 'candidates', 'data_notice') if read.get(k) is not None}
        if read.get('status') in ('ok', 'no_match'):
            stated = await parts.datasheet_inputs(mpn, url=read.get('url'))
            out['inputs'] = {name: {k: v for k, v in item.items() if k != 'path'} for name, item in (stated.get('inputs') or {}).items() if isinstance(item, dict)}
        return out
