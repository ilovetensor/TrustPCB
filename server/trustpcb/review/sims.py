"""The simulations of one review, for the dashboard: GET /api/reviews/{id}/sims, and the file names a job may serve."""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import evidence as ev

#: Files of a job folder that GET /api/sim/{job_id}/files/{name} may serve. Nothing else is ever served.
SIM_FILES = ('s11.json', 's11_as_built.json', 'zin.json', 'pattern.json', 'field_xy.bin', 'field_xz.bin', 'field.json', 'pulse.bin', 'pulse.json',
             'current.bin', 'current.json', 'result.json', 'summary.json', 'model.png', 'meta.json')
MEDIA = {'.json': 'application/json', '.bin': 'application/octet-stream', '.png': 'image/png'}


def job_folder(store, job: dict | None) -> Path | None:
    """The job's folder when it lies inside the data folder's scenarios directory.

    The job row keeps an absolute path. When the data folder was moved or copied that path points outside it, so the folder named by the
    job's scenario hash inside this data folder is used instead (the folder name is the cache key, so it is the same run).
    """
    if not job or not job.get('job_dir'):
        return None
    root = (store.data_dir / 'scenarios').resolve()
    path = Path(job['job_dir']).resolve()
    if path.is_relative_to(root) and path.is_dir():
        return path
    moved = (root / path.name).resolve()
    return moved if moved.is_relative_to(root) and moved.is_dir() and moved.name == path.name else None


RUN_HASH = re.compile(r'^[0-9a-f]{20}$')
FILE_NAME = re.compile(r'^[a-z0-9][a-z0-9_-]{0,59}$')
BOARD_MEDIA = {'.bin': 'application/octet-stream', '.json': 'application/json'}


def board_run_folder(store, review_id: str, run: str) -> Path | None:
    """The folder of one board-simulation run when it belongs to this review and lies directly inside the data folder's sims directory. Nothing else is ever served."""
    if not RUN_HASH.fullmatch(run or ''):
        return None
    if not any(row['type'] == ev.BOARD_SIM and (row['values'] or {}).get('run_hash') == run for row in store.list_evidence(review_id)):
        return None
    root = (store.data_dir / 'sims').resolve()
    path = (root / run).resolve()
    return path if path.parent == root and path.is_dir() else None


def _result_of(folder: Path | None) -> dict:
    if folder is None:
        return {}
    try:
        return json.loads((folder / 'result.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def overlay_file(folder: Path, overlay_id: str) -> Path | None:
    """The raster of one overlay named in the run's result.json: float32 little-endian [ny, nx]. The file name must be a plain name inside the run folder."""
    for item in _result_of(folder).get('overlays') or []:
        if isinstance(item, dict) and item.get('id') == overlay_id:
            name = str(item.get('file') or '')
            path = (folder / name).resolve()
            if name == Path(name).name and name.endswith('.bin') and path.parent == folder.resolve() and path.is_file():
                return path
    return None


def curve_file(folder: Path, name: str) -> Path | None:
    """A curve the simulator stored next to its result as `<name>.json` (never result.json itself)."""
    if not FILE_NAME.fullmatch(name or '') or name == 'result':
        return None
    path = (folder / f'{name}.json').resolve()
    return path if path.parent == folder.resolve() and path.is_file() else None


#: board simulations that are running now, by review: {ref: row}. In memory only; a run that finished has its evidence row, which replaces this one.
LIVE_RUNS: dict[str, dict[str, dict]] = {}


def live_started(review_id: str, ref: str, row: dict) -> None:
    LIVE_RUNS.setdefault(review_id, {})[ref] = row


def live_finished(review_id: str, ref: str) -> None:
    LIVE_RUNS.get(review_id, {}).pop(ref, None)


def board_items(store, review_id: str, rows: list[dict]) -> list[dict]:
    """The board-simulation runs of a review (power, thermal, signal), oldest first. Each row has what the app lists (`run_id`, `sim_kind`, `status`, `label`, `ref`, `whatif`,
    `base_run`, `progress`, `step`) and, once done, the `result` in the shape the app reads (simview.web_result), with the overlays' urls."""
    from . import simview
    items = []
    for row in rows:
        values = row['values'] or {}
        if row['type'] != ev.BOARD_SIM or not values.get('sim_kind'):
            continue
        run = values.get('run_hash')
        if values.get('comparison'):
            items.append({'family': 'board', 'sim_kind': values['sim_kind'], 'kind': 'comparison', 'ref': values.get('ref'), 'label': values.get('label'), 'quality': values.get('quality'), 'trust': values.get('trust'),
                          'measurements': values.get('measurements'), 'evidence': row['code'], 'outcome': values.get('outcome'), 'statement': values.get('statement'),
                          'confirmation': values.get('confirmation'), 'defaults_used': values.get('defaults_used') or [], 'created_at': row.get('created_at')})
            continue
        folder = board_run_folder(store, review_id, run) if run else None
        stored = _result_of(folder)
        result = simview.web_result(stored) if stored else None
        if result is not None:
            for overlay in result['overlays']:
                overlay['url'] = f'/api/reviews/{review_id}/sims/{run}/overlay/{overlay.get("id")}' if overlay_file(folder, overlay.get('id')) else None
        items.append({'family': 'board', 'sim_kind': values['sim_kind'], 'run_id': run, 'run': run, 'status': 'done' if result is not None else 'failed', 'role': {'screen': 'whatif' if values.get('whatif_ops') else 'base'}.get(values.get('quality'), 'confirm'),
                      'ref': values.get('ref'), 'label': values.get('label'), 'quality': values.get('quality'), 'trust': values.get('trust'), 'measurements': values.get('measurements'),
                      'primary': values.get('primary'), 'defaults_used': values.get('defaults_used') or [], 'whatif': values.get('whatif_ops') or [], 'whatif_ops': values.get('whatif_ops') or [],
                      'base_run': values.get('base_run'), 'progress': 1.0 if result is not None else None, 'step': 'done' if result is not None else None, 'inputs': values.get('inputs') or {},
                      'evidence': row['code'], 'assumptions_evidence': values.get('assumption_evidence'), 'suspect': values.get('suspect'), 'compared_with': values.get('compared_with'),
                      'solve_s': values.get('solve_s'), 'cached': values.get('cached'), 'result': result, 'created_at': row.get('created_at'), 'error': None})
    for ref, live in LIVE_RUNS.get(review_id, {}).items():
        items.append({'family': 'board', **live})
    return items


def board_run(store, review_id: str, run: str) -> dict | None:
    """One board-simulation run of the review by its run id, the newest row when the same stored run was asked for more than once."""
    found = [i for i in board_items(store, review_id, store.list_evidence(review_id)) if i.get('run_id') == run and i.get('kind') != 'comparison']
    return found[-1] if found else None


def _kind(row: dict | None, ops: list) -> str:
    if row is None:
        return 'base' if not ops else 'whatif'
    if row['type'] == ev.CONFIRM or row.get('source_tool') == 'confirm_result':
        return 'confirm'
    return 'whatif' if (row['values'].get('whatif_ops') or row['values'].get('material')) else 'base'


def list_sims(store, review_id: str) -> list[dict]:
    """Every simulation job of the review, oldest first, with its label, quality, what-if operations, trust and key numbers."""
    evidence_by_job: dict[str, dict] = {}
    for row in store.list_evidence(review_id):
        if row['type'] in (ev.SIMULATION, ev.CONFIRM) and (row['values'] or {}).get('job_id'):
            evidence_by_job[row['values']['job_id']] = row
    items, seen = [], set()
    for job in store.list_sim_jobs(review_id=review_id):
        seen.add(job['id'])
        row = evidence_by_job.get(job['id'])
        values = (row or {}).get('values') or {}
        scenario = job.get('scenario') or {}
        ops = scenario.get('whatif_ops') or values.get('whatif_ops') or []
        folder = job_folder(store, job)
        metrics = ev.metrics_from_result(job.get('result') or {}) if job.get('result') else {}
        trust = values.get('trust') or ({'status': metrics.get('trust_status'), 'reasons': metrics.get('trust_reasons') or []} if metrics else None)
        if trust is None and job.get('trust'):
            try:
                trust = json.loads(job['trust']) if isinstance(job['trust'], str) else job['trust']
            except ValueError:
                trust = None
        if isinstance(trust, dict):
            trust = {'status': trust.get('status'), 'reasons': trust.get('reasons') or []}
        items.append({
            'job_id': job['id'], 'status': job['status'], 'scenario_hash': job['scenario_hash'], 'quality': scenario.get('quality') or values.get('quality'),
            'label': values.get('label'), 'ref': values.get('ref'), 'kind': _kind(row, ops), 'suspect': values.get('suspect'), 'investigation_id': job.get('investigation_id'),
            'evidence': row['code'] if row else None, 'whatif_ops': ops, 'material': scenario.get('material') or values.get('material'), 'trust': trust,
            'measurements': values.get('measurements') or ev.measurements_of(metrics), 'solve_s': values.get('solve_s') or metrics.get('solve_s'),
            'progress': job.get('progress'), 'error': job.get('error'), 'created_at': job.get('created_at'),
            'files': [name for name in SIM_FILES if folder and (folder / name).is_file()],
        })
    for job_id, row in evidence_by_job.items():           # runs whose job row is not in this database (a test lab, a pruned job)
        if job_id in seen:
            continue
        values = row['values']
        items.append({'job_id': job_id, 'status': values.get('status') or 'done', 'scenario_hash': values.get('scenario_hash'), 'quality': values.get('quality'),
                      'label': values.get('label'), 'ref': values.get('ref'), 'kind': _kind(row, values.get('whatif_ops') or []), 'suspect': values.get('suspect'),
                      'investigation_id': None, 'evidence': row['code'], 'whatif_ops': values.get('whatif_ops') or [], 'material': values.get('material'),
                      'trust': values.get('trust'), 'measurements': values.get('measurements') or {}, 'solve_s': values.get('solve_s'), 'progress': 1.0,
                      'error': None, 'created_at': row.get('created_at'), 'files': []})
    items.sort(key=lambda i: i.get('created_at') or '')
    return items + board_items(store, review_id, store.list_evidence(review_id))
