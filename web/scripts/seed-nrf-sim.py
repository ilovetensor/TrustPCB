"""Dev only: seed a local TrustPCB data folder with the real nRF52840 openEMS run in web/public/fixtures/nrf-sim.

    uv run --project server python web/scripts/seed-nrf-sim.py <data_dir>
    TRUSTPCB_DATA_DIR=<data_dir> TRUSTPCB_TOKEN=devtoken uv run --project server uvicorn trustpcb.api:app --app-dir server --port 8118

It creates one project with two versions of the sample board and a finished review for each, with the solver files
exactly as the worker wrote them. Nothing is invented: the numbers come from the bundled summary files. Version 2's
only simulation is the bundled "antenna shortened by 6 mm" screen run, so the compare page has two runs to overlay.
The project is named so nobody mistakes it for a real review.
"""
from __future__ import annotations

import gzip
import json
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'server'))

from trustpcb.review import evidence as ev  # noqa: E402
from trustpcb.storage import Store  # noqa: E402

SIM = REPO / 'web' / 'public' / 'fixtures' / 'nrf-sim'
BOARD = REPO / 'web' / 'public' / 'fixtures' / 'nrf'
LIGHT = ['s11.json', 's11_as_built.json', 'zin.json', 'pattern.json', 'result.json', 'summary.json', 'model.png', 'meta.json']
FULL = LIGHT + ['field.json', 'field_xy.bin', 'field_xz.bin', 'pulse.json', 'pulse.bin', 'current.json', 'current.bin']


def curve_of(folder: Path) -> dict:
    s11 = json.loads((folder / 's11.json').read_text())
    built = json.loads((folder / 's11_as_built.json').read_text())
    return {'freq_hz': [round(v) for v in s11['freq_hz']], 's11_db': [round(v, 2) for v in built['s11_db']],
            's11_antenna_only_db': [round(v, 2) for v in s11['s11_db']], 'basis': 'as_built'}


def seed_job(store, review, folder: Path, names: list[str], *, ref, label, kind, quality, suspect, whatif_ops, investigation_id, events):
    summary = json.loads((folder / 'summary.json').read_text())
    meta = json.loads((folder / 'meta.json').read_text())
    job_dir = store.scenario_dir(meta['scenario_hash'] + f'-{review["id"][:6]}')
    for name in names:
        shutil.copy(folder / name, job_dir / name)
    metrics = ev.metrics_from_result(summary)
    trust = {'status': 'ok' if summary['trust']['status'] == 'ok' else 'fail', 'reasons': summary['trust'].get('reasons', [])}
    values = {'kind': 'simulated', 'ref': ref, 'label': label, 'quality': quality, 'scenario_hash': meta['scenario_hash'], 'suspect': suspect, 'trust': trust,
              'measurements': ev.measurements_of(metrics), 'metrics': metrics, 'whatif_ops': whatif_ops, 'material': None, 'status': 'done',
              'solve_s': metrics.get('solve_s'), 'curve': curve_of(folder), 'job_id': None}
    job = store.create_sim_job(meta['scenario_hash'], review_id=review['id'], investigation_id=investigation_id, scenario={'quality': quality, 'whatif_ops': whatif_ops}, job_dir=job_dir, status='done')
    store.update_sim_job(job['id'], progress=1.0, result=summary, trust=json.dumps(trust))
    values['job_id'] = job['id']
    store.add_evidence(review['id'], ev.CONFIRM if kind == 'confirm' else ev.SIMULATION, 'confirm_result' if kind == 'confirm' else 'run_simulation', values)
    events.append(('sim_queued', {'ref': ref, 'hash': meta['scenario_hash'], 'label': label, 'suspect': suspect, 'quality': quality, 'kind': kind}))
    for fraction, phase in ((0.18, 'meshing'), (0.55, 'solving'), (0.93, 'far field')):
        events.append(('sim_progress', {'ref': ref, 'hash': meta['scenario_hash'], 'phase': phase, 'fraction': fraction}))
    events.append(('sim_done', {'ref': ref, 'hash': meta['scenario_hash'], 'label': label, 'ok': True, 'trust': trust, 'quality': quality, 'kind': kind, 'measurements': values['measurements'],
                                'delta': None, 'meets_target': False, 'solve_s': metrics.get('solve_s'), 'suspect': suspect, 'evidence': f'E-{len(store.list_evidence(review["id"])):03d}', 'curve': values['curve']}))
    return job


def seed_version(store, project, label, board_json: bytes):
    version = store.create_version(project['id'], label=label, status='ready')
    folder = store.version_dir(version['id'])
    (folder / 'board.json').write_bytes(board_json)
    # the bundled model is stored gzip compressed; the data folder gets the plain GLB the worker would have written
    (folder / 'board.glb').write_bytes(gzip.decompress((BOARD / 'board.glb.gz').read_bytes()))
    store.add_artifact('version', version['id'], 'board.json', folder / 'board.json', 'application/json')
    store.add_artifact('version', version['id'], 'board.glb', folder / 'board.glb', 'model/gltf-binary')
    return version


def main(data_dir: str) -> None:
    store = Store(Path(data_dir))
    if store.list_projects():
        sys.exit(f'{data_dir} already holds a project. Use an empty folder.')
    board_json = (BOARD / 'board.json').read_bytes()
    project = store.create_project('nRF52840 node (dev seed: bundled openEMS run)')
    goal = {'purpose': 'BLE node on 2.4 GHz', 'band_hz': [2.4e9, 2.4835e9], 'target_s11_db': -10, 'focus': 'Is the antenna matched?'}

    for number, label in ((1, 'v1 as drawn'), (2, 'v2 antenna 6 mm shorter')):
        version = seed_version(store, project, label, board_json)
        review = store.create_review(version['id'], goal=goal, depth='standard', profile={'runner': 'scripted', 'name': 'seed'})
        events: list[tuple[str, dict]] = []
        inv = store.create_investigation(review['id'], 'Is the antenna tuned to the band?')
        suspect = store.add_suspect(inv['id'], 'Antenna electrically too long', rank=1)
        events.append(('investigation_updated', {'investigation_id': inv['id'], 'code': 'I-001', 'question': 'Is the antenna tuned to the band?', 'status': 'running',
                                                'suspects': [{'code': 'S-1', 'title': 'Antenna electrically too long', 'status': 'open'}]}))
        if number == 1:
            seed_job(store, review, SIM / 'screen', LIGHT, ref='R1', label='Base, antenna as drawn', kind='base', quality='screen', suspect='S-1', whatif_ops=[], investigation_id=inv['id'], events=events)
            seed_job(store, review, SIM / 'whatif_shorten_6mm_screen', LIGHT, ref='R2', label='Shorten the antenna by 6 mm', kind='whatif', quality='screen', suspect='S-1',
                     whatif_ops=[{'op': 'adjust_antenna_length', 'delta_mm': -6.0}], investigation_id=inv['id'], events=events)
            seed_job(store, review, SIM, FULL, ref='R3', label='Confirm, antenna as drawn', kind='confirm', quality='confirm', suspect='S-1', whatif_ops=[], investigation_id=inv['id'], events=events)
        else:
            seed_job(store, review, SIM / 'whatif_shorten_6mm_screen', LIGHT, ref='R1', label='Base, antenna shortened by 6 mm', kind='base', quality='screen', suspect='S-1', whatif_ops=[], investigation_id=inv['id'], events=events)
        turn = 'seed-turn'
        for typ, payload in events:
            store.append_event(review['chat_id'], turn, typ, payload)
        store.append_event(review['chat_id'], turn, 'completed', {'end_state': 'complete'})
        store.update_review(review['id'], status='completed', end_state='complete', summary={'rf': {'simulatable': True}})
        print(f'v{number}: version {version["id"]} review {review["id"]}')
    print('project', project['id'])


if __name__ == '__main__':
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
