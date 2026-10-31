"""The simulations of one review, for the dashboard: GET /api/reviews/{id}/sims, and the file names a job may serve."""
from __future__ import annotations

import json
from pathlib import Path

from . import evidence as ev

#: Files of a job folder that GET /api/sim/{job_id}/files/{name} may serve. Nothing else is ever served.
SIM_FILES = ('s11.json', 's11_as_built.json', 'zin.json', 'pattern.json', 'field_xy.bin', 'field_xz.bin', 'field.json', 'pulse.bin', 'pulse.json',
             'current.bin', 'current.json', 'result.json', 'summary.json', 'model.png', 'meta.json')
MEDIA = {'.json': 'application/json', '.bin': 'application/octet-stream', '.png': 'image/png'}


def job_folder(store, job: dict | None) -> Path | None:
    """The job's folder when it lies inside the data folder's scenarios directory."""
    if not job or not job.get('job_dir'):
        return None
    root = (store.data_dir / 'scenarios').resolve()
    path = Path(job['job_dir']).resolve()
    return path if path.is_relative_to(root) and path.is_dir() else None


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
    return items
