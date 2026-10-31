"""A version recheck on the real boards: nrf52840-node-v2 is the sample after the engineer's hand edits for the confirmed free re-tune
(L1 becomes a 0.51 pF series capacitor, C20 and C19 are fitted with 0.3 pF and 0.36 pF). The copper is unchanged, so the earlier solver run is reused and only
the matching parts differ. The solver results come from the repository's fixtures; no solver runs."""
from __future__ import annotations

import json
import shutil
import sqlite3

import pytest

from helpers import BRIEF, GOAL, ROOT, VIEWS, make_test_lab, tool_results
from trustpcb.pcb import kicad_cli
from trustpcb.review import report, service
from trustpcb.review.fingerprint import TargetIndex, fingerprint
from trustpcb.review.lab import BaselineLab
from trustpcb.review.lab_real import RealLab
from trustpcb.rf.queue import SimQueue
from trustpcb.runners.scripted import ScriptedRunner
from trustpcb.sessions import SessionManager
from trustpcb.storage import Store

FIXTURES = ROOT / 'web' / 'public' / 'fixtures' / 'nrf-sim'
SAMPLE_V2 = ROOT / 'samples' / 'nrf52840-node-v2'
OPEN = {'tool': 'open_investigation', 'args': {'question': 'Does the antenna sit on the 2.4 GHz band as built?', 'suspects': [
    {'title': 'Antenna length', 'why': 'The deepest dip is far below the band.'}, {'title': 'Matching network', 'why': 'The impedance at the band centre is inductive.'}]}}
BASE = {'tool': 'run_simulation', 'args': {'suspect_id': 'S1', 'label': 'base'}}
MATCH = {'tool': 'tune_matching', 'args': {'ref': 'R1', 'suspect_id': 'S2'}}
FINDING = {'tool': 'record_finding', 'args': {
    'title': 'The matching network leaves the antenna out of band', 'severity': 'high', 'what': 'In the screen comparison a different series part at L1 improves the match across the band against the base.',
    'why_it_matters': 'An antenna that reflects most of the power in band shortens the radio link.', 'recommendation': 'Try a different series part at L1 in your own tool and measure the match again.',
    'evidence_ids': ['E-002'], 'targets': ['ref:L1']}}
V1_STEPS = [BRIEF, {'tool': 'advance_phase'}, *VIEWS, {'tool': 'advance_phase'}, OPEN, BASE, MATCH,
            {'tool': 'conclude_suspect', 'args': {'suspect_id': 'S2', 'verdict': 'confirmed', 'reasoning': 'The screen comparison shows a clear gain from a different series part at L1.'}},
            FINDING, {'tool': 'close_investigation', 'args': {'investigation_id': 'I1', 'outcome': 'improved', 'summary': 'A matching change improves the band result in the screen comparison.', 'finding_code': 'F-001'}},
            {'tool': 'advance_phase'}, {'tool': 'confirm_result', 'args': {'investigation_id': 'I1', 'which': 'winner'}}]
DECLINE_REFINE = {'tool': 'advance_phase', 'args': {'decline_refine': 'The confirm base E-003 and the confirm winner E-004 do not meet the target, but the second geometry attempt is not warranted here.'}}
V2_STEPS = [{'tool': 'get_board_summary'}, BRIEF, {'tool': 'advance_phase'}, *VIEWS, {'tool': 'advance_phase'}, OPEN, BASE, {'tool': 'get_status'}]


def seed(store, job_hash, source):
    destination = store.scenario_dir(job_hash)
    shutil.copytree(source, destination, dirs_exist_ok=True, ignore=shutil.ignore_patterns('screen', 'whatif_shorten_6mm_screen', '*.bin', '*.png'))
    progress = destination / 'progress.jsonl'
    if not progress.is_file():
        progress.write_text(json.dumps({'phase': 'done'}) + '\n')


def add_v2(store, project_id: str) -> dict:
    version = store.create_version(project_id, label='after the free re-tune')
    db = sqlite3.connect(store.database)
    with db:
        db.execute('UPDATE versions SET id=? WHERE id=?', ('v-nrf-2', version['id']))
    db.close()
    shutil.copytree(SAMPLE_V2, store.version_dir('v-nrf-2') / 'project')
    return store.get_version('v-nrf-2')


def scenario_of(rf, quality):
    return {'antenna_id': rf['antenna_id'], 'band': rf['band_hz'], 'quality': quality, 'whatif_ops': [], 'material': None, 'region': rf['region_bbox']}


@pytest.fixture
async def two_versions(store, version, fake_docker):
    """Version 1 with the geometry runs of its first review seeded (screen and confirm), and version 2 beside it."""
    wrapper, _ = fake_docker
    queue = SimQueue(store, docker=str(wrapper), poll_s=0.1, timeout_s=30)
    lab = make_test_lab(RealLab, store, queue)
    rf1 = await lab.rf_status(version_id='v-nrf', goal=GOAL)
    hashes = {q: (await lab._prepare('v-nrf', {'version_id': 'v-nrf', **scenario_of(rf1, q)}))[3] for q in ('screen', 'confirm')}
    seed(store, hashes['screen'], FIXTURES / 'screen')
    seed(store, hashes['confirm'], FIXTURES)
    add_v2(store, version['project_id'])
    return lab, queue, hashes


async def run_review(store, lab, steps, version_id, depth='standard'):
    import asyncio
    version = store.get_version(version_id)
    runners = []
    manager = SessionManager(store, runner_factory=lambda profile: runners.append(ScriptedRunner(steps)) or runners[-1])
    service.configure(lab=lab, poll_s=0.2)
    started = await service.start_review(store=store, manager=manager, version_id=version_id, goal=GOAL, depth=depth)
    current = manager.active_for_project(version['project_id'])
    if current and current.task:
        await asyncio.wait_for(asyncio.shield(current.task), timeout=120)
    await manager.shutdown()
    run_review.runners = runners
    return store.get_review(started['id'])


def payload(result):
    return json.loads(result['content'])


async def test_the_same_copper_in_a_new_version_is_the_same_cache_key_and_the_new_parts_are_read(store, version, two_versions):
    lab, queue, hashes = two_versions
    rf2 = await lab.rf_status(version_id='v-nrf-2', goal=GOAL)
    assert {e['ref']: (e['kind'], e['fitted'], e['value']) for e in rf2['matching_network']} == {'C20': ('C', True, '0.3pF'), 'L1': ('C', True, '0.51pF'), 'C19': ('C', True, '0.36pF')}
    for quality in ('screen', 'confirm'):
        assert (await lab._prepare('v-nrf-2', {'version_id': 'v-nrf-2', **scenario_of(rf2, quality)}))[3] == hashes[quality]


async def test_the_v2_as_built_result_comes_from_the_stored_solver_output_with_no_solver_run(store, version, two_versions):
    lab, queue, hashes = two_versions
    rf2 = await lab.rf_status(version_id='v-nrf-2', goal=GOAL)
    kwargs = dict(review_id=None, investigation_id=None, max_seconds=30, on_progress=None)
    run = await lab.run_scenario(version_id='v-nrf-2', scenario={'version_id': 'v-nrf-2', **scenario_of(rf2, 'confirm')}, scenario_hash='x', **kwargs)
    assert run['status'] == 'done' and run['cached'] is True and run['solve_s'] == 0 and run['trust']['status'] == 'ok'
    assert queue.stats['ran'] == 0 and queue.stats['cache_hits'] == 1
    alone, built = run['s11']['antenna_only'], run['s11']['as_built']
    assert alone['band_worst_db'] == pytest.approx(-0.82, abs=0.01)                      # the antenna is the same
    assert built['band_worst_db'] == pytest.approx(-6.12, abs=0.02) and built['band_covered_10db'] is False       # the free re-tune: about 6 dB at worst in band, as the review's matching search predicted
    assert [(p['topology'], p['kind'], p['text']) for p in built['parts']] == [('shunt', 'C', '0.3 pF'), ('series', 'C', '0.51 pF'), ('shunt', 'C', '0.36 pF')]
    assert built['band_worst_accepted_power_pct'] == pytest.approx(75.5, abs=0.2) and alone['band_worst_accepted_power_pct'] == pytest.approx(17.2, abs=0.2)
    assert built['band_mean_accepted_power_pct'] > built['band_worst_accepted_power_pct']
    # the stored run of version 1 is untouched: its own parts, its own as-built result
    again = await lab.run_scenario(version_id='v-nrf', scenario={'version_id': 'v-nrf', **scenario_of(rf2, 'confirm')}, scenario_hash='y', **kwargs)
    assert again['s11']['as_built']['band_worst_db'] == pytest.approx(-0.503, abs=0.01) and again['s11']['as_built']['band_worst_accepted_power_pct'] == pytest.approx(10.9, abs=0.2)
    assert queue.stats['ran'] == 0
    # matching tools work on the derived result with the new parts as the starting point
    out = await lab.tune_matching(review_id=None, version_id='v-nrf-2', result=run, params={'band_hz': rf2['band_hz'], 'target_db': -10})
    assert out['as_built_before']['band_worst_db'] == pytest.approx(-6.12, abs=0.02) and out['as_built_before']['band_worst_accepted_power_pct'] == pytest.approx(75.5, abs=0.2)
    assert out['predicted']['band_worst_db'] <= out['as_built_before']['band_worst_db']


async def test_a_v2_review_starts_from_the_v1_findings_and_sees_the_new_parts_for_free(store, version, two_versions):
    lab, queue, hashes = two_versions
    first = await run_review(store, lab, V1_STEPS, 'v-nrf')
    assert first['end_state'] and store.get_finding_by_code(first['id'], 'F-001')
    ran_before = queue.stats['ran']
    second = await run_review(store, lab, V2_STEPS, 'v-nrf-2')
    assert queue.stats['ran'] == ran_before == 0
    assert second['summary']['previous_review_id'] == first['id'] and second['summary']['previous_version_number'] == 1
    base = payload(tool_results(store, second, 'run_simulation')[0])
    assert base['trust']['status'] == 'ok' and base['cached'] is False and base['in_band']['worst_s11_db'] < -4 and base['in_band']['worst_accepted_power_pct'] > 50
    assert 'mismatch only' in base['in_band']['accepted_power_note'] or 'not radiated power' in base['in_band']['accepted_power_note']
    status = payload(tool_results(store, second, 'get_status')[0])
    assert status['recheck']['previous_review_id'] == first['id'] and [f['title'] for f in status['recheck']['not_recorded_again']] == ['The matching network leaves the antenna out of band']
    summary = payload(tool_results(store, second, 'get_board_summary')[0])
    changes = summary['previous_version']['board_changes']
    assert {p['ref']: (p['from'], p['to']) for p in changes['parts']['value_changed']} == {'L1': ('2.2nH', '0.51pF'), 'C20': ('DNP 0.5pF', '0.3pF'), 'C19': ('DNP 0.5pF', '0.36pF')}
    assert changes['tracks'] == {'added': 0, 'removed': 0} and changes['vias'] == {'added': 0, 'removed': 0} and summary['previous_version']['previous_review_id'] == first['id']
    briefing = run_review.runners[-1].requests[0].user_text
    assert 'You are reviewing version 2' in briefing and 'previous version is version 1' in briefing and first['id'] in briefing and 'F-001' in briefing and 'L1 2.2nH to 0.51pF' in briefing and 'C20 DNP 0.5pF to 0.3pF' in briefing and 'the antenna model is unchanged' in briefing
    classified = report.version_changes(store, second['id'])
    assert classified['against']['review_id'] == first['id'] and [f['code'] for f in classified['fixed']] == ['F-001']


@pytest.mark.skipif(kicad_cli.find_kicad_cli() is None, reason='kicad-cli not installed')   # honours KICAD_CLI_PATH like the app
async def test_the_drc_findings_carry_forward_as_still_present_and_only_the_fitted_part_parity_result_goes(tmp_path):
    """The real KiCad checks on both samples. Only the footprint/symbol mismatch on C20 (fitted in the schematic and not on the board before) is gone."""
    store = Store(tmp_path / 'data')
    project = store.create_project('nRF')
    fingerprints = {}
    for name in ('nrf52840-node', 'nrf52840-node-v2'):
        version = store.create_version(project['id'])
        shutil.copytree(ROOT / 'samples' / name, store.version_dir(version['id']) / 'project')
        lab = BaselineLab(store)
        await lab.ensure_baseline(version_id=version['id'])
        board = await lab.load_board(version_id=version['id'])
        index = TargetIndex(board)
        candidates = await lab.list_candidates(version_id=version['id'])
        fingerprints[name] = {fingerprint(c.get('rule') or '', c.get('targets') or [], index) for c in candidates}
        assert any(c['source'] == 'kicad' for c in candidates) or any((c.get('rule') or '').startswith('drc_') for c in candidates)
    v1, v2 = fingerprints['nrf52840-node'], fingerprints['nrf52840-node-v2']
    assert len(v1 & v2) >= len(v1) - 2 and all(item.startswith('parity_') for item in v2 ^ v1), sorted(v2 ^ v1)
    assert v1 - v2 <= {'parity_footprint_symbol_mismatch||ref:C20', 'parity_footprint_symbol_mismatch||ref:C19'}


async def test_a_v2_summary_that_does_not_open_with_the_change_since_v1_is_refused_and_code_writes_the_lead(store, version, two_versions):
    lab, queue, hashes = two_versions
    first = await run_review(store, lab, V1_STEPS, 'v-nrf')
    bad = {'tool': 'submit_summary', 'args': {'summary': 'Recheck of version 2. The antenna is not tuned. So this is not a partial win.', 'top_actions': ['Measure the match on a real board.']}}
    second = await run_review(store, lab, [*V1_STEPS, DECLINE_REFINE, DECLINE_REFINE, DECLINE_REFINE, bad], 'v-nrf-2', depth='deep')
    refused = [payload(r) for r in tool_results(store, second, 'submit_summary')]
    assert refused and refused[0]['error'] == 'summary_must_lead_with_version_change' and 'Compared with version 1' in refused[0]['message']
    vc = report.recheck_change(store, second['id'])
    assert vc and vc['from_version'] == 1 and vc['to_version'] == 2 and vc['direction'] in ('improved', 'worse', 'unchanged')
    summary = store.get_review(second['id'])['summary']['summary']
    assert summary['source'] == 'code' and summary['text'].startswith('Compared with version 1, the as-built worst S11 across the band')
    snapshot = store.latest_report_revision(second['id'])['snapshot']
    assert snapshot['headline']['kind'] == 'version_change' and snapshot['version_change']['text'] == vc['text']
    html = (await report.render_revision(store=store, review=second, revision=store.latest_report_revision(second['id']), fmt='html')).decode()
    assert html.index('Compared with version 1') < html.index('class="power"') < html.index('<h2>Summary</h2>')
