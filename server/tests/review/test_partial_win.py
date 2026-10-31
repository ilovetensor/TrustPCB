"""The partial win (a confirm gain of 3 dB or more in band that still misses the target), accepted power, and fixed dips."""
import json
from pathlib import Path

import pytest

from test_refine import antenna, contents, errors

SCRIPTS = Path(__file__).parent / 'scripts'


def accepted_pct(db):
    """100 x (1 - |S11|^2) for an S11 given in dB, rounded as the server rounds it (the same formula as the fake lab's model)."""
    return round(100.0 * (1.0 - 10 ** (db / 10.0)), 1)
DECLINE = {'tool': 'advance_phase', 'args': {'decline_refine': 'The confirm base E-003 and the confirm winner E-004 do not meet the target, but the second geometry attempt is not warranted here.'}}


def first_steps():
    """Through the confirm of the winner in the Deep decline script: a geometry winner that held at the screen only."""
    steps = json.loads((SCRIPTS / 'refine_decline.json').read_text())
    cut = next(i for i, x in enumerate(steps) if x.get('tool') == 'advance_phase' and (x.get('args') or {}).get('decline_refine'))
    return steps[:cut]


@pytest.fixture
def partial_env(env, base_model):
    env.lab.model = antenna(base_model)
    env.lab.match_gain['confirm_free'] = 5.0                  # a free re-tune at confirm quality gains 5 dB in band and still misses the target
    return env


async def test_a_confirm_gain_of_3_db_that_misses_the_target_is_a_partial_win(partial_env):
    env = partial_env
    review = await env.review(first_steps() + [DECLINE], depth='deep')
    confirmation = env.store.list_investigations(review['id'])[0]['outcome']['confirmation']
    assert confirmation['outcome'] == 'improved_short_of_target' and confirmation['hold'] == 'held_after_retune' and confirmation['meets_target'] is False
    best, base = confirmation['best'], confirmation['base']
    assert best['mode'] == 'free_retune' and best['band_worst_s11_db'] == -7.9 and base['band_worst_s11_db'] == -2.9 and confirmation['best_gain_db'] == 5.0
    assert best['accepted_power_worst_pct'] == accepted_pct(-7.9) and base['accepted_power_worst_pct'] == accepted_pct(-2.9)
    assert best['parts'][0]['ref'] == 'L1' and best['evidence'] and confirmation['base_evidence'] and confirmation['comparison_evidence']
    assert 'target is not met' in confirmation['statement'] and 'partial win' in confirmation['statement']


async def test_advance_is_refused_until_the_finding_reports_the_partial_win(partial_env):
    env = partial_env
    review = await env.review(first_steps() + [DECLINE], depth='deep')
    refused = contents(env, review, 'advance_phase')[-1]
    assert refused['error'] == 'partial_win_unreported' and refused['finding'] == 'F-001'
    wanted = ' '.join(refused['missing'])
    assert 'accepted power before' in wanted and 'accepted power after' in wanted and 'target is not met' in wanted and 'L1' in wanted
    finding = env.store.get_finding_by_code(review['id'], 'F-001')                       # the confirm rows were attached to the finding by the server already
    assert set(refused['evidence']) <= {env.store.get_evidence(i)['code'] for i in finding['evidence_ids']}
    assert 'mismatch figure only' in refused['facts'] and refused['best_parts'] and refused['evidence']
    assert review['phase'] != '6' or review['end_state'] != 'complete'


async def test_a_finding_that_reports_it_passes_and_the_report_leads_with_it(partial_env):
    from trustpcb.review import report
    env = partial_env
    base_pct, best_pct = accepted_pct(-2.9), accepted_pct(-7.9)
    update = {'tool': 'record_finding', 'args': {
        'update': 'F-001', 'title': 'Ground copper next to the feed detunes the antenna', 'severity': 'high',
        'what': f'At confirm quality the worst in-band S11 improves from -2.9 dB to -7.9 dB with a re-tuned series part, a gain of 5.0 dB. Accepted power at the worst point of the band goes from {base_pct} % to {best_pct} % (a mismatch figure only). The target is not met.',
        'why_it_matters': 'A detuned antenna wastes transmit power.',
        'recommendation': 'In your own tool, replace L1 with a 0.75 pF series capacitor and measure again. In simulation this is a partial win and the target is still not met.',
        'evidence_ids': ['E-003', 'E-004', 'E-005', 'E-006', 'E-007', 'E-008']}}
    probe = await env.review(first_steps() + [DECLINE], depth='deep')
    refused = contents(env, probe, 'advance_phase')[-1]
    update['args']['evidence_ids'] = refused['evidence']                                      # the confirm rows the server names
    review = await env.review(first_steps() + [update, DECLINE, {'tool': 'submit_summary', 'args': {
        'summary': 'Confirm improved the match but the target is not met. Replace L1 with a 0.75 pF series capacitor.', 'top_actions': ['Try the 0.75 pF series capacitor at L1 and measure again (F-001).']}}], depth='deep')
    assert errors(env, review) == [], errors(env, review)
    assert review['end_state'] == 'complete' and review['phase'] == '6'
    finding = env.store.get_finding_by_code(review['id'], 'F-001')
    assert 'target is not met' in finding['what'] and not any(l['rule'] == 'partial_win_completed_by_code' for l in review['summary']['limits'])
    snapshot = env.store.latest_report_revision(review['id'])['snapshot']
    assert snapshot['headline']['outcome'] == 'improved_short_of_target' and snapshot['headline']['finding'] == 'F-001' and snapshot['headline']['best']['mode'] == 'free_retune'
    html = (await report.render_revision(store=env.store, review=review, revision=env.store.latest_report_revision(review['id']), fmt='html')).decode()
    assert 'Improved, target not met' in html and html.index('Improved, target not met') < html.index('<h2>Summary</h2>') and 'Accepted power' in html
    assert html.index('Improved, target not met') < html.index('class="power"') < html.index('<h2>Summary</h2>')
    assert report.power_bars('x', [('a', None), ('b', 50)]) == ''
    assert 'width:100%' in report.power_bars('x', [('a', 130)])


async def test_after_two_refusals_the_server_adds_the_stored_result_to_the_finding(partial_env):
    env = partial_env
    review = await env.review(first_steps() + [DECLINE, DECLINE, DECLINE, {'tool': 'submit_summary', 'args': {'summary': 'The match improved a little.', 'top_actions': ['Measure again (F-001).']}}], depth='deep')
    results = contents(env, review, 'advance_phase')
    assert [r.get('error') for r in results[-3:-1]] == ['partial_win_unreported', 'partial_win_unreported'] and results[-1].get('error') is None
    finding = env.store.get_finding_by_code(review['id'], 'F-001')
    assert 'Confirmed result, written by the server from the stored evidence' in finding['recommendation'] and 'is not met' in finding['recommendation']
    assert 'L1' in finding['recommendation'] and 'mismatch figure only' in finding['recommendation']
    assert any(l['rule'] == 'partial_win_completed_by_code' for l in review['summary']['limits'])
    summary = contents(env, review, 'submit_summary')
    assert summary[0]['error'] == 'summary_must_lead_with_partial_win'               # the agent's summary did not lead with it, three tries later code writes it
    assert review['summary']['summary']['text'].startswith('Result: At confirm quality')


async def test_accepted_power_is_in_the_tool_results_and_quoting_it_passes_the_number_check(env):
    review = await env.review(SCRIPTS / 'matching_win.json')
    base = contents(env, review, 'run_simulation')[0]
    assert base['measurements']['band_worst_accepted_power_pct'] == {'value': accepted_pct(-3.1), 'unit': '%'}
    assert base['in_band']['worst_accepted_power_pct'] == accepted_pct(-3.1) and 'not radiated power' in base['in_band']['accepted_power_note']
    tuned = contents(env, review, 'tune_matching')[0]
    assert tuned['measurements']['band_worst_accepted_power_pct']['value'] == accepted_pct(-12.1)
    from trustpcb.review import evidence as ev
    from trustpcb.review.findings import check_numbers
    rows = [row for row in env.store.list_evidence(review['id']) if row['code'] == base['evidence']]
    allowed = [q for row in rows for q in ev.quantities_of(row)]
    check_numbers({'what': f'The screen run shows accepted power of {accepted_pct(-3.1)} % at the worst point of the band.'}, allowed, compare_only=True)


async def test_a_dip_that_stays_put_is_flagged_on_the_evidence_and_cannot_be_called_the_resonance(env):
    from trustpcb.review.findings import FindingRejected, check_fixed_dip_claims
    review = await env.review(SCRIPTS / 'matching_win.json')
    shorten = contents(env, review, 'run_simulation')[1]
    assert {d['frequency_mhz'] for d in shorten['fixed_dips']} == {2518.2, 2254.0} and shorten['measurements']['fixed_dip_2']['value'] == 2254.0
    assert any('unknown origin' in n for n in shorten['notices'])
    rows = env.store.list_evidence(review['id'])
    assert any('fixed_dip_2' in (r['values'].get('measurements') or {}) and r['values'].get('fixed_dips') for r in rows)
    with pytest.raises(FindingRejected) as error:
        check_fixed_dip_claims({'what': 'The antenna resonates at 2.254 GHz, below the band.'}, rows, rows)
    assert error.value.code == 'fixed_dip_not_resonance' and 'unknown origin' in error.value.message and 'first series resonance' in error.value.message
    check_fixed_dip_claims({'what': 'A dip at 2254 MHz stays where it was when the length changes, so it is not the antenna resonance.'}, rows, rows)
    check_fixed_dip_claims({'what': 'The antenna resonates at 1.7 GHz.'}, rows, rows)
    check_fixed_dip_claims({'what': 'The first series resonance is at 2254 MHz and the antenna resonates there.'}, rows, [{'values': {'measurements': {'first_series_resonance': {'value': 2250.0, 'unit': 'MHz'}}}}])
