"""Success is judged in band, matching is an experiment, a combined candidate is confirmed as a pair, and an investigation does not stop early."""
import json

import pytest

from gate_steps import clear
from trustpcb.review import evidence as ev
from trustpcb.review.budgets import config_for

BRIEF = {'tool': 'submit_board_brief', 'args': {'purpose': 'Battery powered Bluetooth sensor node', 'part_roles': {'U3': 'radio'}, 'risks': [], 'priorities': ['antenna']}}
SUSPECTS = [{'title': 'Antenna length', 'why': 'The resonance may sit away from the band.'}, {'title': 'Matching network', 'why': 'The reactance at the band centre is large.'},
            {'title': 'Nearby copper', 'why': 'Ground copper next to the feed can detune the antenna.'}]
OPEN = {'tool': 'open_investigation', 'args': {'question': 'Why does the antenna miss the band?', 'suspects': SUSPECTS}}
PRE = [BRIEF, {'tool': 'advance_phase'}] + clear() + [{'tool': 'advance_phase'}, OPEN]
BASE = {'tool': 'run_simulation', 'args': {'suspect_id': 'S1', 'label': 'base'}}
MATCH = {'tool': 'tune_matching', 'args': {'ref': 'R1', 'suspect_id': 'S2'}}
CLOSE_INCONCLUSIVE = {'tool': 'close_investigation', 'args': {'investigation_id': 'I1', 'outcome': 'inconclusive', 'summary': 'Nothing settled the question.'}}


def shorten(delta=-7.0, suspect='S1'):
    return {'tool': 'run_simulation', 'args': {'suspect_id': suspect, 'label': f'shorten {abs(delta):g} mm', 'whatif_ops': [{'op': 'adjust_antenna_length', 'delta_mm': delta}]}}


def contents(env, review, name):
    return [json.loads(e['payload']['content']) for e in env.events(review['chat_id'], 'tool_result') if e['payload']['name'] == name]


def fake_run(worst, **extra):
    """A run result the way the real lab shapes it, for the evidence helpers."""
    base = {'f_res': 1.87e9, 's11_min_db': -16.8, 'band_worst_s11_db': worst, 'band_mean_s11_db': worst - 1, 'trust': {'status': 'ok', 'reasons': []}}
    return {'status': 'done', 'result': {**base, **extra}}


# ---- success is the band, not the deepest dip -----------------------------------------------------------------------------------------------

def test_the_deepest_dip_does_not_decide_success():
    deep_dip_out_of_band = ev.metrics_from_result(fake_run(-1.3, s11_min_db=-23.0))
    shallow_dip_in_band = ev.metrics_from_result(fake_run(-4.0, s11_min_db=-4.5, f_res=2.44e9))
    assert ev.objective(deep_dip_out_of_band) == -1.3 and ev.objective(shallow_dip_in_band) == -4.0
    delta = ev.delta(shallow_dip_in_band, deep_dip_out_of_band)
    assert delta['s11_db'] == -2.7 and ev.is_improvement(delta)                  # a much shallower dip, but 2.7 dB better across the band
    assert ev.delta(deep_dip_out_of_band, shallow_dip_in_band)['s11_db'] == 2.7 and not ev.is_improvement(ev.delta(deep_dip_out_of_band, shallow_dip_in_band))
    goal = {'target_s11_db': -10}
    assert not ev.meets_target(deep_dip_out_of_band, goal) and ev.meets_target(ev.metrics_from_result(fake_run(-10.4, s11_min_db=-10.6)), goal)
    assert not ev.meets_target({'trust_status': 'ok', 's11_min_db': -30.0, 'f_res_hz': 2.44e9}, goal)          # no band numbers: a deep dip alone never meets the target
    assert ev.objective({'band_mean_s11_db': -5.0, 's11_min_db': -30.0}) == -5.0                                # the mean in band comes before the deepest dip


def test_the_threshold_is_one_decibel_and_ties_go_to_the_better_mean():
    assert ev.is_improvement({'s11_db': -1.0}) and not ev.is_improvement({'s11_db': -0.99}) and not ev.is_improvement({'s11_db': 2.0})
    assert ev.rank_key({'s11_db': -3.0, 'band_mean_db': -4.0}) > ev.rank_key({'s11_db': -3.0, 'band_mean_db': -2.0}) > ev.rank_key({'s11_db': -2.0, 'band_mean_db': -9.0})


def test_screen_and_confirm_agree_in_band_not_on_the_deepest_dip():
    screen, confirm = ev.metrics_from_result(fake_run(-1.3, f_res=1.866e9)), ev.metrics_from_result(fake_run(-0.5, f_res=2.3e9))
    assert ev.screen_confirm_agree(screen, confirm)[0]                          # the deepest dip jumped 400 MHz between runs; the band result did not
    far = ev.metrics_from_result(fake_run(-6.0))
    assert not ev.screen_confirm_agree(screen, far)[0]
    resonance = {**screen, 'first_series_resonance_hz': 1.67e9}
    assert not ev.screen_confirm_agree(resonance, {**confirm, 'first_series_resonance_hz': 1.9e9})[0]


def test_dips_that_stay_put_and_the_character_of_a_reactance():
    a = [{'f_hz': 1.866e9, 's11_db': -16.8}, {'f_hz': 2.254e9, 's11_db': -4.0}]
    b = [{'f_hz': 2.2535e9, 's11_db': -6.8}, {'f_hz': 2.709e9, 's11_db': -5.5}]
    fixed = ev.fixed_dips(a, b)
    assert len(fixed) == 1 and fixed[0]['f_hz'] == pytest.approx(2.2538e9, rel=1e-3) and abs(fixed[0]['moved_hz']) < 1e6
    assert ev.fixed_dips(a, [{'f_hz': 2.0e9, 's11_db': -5}]) == []
    assert [ev.reactance_character(10.0, 88.0), ev.reactance_character(10.0, -40.0), ev.reactance_character(50.0, 2.0)] == ['inductive', 'capacitive', 'resistive']


def test_metrics_carry_the_band_mean_and_the_impedance_at_the_band_centre():
    run = {'status': 'done', 'result': {'f_res': 1.87e9, 'trust': {'status': 'ok'}}, 'zin_at_band_centre': {'f_hz': 2.44175e9, 're': 10.4, 'im': 88.1},
           's11': {'as_built': {'band_worst_db': -0.5, 'band_mean_db': -0.53, 'band_best_db': -0.6}, 'antenna_only': {'band_worst_db': -0.8, 'band_mean_db': -0.9, 'band_best_db': -1.0}}}
    metrics = ev.metrics_from_result(run)
    assert metrics['band_worst_s11_db'] == -0.5 and metrics['band_mean_s11_db'] == -0.53 and metrics['centre_re'] == 10.4 and metrics['centre_im'] == 88.1
    measurements = ev.measurements_of(metrics)
    assert measurements['band_centre_reactance'] == {'value': 88.1, 'unit': 'ohm'} and measurements['s11_band_mean']['value'] == -0.53 and measurements['band_centre_frequency']['value'] == 2441.75


# ---- what the agent reads ------------------------------------------------------------------------------------------------------------------

async def test_a_run_states_the_band_result_the_impedance_and_every_dip(env):
    review = await env.review(PRE + [BASE])
    base = contents(env, review, 'run_simulation')[0]
    assert base['in_band']['worst_s11_db'] == -3.1 and base['in_band']['mean_s11_db'] == -4.6 and base['in_band']['band_mhz'] == [2400.0, 2483.5] and base['in_band']['meets_target'] is False
    assert 'deepest dip is only a description' in base['in_band']['how_to_read']
    assert base['band_centre_impedance'] == {'resistance_ohm': 31.0, 'reactance_ohm': 62.0, 'character': 'inductive', 'seen': 'at the antenna feed, before the matching parts',
                                             'meaning': ev.CHARACTER_MEANING['inductive'], 'frequency_mhz': 2441.75}
    assert len(base['rf_detail']['s11_dips']) == 2 and base['rf_detail']['first_series_resonance_mhz'] == 1700.0
    assert base['measurements']['band_centre_reactance']['value'] == 62.0 and base['measurements']['s11_band_mean']['value'] == -4.6


# ---- matching is an experiment ------------------------------------------------------------------------------------------------------------

async def test_matching_is_cached_counted_and_follows_the_duplicate_rule(env):
    steps = PRE + [BASE, MATCH, MATCH, MATCH, MATCH, MATCH]
    review = await env.review(steps)
    results = contents(env, review, 'tune_matching')
    assert results[0]['cached'] is False and results[0]['kind'] == 'matching'
    assert results[1]['cached'] is True and results[1]['free'] is True and results[1]['ref'] == results[0]['ref']
    assert results[2]['cached'] is True and 'suspect_closed' not in results[2] and results[3]['cached'] is True and results[3]['suspect_closed']['status'] == 'looped'     # the third repeat closes the suspect, like a simulation
    assert results[4]['error'] == 'suspect_closed'
    assert env.lab.count('tune_matching') == 1                                                # the repeats never reached the lab
    assert review['budget']['duplicates'] == 3 and review['budget']['experiments']['used'] == 1
    assert any(l['rule'] == 'duplicate_scenario' for l in review['summary']['limits'])
    suspect = env.store.list_suspects(env.store.list_investigations(review['id'])[0]['id'])[1]
    assert suspect['status'] == 'looped'


async def test_matching_counts_against_the_experiment_budget(env):
    env.lab.match_gain['screen'] = 3.0                           # a clear gain that does not meet the target, so the good-enough rule stays out of the way
    explicit = [{'tool': 'tune_matching', 'args': {'ref': 'R1', 'suspect_id': 'S2', 'parts': [{'ref': 'L1', 'kind': 'C', 'value': v}]}} for v in (0.5, 0.75, 1.0, 1.2, 1.5, 1.8)]
    review = await env.review(PRE + [BASE] + explicit)
    results = contents(env, review, 'tune_matching')
    assert [r.get('kind') for r in results[:6]] == ['matching'] * 5 + [None] and results[5]['error'] == 'budget_exhausted' and 'experiments' in results[5]['message']
    assert [r['experiments_left'] for r in results[:5]] == [4, 3, 2, 1, 0]
    assert env.lab.count('tune_matching') == 5 and review['budget']['experiments']['used'] == 5
    assert [c[1] for c in env.lab.calls if c[0] == 'tune_matching'] == ['locked'] * 5            # explicit parts were applied, not searched


async def test_matching_can_be_the_winner_and_supports_confirming_a_suspect(env):
    review = await env.review(PRE + [BASE, MATCH, {'tool': 'conclude_suspect', 'args': {'suspect_id': 'S1', 'verdict': 'confirmed', 'reasoning': 'The antenna length is the cause.'}},
                                     {'tool': 'conclude_suspect', 'args': {'suspect_id': 'S2', 'verdict': 'confirmed', 'reasoning': 'Matching parts change the band result clearly.'}}])
    first, second = contents(env, review, 'conclude_suspect')
    assert first['error'] == 'not_supported' and 'trusted experiment' in first['message']        # S1 has no experiment of its own
    assert second['status'] == 'confirmed'


async def test_a_matching_experiment_that_does_nothing_is_no_progress(env):
    env.lab.match_gain['screen'] = 0.2
    tries = [{'tool': 'tune_matching', 'args': {'ref': 'R1', 'suspect_id': 'S2', 'parts': [{'ref': 'L1', 'kind': 'C', 'value': v}]}} for v in (0.5, 0.75, 1.0)]
    review = await env.review(PRE + [BASE] + tries)
    results = contents(env, review, 'tune_matching')
    assert 'no_effect' in json.dumps(results[1]['notices']) and results[2]['error'] == 'suspect_closed'
    assert any(l['rule'] == 'no_progress' for l in review['summary']['limits'])


async def test_matching_on_a_screen_what_if_makes_a_combined_candidate_that_is_confirmed_as_a_pair(env, base_model):
    def model(scenario):
        result = base_model(scenario)
        if scenario['quality'] == 'screen' and scenario.get('whatif_ops'):
            result['band_worst_s11_db'] -= 2.0                        # shortening helps a little across the band
        return result
    env.lab.model = model
    finding = {'tool': 'record_finding', 'args': {'title': 'Length and matching together bring the antenna on the band', 'severity': 'high', 'what': 'In the screen comparison a shorter antenna with a changed series part improves the band result.',
                                                 'why_it_matters': 'Out-of-band antennas waste transmit power.', 'recommendation': 'Try both changes in your own tool.', 'evidence_ids': ['E-003'], 'targets': ['ref:L1']}}
    steps = PRE + [BASE, shorten(-7.0), {'tool': 'tune_matching', 'args': {'ref': 'R2', 'suspect_id': 'S2'}}, finding,
                   {'tool': 'close_investigation', 'args': {'investigation_id': 'I1', 'outcome': 'improved', 'summary': 'The pair improves the band result.', 'finding_code': 'F-001'}},
                   {'tool': 'advance_phase'}, {'tool': 'confirm_result', 'args': {'investigation_id': 'I1', 'which': 'base'}}, {'tool': 'confirm_result', 'args': {'investigation_id': 'I1', 'which': 'winner'}}]
    review = await env.review(steps)
    matching = contents(env, review, 'tune_matching')[0]
    assert matching['applied_to'] == 'R2' and matching['ref'] == 'R3'
    outcome = env.store.list_investigations(review['id'])[0]['outcome']
    assert outcome['winner'] == 'R3' and outcome['winner_change']['ops'] == [{'op': 'adjust_antenna_length', 'delta_mm': -7.0}] and outcome['winner_change']['parts'][0]['ref'] == 'L1'
    assert env.lab.count('run_scenario') == 4                          # base, shorten, confirm base, confirm of the shortened geometry: a pair needs its own run
    assert [c[1:] for c in env.lab.calls if c[0] == 'tune_matching'] == [('search', 'screen'), ('locked', 'confirm'), ('search', 'confirm')]
    confirmation = outcome['confirmation']
    assert confirmation['winner']['ops'] and confirmation['winner']['parts'] and confirmation['outcome'] in ('held', 'held_smaller')
    assert confirmation['rows']['winner']['confirm']['band_worst_s11_db'] == confirmation['matching_at_confirm']['locked']['band_worst_s11_db']        # the pair's result is the locked matching
    assert confirmation['confirm_gain_db'] == pytest.approx(8.14, abs=0.01)           # -2.9 dB confirmed base, the shorter antenna with the locked parts at -11.04 dB


async def test_matching_on_a_confirm_result_is_a_calculation_not_an_experiment(env):
    steps = PRE + [BASE, shorten(), MATCH,
                   {'tool': 'record_finding', 'args': {'title': 'Matching parts bring the antenna in band', 'severity': 'high', 'what': 'In the screen comparison different series parts improve the band result.', 'why_it_matters': 'It saves range.',
                                                      'recommendation': 'Try other series parts in your own tool.', 'evidence_ids': ['E-003'], 'targets': ['ref:L1']}},
                   {'tool': 'close_investigation', 'args': {'investigation_id': 'I1', 'outcome': 'improved', 'summary': 'Matching improves the band result.', 'finding_code': 'F-001'}},
                   {'tool': 'advance_phase'}, {'tool': 'confirm_result', 'args': {'investigation_id': 'I1', 'which': 'base'}}, {'tool': 'tune_matching', 'args': {'ref': 'R4'}},
                   {'tool': 'tune_matching', 'args': {'ref': 'R3'}}, {'tool': 'tune_matching', 'args': {'ref': 'R1'}}, {'tool': 'view_result', 'args': {'ref': 'R3', 'kind': 's11'}}]
    review = await env.review(steps)
    results = contents(env, review, 'tune_matching')
    calc, on_matching, on_screen, view = results[1], results[2], results[3], contents(env, review, 'view_result')[0]
    assert 'not_an_experiment' in calc and calc['in_band']['worst_s11_db'] < -10 and 'ref' not in calc
    assert on_matching['error'] == 'not_a_run' and view['error'] == 'not_a_run'
    assert 'not_an_experiment' in on_screen and 'ref' not in on_screen         # a screen result after the investigation closed is a calculation too, never a refusal
    assert review['budget']['experiments']['used'] == 2                      # the shorten run and the first matching experiment only


async def test_matching_that_cannot_run_is_reported_and_does_not_block_closing(env):
    env.lab.matching_error = 'The matching network at the feed is not understood.'
    steps = PRE + [BASE, shorten(), MATCH, shorten(-3.0, 'S3'), {'tool': 'close_investigation', 'args': {'investigation_id': 'I1', 'outcome': 'no_improvement', 'summary': 'Nothing changed the band result.'}}]
    review = await env.review(steps)
    assert contents(env, review, 'tune_matching')[0]['error'] == 'unavailable'
    assert contents(env, review, 'close_investigation')[0]['outcome'] == 'no_improvement'


async def test_parts_that_cannot_be_applied_are_a_mistake_to_correct_not_a_reason_to_skip_matching(env):
    env.lab.matching_bad = 'these parts are not in the matching network: R99'
    env.lab.match_gain['screen'] = 0.3
    wrong = {'tool': 'tune_matching', 'args': {'ref': 'R1', 'suspect_id': 'S2', 'parts': [{'ref': 'R99', 'kind': 'C', 'value': 1.0}]}}
    zero = {'tool': 'tune_matching', 'args': {'ref': 'R1', 'suspect_id': 'S2', 'parts': [{'ref': 'L1', 'kind': 'C', 'value': 0.0}]}}
    review = await env.review(PRE + [BASE, shorten(), wrong, zero, CLOSE_INCONCLUSIVE])
    wrong_result, zero_result = contents(env, review, 'tune_matching')
    assert wrong_result['error'] == 'parts_not_allowed' and 'R99' in wrong_result['message'] and zero_result['error'] == 'invalid_arguments'
    assert contents(env, review, 'close_investigation')[0]['error'] == 'investigation_incomplete'          # matching still has to be tried


async def test_matching_needs_a_trusted_result_and_a_known_ref(env):
    env.lab.fail_trust = 1
    review = await env.review(PRE + [BASE, {'tool': 'tune_matching', 'args': {'ref': 'R9'}}, MATCH])
    unknown, untrusted = contents(env, review, 'tune_matching')
    assert unknown['error'] == 'unknown_result' and untrusted['error'] == 'trust_failed'
    assert env.lab.count('tune_matching') == 0


# ---- an investigation does not stop early -----------------------------------------------------------------------------------------------

async def test_an_improvement_cannot_be_closed_as_inconclusive(env):
    review = await env.review(PRE + [BASE, MATCH, CLOSE_INCONCLUSIVE])
    refused = contents(env, review, 'close_investigation')[0]
    assert refused['error'] == 'improvement_found' and refused['winner'] == 'R2' and 'close as improved' in refused['message'] and 'confirm quality' in refused['message']
    assert env.store.list_investigations(review['id'])[0]['status'] != 'closed'


async def test_closing_without_a_result_asks_for_a_matching_experiment_and_two_tested_suspects(env):
    env.lab.match_gain['screen'] = 0.3
    review = await env.review(PRE + [BASE, shorten(), CLOSE_INCONCLUSIVE])
    refused = contents(env, review, 'close_investigation')[0]
    assert refused['error'] == 'investigation_incomplete' and len(refused['missing']) == 2 and refused['experiments_left'] == 4
    assert 'matching experiment' in refused['missing'][0] and 'only 1 of 2 required suspects tested' in refused['missing'][1]
    assert env.store.list_investigations(review['id'])[0]['status'] != 'closed'        # it stayed open: the script ended with it


async def test_matching_alone_is_not_enough_two_suspects_must_be_tested(env):
    env.lab.match_gain['screen'] = 0.3
    review = await env.review(PRE + [BASE, MATCH, CLOSE_INCONCLUSIVE])
    refused = contents(env, review, 'close_investigation')[0]
    assert refused['error'] == 'investigation_incomplete' and refused['missing'] == ['only 1 of 2 required suspects tested; not tested yet: S1 (Antenna length), S3 (Nearby copper)']


async def test_an_investigation_with_one_suspect_needs_only_that_suspect_and_a_matching_experiment(env):
    env.lab.match_gain['screen'] = 0.3
    one = {'tool': 'open_investigation', 'args': {'question': 'Why does the antenna miss the band?', 'suspects': SUSPECTS[:1]}}
    review = await env.review([BRIEF, {'tool': 'advance_phase'}] + clear() + [{'tool': 'advance_phase'}, one, BASE, shorten(), CLOSE_INCONCLUSIVE, {'tool': 'tune_matching', 'args': {'ref': 'R1'}}, CLOSE_INCONCLUSIVE])
    first, second = contents(env, review, 'close_investigation')
    assert first['error'] == 'investigation_incomplete' and len(first['missing']) == 1 and 'matching experiment' in first['missing'][0]
    assert second['outcome'] == 'inconclusive'


async def test_the_refusal_gives_way_when_a_budget_ends(env):
    env.lab.solve_s['screen'] = 1300.0                         # two screen runs use the 2400 s of solver time
    review = await env.review(PRE + [BASE, shorten(), CLOSE_INCONCLUSIVE])
    assert contents(env, review, 'close_investigation')[0]['outcome'] == 'inconclusive'
    assert any(l['rule'] == 'budget_wall' for l in review['summary']['limits'])


async def test_the_refusal_gives_way_when_the_experiments_are_used(env):
    env.lab.match_gain['screen'] = 0.3
    steps = PRE + [BASE] + [shorten(d) for d in (-1.0, -3.0, -5.0, -8.0, -10.0)] + [CLOSE_INCONCLUSIVE]
    review = await env.review(steps)
    closing = contents(env, review, 'close_investigation')[0]
    assert closing.get('outcome') == 'inconclusive', closing


async def test_six_refusals_in_a_row_still_stop_the_agent(env):
    env.lab.match_gain['screen'] = 0.3
    review = await env.review(PRE + [BASE, shorten()] + [CLOSE_INCONCLUSIVE] * 8)
    results = contents(env, review, 'close_investigation')
    assert [r['error'] for r in results[:6]] == ['investigation_incomplete'] * 6 and results[6]['error'] == 'agent_stopped'
    assert review['end_state'] == 'agent_stopped' and review['summary']['end']['reason'] == 'loop'
    assert review['budget']['tool_calls']['4'] >= 9                      # each refusal cost a phase 4 call


def test_only_standard_requires_the_full_method():
    assert config_for('standard').full_investigation is True and config_for('deep').full_investigation is False and config_for('quick').full_investigation is False


# ---- screen numbers: allowed where the sentence says they are screen results, banned in findings and the summary ----------------------

async def test_a_screen_number_may_be_quoted_in_a_conclusion_when_it_is_labelled(env):
    labelled = {'tool': 'conclude_suspect', 'args': {'suspect_id': 'S2', 'verdict': 'confirmed', 'reasoning': 'In the screen run the worst in-band S11 reaches -12.1 dB with the new part.'}}
    review = await env.review(PRE + [BASE, MATCH, labelled])
    assert contents(env, review, 'conclude_suspect')[0]['status'] == 'confirmed'


async def test_an_unlabelled_screen_number_is_still_rejected_in_a_conclusion(env):
    bare = {'tool': 'conclude_suspect', 'args': {'suspect_id': 'S2', 'verdict': 'confirmed', 'reasoning': 'The match is good. The worst in-band S11 reaches -12.1 dB.'}}
    invented = {'tool': 'conclude_suspect', 'args': {'suspect_id': 'S2', 'verdict': 'confirmed', 'reasoning': 'In the screen run the worst in-band S11 reaches -17.3 dB.'}}
    review = await env.review(PRE + [BASE, MATCH, bare, invented])
    first, second = contents(env, review, 'conclude_suspect')
    assert first['error'] == 'number_check_failed' and first['reason'] == 'needs_confirm_run'
    assert second['error'] == 'number_check_failed' and second['reason'] == 'not_in_evidence'          # labelling does not make an invented number acceptable


async def test_closing_an_investigation_may_quote_a_labelled_screen_number_but_findings_may_not(env):
    finding = {'tool': 'record_finding', 'args': {'title': 'Matching parts bring the antenna in band', 'severity': 'high', 'what': 'In the screen run the worst in-band S11 reaches -12.1 dB.', 'why_it_matters': 'It saves range.',
                                                 'recommendation': 'Try other series parts in your own tool.', 'evidence_ids': ['E-002'], 'targets': ['ref:L1']}}
    fixed = {'tool': 'record_finding', 'args': {**finding['args'], 'what': 'In the screen comparison different series parts improve the band result.'}}
    close = {'tool': 'close_investigation', 'args': {'investigation_id': 'I1', 'outcome': 'improved', 'summary': 'In the screen run the worst in-band S11 reaches -12.1 dB, a clear gain over the base run.', 'finding_code': 'F-001'}}
    review = await env.review(PRE + [BASE, MATCH, finding, fixed, close])
    banned, accepted = contents(env, review, 'record_finding')
    assert banned['error'] == 'number_check_failed' and banned['reason'] == 'needs_confirm_run' and accepted['code'] == 'F-001'
    assert contents(env, review, 'close_investigation')[0]['outcome'] == 'improved'
