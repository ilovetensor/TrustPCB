"""RealLab on the real nRF sample board with a real SimQueue over a stand-in docker: no solver runs here."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json

import pytest

from helpers import GOAL, tiny_pdf, write_pattern, write_planes
from trustpcb.review.lab import LabUnavailable, canonical_json
from trustpcb.review.lab_real import RealLab
from trustpcb.rf.queue import SimQueue

PNG = b'\x89PNG\r\n\x1a\n'


async def status(lab):
    return await lab.rf_status(version_id='v-nrf', goal=GOAL)


def scenario(rf, **changes):
    base = {'version_id': 'v-nrf', 'antenna_id': rf['antenna_id'], 'band': rf['band_hz'], 'quality': 'screen', 'whatif_ops': [], 'material': None, 'region': rf['region_bbox']}
    return {**base, **changes}


async def run(lab, sc, **kw):
    return await lab.run_scenario(review_id=None, version_id='v-nrf', scenario=sc, scenario_hash='x', investigation_id=None, max_seconds=kw.get('max_seconds'),
                                  on_progress=kw.get('on_progress'))


async def test_rf_status_reports_the_antenna_the_band_and_the_matching_parts(lab, version):
    rf = await status(lab)
    assert rf['simulatable'] and rf['kind'] == 'printed' and rf['antenna_id'] and rf['antenna_net'] == 'ANT_FEED'
    assert rf['band_hz'] == [2.4e9, 2.4835e9] and rf['target_s11_db'] == -10
    assert len(rf['antenna_bbox']) == 4 and len(rf['region_bbox']) == 4 and rf['assumptions'] and rf['port']['on']
    assert rf['matching_network'] and {'ref', 'kind', 'topology', 'value'} <= set(rf['matching_network'][0])


async def test_missing_docker_or_image_is_reported_as_not_tested_with_the_reason(store, version, tmp_path):
    lab = RealLab(store, queue=SimQueue(store, docker=str(tmp_path / 'no-such-docker')))
    rf = await status(lab)
    assert rf['simulatable'] is False and rf['would_simulate'] and 'Docker' in rf['reason'] and 'Not tested' in rf['reason']
    assert rf['antenna_id'] and rf['kind'] == 'printed'          # what was found is still reported
    with pytest.raises(LabUnavailable, match='Docker'):
        await run(lab, scenario(rf))
    image_missing = tmp_path / 'docker'
    image_missing.write_text('#!/bin/sh\nexit 1\n')
    image_missing.chmod(0o755)
    lab = RealLab(store, queue=SimQueue(store, docker=str(image_missing), image='trustpcb-ems:nope'))
    rf = await status(lab)
    assert not rf['simulatable'] and 'trustpcb-ems:nope' in rf['reason']


async def test_image_name_comes_from_the_environment(store, monkeypatch):
    monkeypatch.setenv('TRUSTPCB_EMS_IMAGE', 'my-ems:1')
    assert SimQueue(store).image == 'my-ems:1' and RealLab(store).image == 'my-ems:1'


async def test_a_run_goes_through_the_queue_and_returns_everything_the_agent_needs(lab):
    rf = await status(lab)
    seen = []

    async def progress(update):
        seen.append(update)
    out = await run(lab, scenario(rf), on_progress=progress)
    assert out['status'] == 'done' and out['job_id'] and out['cached'] is False
    assert out['trust']['status'] in ('ok', 'fail') and out['result']['trust'] == out['trust'] and out['result']['quality'] == 'screen'
    assert {'f_res', 's11_min_db', 'dmax_dbi'} <= set(out['result']) and out['sim']['s11_dips'] is not None and 'first_series_resonance_hz' in out['sim']
    assert out['s11']['antenna_only'] and 'as_built' in out['s11']
    assert out['curve']['freq_hz'] and len(out['curve']['freq_hz']) == len(out['curve']['s11_db']) and out['curve']['basis'] in ('as_built', 'antenna_only')
    assert 'comparing' in out['note'].lower() and 'dip' in out['summary_text'] and 'Antenna alone' in out['summary_text'] and 'Trust' in out['summary_text']
    assert seen and all('progress' in u for u in seen) and seen[-1]['fraction'] == 1.0
    again = await run(lab, scenario(rf))
    assert again['cached'] and again['solve_s'] == 0 and again['job_id'] != out['job_id'] and lab.queue.stats['ran'] == 1


async def test_whatif_ops_map_onto_the_rf_scenario_and_bad_ones_are_refused_before_the_solver(lab):
    rf = await status(lab)
    refused = await run(lab, scenario(rf, whatif_ops=[{'op': 'adjust_antenna_length', 'delta_mm': -13.0}]))
    assert refused['status'] == 'failed' and refused['rejected'] and refused['error_code'] == 'out_of_limits' and lab.queue.stats['submitted'] == 0
    ops = RealLab._ops(scenario(rf, whatif_ops=[{'op': 'adjust_antenna_length', 'delta_mm': -9.5}, {'op': 'clear_copper_region', 'bbox': [26, 2, 30, 6], 'layer': 'F.Cu'}], material={'epsilon_r': 4.2}))
    assert [o.name for o in ops] == ['adjust_antenna_length', 'clear_copper_region', 'set_material'] and ops[1].layers == ('F.Cu',)
    out = await run(lab, scenario(rf, whatif_ops=[{'op': 'adjust_antenna_length', 'delta_mm': -12.0}]))
    assert out['status'] == 'done'                               # 12 mm builds and runs
    job = lab.store.get_sim_job(out['job_id'])
    assert job['scenario']['whatif_ops'][0]['delta_mm'] == -12.0


async def test_setup_overrides_get_their_own_cache_key(lab):
    rf = await status(lab)
    plain = await run(lab, scenario(rf))
    changed = await run(lab, scenario(rf, setup={'max_timesteps': 5000}))
    assert changed['status'] == 'done' and not changed['cached']
    rows = {j['id']: j for j in lab.store.list_sim_jobs()}
    assert rows[plain['job_id']]['scenario_hash'] != rows[changed['job_id']]['scenario_hash']
    assert json.loads((lab.store.data_dir / 'scenarios' / rows[changed['job_id']]['scenario_hash'] / 'scenario.json').read_text())['max_timesteps'] == 5000


async def test_cancelling_the_awaiting_task_kills_the_container(lab, fake_docker):
    _, state = fake_docker
    rf = await status(lab)
    task = asyncio.create_task(run(lab, scenario(rf)))
    for _ in range(100):
        await asyncio.sleep(0.1)
        if (state / 'running').exists():
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    name = (state / 'running').read_text()
    for _ in range(50):
        if (state / f'kill-{name}').exists():
            break
        await asyncio.sleep(0.1)
    assert (state / f'kill-{name}').exists()
    assert [j['status'] for j in lab.store.list_sim_jobs()][-1] in ('canceled', 'running')


async def test_the_time_limit_stops_the_solver(lab, fake_docker, monkeypatch):
    _, state = fake_docker
    monkeypatch.setenv('FAKE_DOCKER_MODE', 'hang')
    rf = await status(lab)
    out = await run(lab, scenario(rf), max_seconds=1.5)
    assert out['status'] == 'timeout' and 'time' in out['error']
    assert any(p.name.startswith('kill-') for p in state.iterdir())


@pytest.fixture
async def finished(lab):
    rf = await status(lab)
    out = await run(lab, scenario(rf))
    job = lab.job_dir(out)
    write_pattern(job)
    write_planes(job)
    return out


@pytest.mark.parametrize('kind', ['s11', 'smith', 'pattern', 'field', 'current'])
async def test_every_view_is_a_png(lab, finished, kind):
    image = await lab.view_result(review_id=None, result=finished, kind=kind, params={})
    data = base64.b64decode(image['data_b64'])
    assert image['mime'] == 'image/png' and data.startswith(PNG) and len(data) > 4000 and 'Screen run' in image['caption']


async def test_view_result_accepts_the_form_the_report_rebuilds_from_evidence(lab, finished):
    rebuilt = {'job_id': finished['job_id'], 'status': 'done', 'result': {'f_res_hz': 2.4e9}, 'curve': finished['curve']}
    assert base64.b64decode((await lab.view_result(review_id=None, result=rebuilt, kind='pattern', params={}))['data_b64']).startswith(PNG)
    gone = {'job_id': 'job-that-is-gone', 'status': 'done', 'result': {}, 'curve': finished['curve']}
    assert base64.b64decode((await lab.view_result(review_id=None, result=gone, kind='s11', params={'band_hz': [2.4e9, 2.4835e9]}))['data_b64']).startswith(PNG)
    with pytest.raises(LabUnavailable, match='no longer stored'):
        await lab.view_result(review_id=None, result=gone, kind='field', params={})
    with pytest.raises(LabUnavailable):
        await lab.view_result(review_id=None, result=gone, kind='bogus', params={})


async def test_compare_results_gives_deltas_for_every_band_view(lab, finished):
    rf = await status(lab)
    other = await run(lab, scenario(rf, whatif_ops=[{'op': 'adjust_antenna_length', 'delta_mm': -2.0}]))
    out = await lab.compare_results(a=finished, b=other)
    assert 'antenna_only_band_worst' in out['measurements'] and out['measurements']['antenna_only_band_worst']['unit'] == 'dB' and 'b minus a' in out['note']
    same = await lab.compare_results(a=finished, b=finished)
    assert same['measurements']['antenna_only_band_worst']['value'] == 0
    confirm = await run(lab, scenario(rf, quality='confirm'))
    mixed = await lab.compare_results(a=finished, b=confirm)
    assert 'agree' in mixed['screen_vs_confirm']


async def test_tune_matching_returns_standard_values_for_the_engineer(lab, finished):
    out = await lab.tune_matching(review_id=None, version_id='v-nrf', result=finished, params={'band_hz': [2.4e9, 2.4835e9], 'target_db': -10})
    assert out['components'] and all(c['unit'] in ('nH', 'pF', 'ohm') and c['value'] > 0 for c in out['components'])
    assert set(out['predicted']) == {'s11_min_db', 'f_min_hz', 'band_worst_db', 'band_best_db', 'band_mean_db', 'band_worst_accepted_power_pct', 'band_mean_accepted_power_pct'} and 'own tool' in out['note']
    assert out['parts'] and all({'ref', 'position', 'kind', 'value', 'was', 'now'} <= set(p) for p in out['parts']) and out['dips'] is not None and out['zin_band_centre']['f_hz'] and out['curve']['freq_hz']
    # the same parts applied explicitly give the same band result: this is how the screen's parts are locked at confirm quality
    again = await lab.tune_matching(review_id=None, version_id='v-nrf', result=finished, params={'band_hz': [2.4e9, 2.4835e9], 'target_db': -10,
                                                                                               'changes': [{'ref': p['ref'], 'kind': p['kind'], 'value': p['value']} for p in out['parts']]})
    assert again['predicted']['band_worst_db'] == pytest.approx(out['predicted']['band_worst_db'], abs=0.01)
    with pytest.raises(LabUnavailable, match='not in the matching network'):
        await lab.tune_matching(review_id=None, version_id='v-nrf', result=finished, params={'changes': [{'ref': 'R99', 'kind': 'C', 'value': 1e-12}]})
    with pytest.raises(LabUnavailable):
        await lab.tune_matching(review_id=None, version_id='v-nrf', result={'job_id': 'nope'}, params={})


async def test_placement_what_if_measures_and_never_writes(lab, version):
    board = await lab.load_board(version_id='v-nrf')
    pcb = next((lab.store.version_dir('v-nrf') / 'project').rglob('*.kicad_pcb'))
    before = hashlib.sha256(pcb.read_bytes()).hexdigest()
    fp = next(f for f in board.footprints if f.ref.startswith('C') and f.courtyard)
    other = next(f for f in board.footprints if f.ref.startswith('U'))
    near = await lab.try_placement(version_id='v-nrf', ref=fp.ref, target=other.ref)
    for key in ('ref', 'new_position', 'distances', 'overlaps', 'conflicts', 'ghost', 'rough_length_change_mm', 'connections', 'rf_effect'):
        assert key in near
    assert near['rf_effect'] == 'Not tested' and near['ghost']['ring'] and near['ghost']['pads'] and 'beside' in near['placed']
    exact = await lab.try_placement(version_id='v-nrf', ref=fp.ref, target=[fp.x + 3.0, fp.y], rotation=fp.rotation + 90)
    assert exact['new_position'] == [round(fp.x + 3.0, 4), round(fp.y, 4)] and exact['moved_mm'] == 3.0 and exact['rotation'] == round(fp.rotation + 90, 3)
    onto = await lab.try_placement(version_id='v-nrf', ref=fp.ref, target=[other.x, other.y])
    assert onto['overlaps'] and not onto['ok'] and any('Courtyard overlaps' in c for c in onto['conflicts'])
    outside = await lab.try_placement(version_id='v-nrf', ref=fp.ref, target=[-30.0, -30.0])
    assert any('outside the board outline' in c for c in outside['conflicts'])
    assert hashlib.sha256(pcb.read_bytes()).hexdigest() == before and not list((lab.store.version_dir('v-nrf')).rglob('*.kicad_pcb-bak'))
    with pytest.raises(ValueError):
        await lab.try_placement(version_id='v-nrf', ref=fp.ref, target=fp.ref)


async def test_datasheets_are_listed_and_read_page_by_page(lab, version):
    assert (await lab.read_datasheet(version_id='v-nrf'))['names'] == []
    folder = lab.store.version_dir('v-nrf') / 'project' / 'datasheets'
    folder.mkdir()
    (folder / 'radio.pdf').write_bytes(tiny_pdf(['Antenna feed impedance is 50 ohm', 'Second page about the crystal']))
    names = (await lab.read_datasheet(version_id='v-nrf'))['names']
    assert names == ['datasheets/radio.pdf']
    page = await lab.read_datasheet(version_id='v-nrf', name='radio.pdf', page=2)
    assert page['pages'] == 2 and page['page'] == 2 and 'crystal' in page['text']
    with pytest.raises(LabUnavailable, match='2 pages'):
        await lab.read_datasheet(version_id='v-nrf', name='radio', page=3)
    with pytest.raises(LabUnavailable, match='No datasheet named'):
        await lab.read_datasheet(version_id='v-nrf', name='other.pdf')
    (folder / 'broken.pdf').write_bytes(b'not a pdf')
    with pytest.raises(LabUnavailable, match='could not be read'):
        await lab.read_datasheet(version_id='v-nrf', name='broken.pdf')


def test_job_dir_finds_the_same_named_folder_when_the_data_folder_moved(store, tmp_path):
    """A job row keeps an absolute folder. After the data folder is moved or copied that path lies outside it: the same-named folder inside is used."""
    lab = RealLab(store, queue=SimQueue(store, docker='docker'))
    name = 'ab12cd34ef56ab12cd34-5cf3ad05'
    inside = store.scenario_dir(name)
    (inside / 's11.json').write_text('{}')
    old = tmp_path / 'old-data' / 'scenarios' / name
    old.mkdir(parents=True)
    (old / 's11.json').write_text('{}')
    job = store.create_sim_job(name, job_dir=old, status='done')
    assert lab.job_dir({'job_id': job['id']}) == inside.resolve()
    assert lab.job_dir({'job_id': job['id'], 'scenario_hash': '0123456789abcdef'}) == inside.resolve()       # an evidence key that names no folder is ignored


def test_job_dir_is_none_when_no_same_named_folder_exists_inside_the_data_folder(store, tmp_path):
    lab = RealLab(store, queue=SimQueue(store, docker='docker'))
    elsewhere = tmp_path / 'old-data' / 'scenarios' / 'ffeeddccbbaa99887766'
    elsewhere.mkdir(parents=True)
    (elsewhere / 's11.json').write_text('{}')
    job = store.create_sim_job('ffeeddccbbaa99887766', job_dir=elsewhere, status='done')
    assert lab.job_dir({'job_id': job['id']}) is None
    assert lab.job_dir({'job_id': 'no-such-job'}) is None
