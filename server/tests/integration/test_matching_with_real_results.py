"""The matching-as-experiment flow on real solver results: the nRF antenna's screen run and its confirm run, from the repository's fixtures.

No solver runs: both results are seeded into the cache under the scenario hashes the review asks for, so the scripted review gets real S11
curves, impedances and matching network from disk. This is the first live Standard review again, with the method the server now enforces.
"""
from __future__ import annotations

from trustpcb.review.findings import untested_line
import json
import shutil

import pytest

from helpers import BRIEF, GOAL, ROOT, VIEWS, make_test_lab, run_scripted_review, tool_results
from trustpcb.review.lab_real import RealLab
from trustpcb.review.sims import list_sims
from trustpcb.rf.queue import SimQueue

FIXTURES = ROOT / 'web' / 'public' / 'fixtures' / 'nrf-sim'

OPEN = {'tool': 'open_investigation', 'args': {'question': 'Does the antenna sit on the 2.4 GHz band as built?', 'suspects': [
    {'title': 'Antenna length', 'why': 'The deepest dip is far below the band.'}, {'title': 'Matching network', 'why': 'The impedance at the band centre is inductive.'},
    {'title': 'Board mode', 'why': 'A dip that does not move with length would be one.'}]}}
BASE = {'tool': 'run_simulation', 'args': {'suspect_id': 'S1', 'label': 'base'}}
MATCH = {'tool': 'tune_matching', 'args': {'ref': 'R1', 'suspect_id': 'S2'}}
FINDING = {'tool': 'record_finding', 'args': {
    'title': 'The matching network leaves the antenna out of band', 'severity': 'high', 'what': 'In the screen comparison a different series part at L1 improves the match across the band against the base.',
    'why_it_matters': 'An antenna that reflects most of the power in band shortens the radio link.', 'recommendation': 'Try a different series part at L1 in your own tool and measure the match again.',
    'evidence_ids': ['E-002'], 'targets': ['ref:L1']}}
STEPS = [BRIEF, {'tool': 'advance_phase'}, *VIEWS, {'tool': 'advance_phase'}, OPEN, BASE, MATCH,
         {'tool': 'conclude_suspect', 'args': {'suspect_id': 'S2', 'verdict': 'confirmed', 'reasoning': 'The screen comparison shows a clear gain from a different series part at L1.'}},
         FINDING, {'tool': 'close_investigation', 'args': {'investigation_id': 'I1', 'outcome': 'improved', 'summary': 'A matching change improves the band result in the screen comparison.', 'finding_code': 'F-001'}},
         {'tool': 'advance_phase'}, {'tool': 'confirm_result', 'args': {'investigation_id': 'I1', 'which': 'winner'}}]


def seed(store, job_hash, source, *, skip=('screen', 'whatif_shorten_6mm_screen')):
    """Put a finished solver result in the cache under `job_hash` (JSON only; the images are not needed here)."""
    destination = store.scenario_dir(job_hash)
    shutil.copytree(source, destination, dirs_exist_ok=True, ignore=shutil.ignore_patterns(*skip, '*.bin', '*.png'))
    progress = destination / 'progress.jsonl'
    if not progress.is_file():
        progress.write_text(json.dumps({'phase': 'done'}) + '\n')


async def seeded_lab(store, fake_docker):
    wrapper, _ = fake_docker
    queue = SimQueue(store, docker=str(wrapper), poll_s=0.1, timeout_s=30)
    lab = make_test_lab(RealLab, store, queue)
    rf = await lab.rf_status(version_id='v-nrf', goal=GOAL)
    hashes = {}
    for quality in ('screen', 'confirm'):
        scenario = {'version_id': 'v-nrf', 'antenna_id': rf['antenna_id'], 'band': rf['band_hz'], 'quality': quality, 'whatif_ops': [], 'material': None, 'region': rf['region_bbox']}
        hashes[quality] = (await lab._prepare('v-nrf', scenario))[3]
    seed(store, hashes['screen'], FIXTURES / 'screen')
    seed(store, hashes['confirm'], FIXTURES)
    return lab, queue, hashes


def payload(result):
    return json.loads(result['content'])


async def run_flow(store, fake_docker, steps=STEPS):
    lab, queue, hashes = await seeded_lab(store, fake_docker)
    review = await run_scripted_review(store, lab, steps)
    return review, queue


async def test_matching_experiment_on_the_real_base_then_an_honest_confirm(store, version, fake_docker):
    review, queue = await run_flow(store, fake_docker)
    assert queue.stats == {'submitted': 2, 'cache_hits': 2, 'ran': 0}, 'no solver time: the base and the confirm base come from the seeded results'

    base = payload(tool_results(store, review, 'run_simulation')[0])
    assert base['in_band']['worst_s11_db'] == pytest.approx(-1.32, abs=0.01) and base['in_band']['meets_target'] is False and 'as built' in base['in_band']['basis']
    impedance = base['band_centre_impedance']
    assert impedance['character'] == 'inductive' and impedance['resistance_ohm'] == pytest.approx(38.2, abs=0.1) and impedance['reactance_ohm'] == pytest.approx(71.2, abs=0.1)
    assert [round(d['frequency_mhz']) for d in base['rf_detail']['s11_dips']] == [1866, 1986, 2185, 2425, 2641] and base['rf_detail']['first_series_resonance_mhz'] == pytest.approx(1673.5, abs=0.1)
    assert 'inductive' in base['rf_summary'] and 'worst -1.324 dB' in base['rf_summary']

    tuned = payload(tool_results(store, review, 'tune_matching')[0])
    assert tuned['kind'] == 'matching' and tuned['ref'] == 'R2' and tuned['applied_to'] == 'R1' and tuned['solver_s'] == 0 and tuned['suspect']['code'] == 'S2'
    assert {p['part'] for p in tuned['parts_changed']} >= {'L1'} and tuned['in_band']['worst_s11_db'] < -10 and tuned['experiments_left'] == 4
    assert tuned['single_part_option']['changes'][0]['now'] == '0.75 pF' and tuned['single_part_option']['band_worst_db'] == pytest.approx(-11.37, abs=0.02)
    assert tuned['vs_base']['delta_s11']['value'] < -10 and any('improves the worst in-band S11' in n for n in tuned['notices'])

    investigation = store.list_investigations(review['id'])[0]
    assert investigation['outcome']['winner'] == 'R2' and investigation['outcome']['winner_change']['experiment'] == 'matching' and investigation['outcome']['winner_change']['parts']
    suspect = next(x for x in store.list_suspects(investigation['id']) if x['data']['code'] == 'S2')
    assert suspect['status'] == 'confirmed' and suspect['data']['experiments'][0]['kind'] == 'matching' and suspect['data']['experiments'][0]['parts']

    confirm = payload(tool_results(store, review, 'confirm_result')[0])
    c = confirm['confirmation']
    rows = c['rows']
    assert rows['base']['confirm']['band_worst_s11_db'] == pytest.approx(-0.503, abs=0.01) and rows['base']['confirm']['centre_reactance_ohm'] == pytest.approx(88.1, abs=0.1)
    assert rows['base']['screen']['band_worst_s11_db'] == pytest.approx(-1.324, abs=0.01)
    free, locked = c['matching_at_confirm']['free'], c['matching_at_confirm']['locked']
    assert free['band_worst_s11_db'] == pytest.approx(-6.12, abs=0.02) and free['meets_target'] is False         # the README's honest best at confirm quality
    assert locked['band_worst_s11_db'] > free['band_worst_s11_db'] and locked['parts'] == c['winner']['parts']       # the screen's own parts fare worse than a re-tune
    assert rows['winner']['screen']['band_worst_s11_db'] < -14 and rows['winner']['confirm']['band_worst_s11_db'] == pytest.approx(locked['band_worst_s11_db'])
    # the screen's own parts held smaller, and the free re-tune gains more than 3 dB in band yet misses the target: a partial win, with the finer verdict in `hold`
    assert c['hold'] == 'held_smaller' and c['agree_with_screen'] is False and c['meets_target'] is False
    assert c['outcome'] == 'improved_short_of_target' and 'screen overstated' in c['statement'] and 'target is not met' in c['statement']
    assert c['best']['mode'] == 'free_retune' and c['best']['band_worst_s11_db'] == pytest.approx(-6.12, abs=0.02) and c['best_gain_db'] == pytest.approx(5.6, abs=0.05)
    assert c['best']['accepted_power_worst_pct'] > c['base']['accepted_power_worst_pct'] + 50 and c['best']['meets_target'] is False
    assert {p['ref'] for p in c['best']['parts']} >= {'L1'}
    assert confirm['solver_s'] == 0 and queue.stats['ran'] == 0

    finding = store.get_finding_by_code(review['id'], 'F-001')
    assert untested_line(c['statement']) in finding['not_tested'] and c['statement'] not in finding['not_tested'] and len(finding['evidence_ids']) >= 5       # what is untested, not the result sentence
    assert investigation['outcome'] and store.list_investigations(review['id'])[0]['outcome']['confirmation']['outcome'] == 'improved_short_of_target'
    sims = list_sims(store, review['id'])
    assert [s['kind'] for s in sims] == ['base', 'confirm']              # matching calculations are not solver jobs


async def test_a_finding_about_the_antenna_must_quote_confirm_numbers_once_confirm_results_exist(store, version, fake_docker):
    late = {'tool': 'record_finding', 'args': {**FINDING['args'], 'title': 'A second reading of the same screen result', 'targets': ['ref:L1'], 'rule': 'second_reading'}}
    review, _ = await run_flow(store, fake_docker, STEPS + [late])
    refused = payload(tool_results(store, review, 'record_finding')[-1])
    assert refused['error'] == 'confirm_required' and 'confirm' in refused['message']
