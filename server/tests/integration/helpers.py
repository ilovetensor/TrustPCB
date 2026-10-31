"""Helpers shared by the integration tests."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import numpy as np

TESTS = Path(__file__).resolve().parents[1]
ROOT = TESTS.parents[1]
SAMPLE_DIR = ROOT / 'samples' / 'nrf52840-node'
FAKE_DOCKER = TESTS / 'rf_model' / 'fake_docker.py'
GOAL = {'purpose': 'Battery Bluetooth sensor', 'band_hz': [2.4e9, 2.4835e9], 'target_s11_db': -10}
SIM_CACHE = Path(os.environ.get('TRUSTPCB_SIM_CACHE') or Path.home() / '.cache' / 'trustpcb' / 'sim-cache')


def make_version(store) -> dict:
    """A version whose id is v-nrf, so scenario hashes match the cached solver runs."""
    import sqlite3
    project = store.create_project('nRF node')
    version = store.create_version(project['id'], label='first')
    db = sqlite3.connect(store.database)                       # fixed id so the cache key (which holds the version id) is stable
    with db:
        db.execute('UPDATE versions SET id=? WHERE id=?', ('v-nrf', version['id']))
    db.close()
    shutil.copytree(SAMPLE_DIR, store.version_dir('v-nrf') / 'project')
    return store.get_version('v-nrf')


def write_pattern(job: Path) -> None:
    theta = np.radians(np.arange(0, 181, 5))[:, None]
    phi = np.radians(np.arange(0, 361, 5))[None, :]
    dbi = 4.0 - 14.0 * (1 - np.cos(theta)) ** 1.5 * (1 + 0.2 * np.cos(phi))
    (job / 'pattern.json').write_text(json.dumps({'f_hz': 2.44e9, 'directivity_dbi': np.round(dbi, 2).tolist(), 'dmax_dbi': float(dbi.max()), 'dmax_theta_deg': 0.0, 'dmax_phi_deg': 0.0}))


def write_planes(job: Path) -> None:
    rows, cols = 40, 60
    yy, xx = np.mgrid[0:rows, 0:cols]
    blob = np.exp(-(((xx - 20) / 8.0) ** 2 + ((yy - 28) / 6.0) ** 2)).astype('<f4')
    np.concatenate([blob[None], (0.5 * blob)[None]]).astype('<f4').tofile(job / 'field_xy.bin')
    np.concatenate([blob[None, :20], (0.2 * blob)[None, :20]]).astype('<f4').tofile(job / 'field_xz.bin')
    blob.astype('<f4').tofile(job / 'current.bin')
    u = [-5.0, 70.0]
    (job / 'field.json').write_text(json.dumps({'f_hz': 2.44e9, 'xy': {'file': 'field_xy.bin', 'shape': [2, rows, cols], 'u_mm': u, 'v_mm': [-45.0, 5.0], 'plane_mm': 2.8},
                                                'xz': {'file': 'field_xz.bin', 'shape': [2, 20, cols], 'u_mm': u, 'v_mm': [-10.0, 10.0], 'plane_mm': -12.8}}))
    (job / 'current.json').write_text(json.dumps({'file': 'current.bin', 'shape': [rows, cols], 'u_mm': u, 'v_mm': [-45.0, 5.0], 'f_hz': 2.44e9}))


def tiny_pdf(text_by_page: list[str]) -> bytes:
    """A small valid PDF with one line of text per page."""
    objects = []
    n_pages = len(text_by_page)
    objects.append('<< /Type /Catalog /Pages 2 0 R >>')
    kids = ' '.join(f'{3 + 2 * i} 0 R' for i in range(n_pages))
    objects.append(f'<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>')
    for i, text in enumerate(text_by_page):
        content_id = 4 + 2 * i
        objects.append(f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Contents {content_id} 0 R /Resources << /Font << /F1 {3 + 2 * n_pages} 0 R >> >> >>')
        stream = f'BT /F1 12 Tf 20 100 Td ({text}) Tj ET'
        objects.append(f'<< /Length {len(stream)} >>\nstream\n{stream}\nendstream')
    objects.append('<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>')
    out = bytearray(b'%PDF-1.4\n')
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f'{i} 0 obj\n{body}\nendobj\n'.encode()
    xref = len(out)
    out += f'xref\n0 {len(objects) + 1}\n0000000000 65535 f \n'.encode()
    for off in offsets:
        out += f'{off:010d} 00000 n \n'.encode()
    out += f'trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode()
    return bytes(out)




# ---- a scripted review on the real board -------------------------------------------------------------

BRIEF = {'tool': 'submit_board_brief', 'args': {'purpose': 'Battery powered Bluetooth sensor node', 'part_roles': {'U3': 'radio'}, 'risks': [], 'priorities': ['antenna']}}
OPEN = {'tool': 'open_investigation', 'args': {'question': 'Does the antenna sit on the 2.4 GHz band as built?', 'suspects': [{'title': 'Antenna length', 'why': 'The resonance looks low.'}]}}
VIEWS = [{'tool': 'view_board', 'args': {'around': 'ref:U3', 'margin_mm': 6}}, {'tool': 'view_board', 'args': {'around': 'net:ANT_FEED', 'margin_mm': 14}}]
BASE = {'tool': 'run_simulation', 'args': {'suspect_id': 'S1', 'label': 'base'}}
STEPS = [BRIEF, {'tool': 'advance_phase'}, *VIEWS, {'tool': 'advance_phase'}, OPEN, BASE,
         {'tool': 'view_result', 'args': {'ref': 'R1', 'kind': 's11'}}, {'tool': 'view_result', 'args': {'ref': 'R1', 'kind': 'smith'}},
         {'tool': 'tune_matching', 'args': {'ref': 'R1'}}]


def make_test_lab(lab_class, store, queue):
    """A RealLab that skips the KiCad baseline (needs kicad-cli) and has no analyzer candidates."""
    class ReviewLab(lab_class):
        async def ensure_baseline(self, *, version_id, progress=None):
            return {'default_rules': False, 'kicad_version': 'test', 'warnings': []}

        async def list_candidates(self, *, version_id, part_roles=None):
            return []

        async def solver_problem(self):
            return None
    return ReviewLab(store, queue=queue)


async def run_scripted_review(store, lab, steps, depth='standard'):
    from trustpcb.review import service
    from trustpcb.runners.scripted import ScriptedRunner
    from trustpcb.sessions import SessionManager
    import asyncio
    version = store.get_version('v-nrf')
    manager = SessionManager(store, runner_factory=lambda profile: ScriptedRunner(steps))
    service.configure(lab=lab, poll_s=0.2)
    started = await service.start_review(store=store, manager=manager, version_id='v-nrf', goal=GOAL, depth=depth)
    current = manager.active_for_project(version['project_id'])
    if current and current.task:
        await asyncio.wait_for(asyncio.shield(current.task), timeout=120)
    await manager.shutdown()
    return store.get_review(started['id'])


def tool_results(store, review, name):
    return [e['payload'] for e in store.events(review['chat_id']) if e['type'] == 'tool_result' and e['payload']['name'] == name]
