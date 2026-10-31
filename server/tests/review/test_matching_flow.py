"""The method the server enforces: matching as an experiment, combined candidates, an honest confirm, and no early stop."""
import json
from pathlib import Path

from gate_steps import clear

SCRIPTS = Path(__file__).parent / 'scripts'


def contents(env, review, name):
    return [json.loads(e['payload']['content']) for e in env.events(review['chat_id'], 'tool_result') if e['payload']['name'] == name]


def errors(env, review):
    return [(e['payload']['name'], json.loads(e['payload']['content']).get('error')) for e in env.events(review['chat_id'], 'tool_result') if e['payload']['is_error']]


async def test_matching_win_is_a_real_experiment_and_is_confirmed_with_one_run(env):
    review = await env.review(SCRIPTS / 'matching_win.json')
    assert review['end_state'] == 'complete', (review['summary']['end'], errors(env, review))
    assert errors(env, review) == []
    assert env.lab.count('run_scenario') == 3                       # base, shorten 7 mm, and ONE confirm run: the matching-only winner shares the base geometry
    assert [c[1:] for c in env.lab.calls if c[0] == 'tune_matching'] == [('search', 'screen'), ('locked', 'confirm'), ('search', 'confirm')]
    matching = contents(env, review, 'tune_matching')[0]
    assert matching['kind'] == 'matching' and matching['ref'] == 'R3' and matching['applied_to'] == 'R1' and matching['solver_s'] == 0 and matching['experiments_left'] == 3
    assert matching['parts_changed'] == [{'part': 'L1', 'position': 'series', 'was': '2.2 nH', 'now': '0.75 pF'}]
    assert matching['in_band']['worst_s11_db'] == -12.1 and matching['in_band']['meets_target'] is True and matching['vs_base']['delta_s11']['value'] == -9.0
    assert matching['suspect'] == {'code': 'S2', 'status': 'open'}
    investigation = env.store.list_investigations(review['id'])[0]
    assert investigation['outcome']['winner'] == 'R3' and investigation['outcome']['winner_change'] == {'ops': [], 'parts': investigation['outcome']['winner_change']['parts'], 'experiment': 'matching'}
    assert investigation['outcome']['winner_change']['parts'][0]['ref'] == 'L1'
    confirmation = investigation['outcome']['confirmation']
    assert confirmation['outcome'] == 'held' and confirmation['confirm_gain_db'] == 8.0 and confirmation['meets_target'] is True
    assert confirmation['rows']['base']['confirm']['band_worst_s11_db'] == -2.9 and confirmation['rows']['winner']['confirm']['band_worst_s11_db'] == -10.9
    assert confirmation['matching_at_confirm']['locked']['parts'] == confirmation['winner']['parts'] and confirmation['matching_at_confirm']['free']['band_worst_s11_db'] == -12.4
    finding = env.store.get_finding_by_code(review['id'], 'F-001')
    assert finding['class'] == 'simulated' and 'did not hold' not in ' '.join(finding['not_tested'])
    assert 'Result is from screen-quality simulation runs only; no confirm run supports it.' not in finding['not_tested']
    spent = review['budget']['confirm_runs']
    assert spent['used'] == 1 and review['budget']['experiments']['used'] == 2        # the shorten run and the matching experiment; the base and the confirm runs are not experiments
    assert review['summary']['summary']['source'] == 'agent'


async def test_the_compare_tool_notes_a_dip_that_stays_put(env):
    review = await env.review(SCRIPTS / 'matching_win.json')
    compared = contents(env, review, 'compare_results')[0]
    assert 'delta_s11' in compared['b_minus_a'] and compared['in_band']['a']['worst_s11_db'] == -3.1
    assert compared['band_centre_impedance']['a']['character'] == 'inductive'
    assert any(abs(d['frequency_mhz'] - 2254.0) < 1 for d in compared['s11_dips']['b'])
    assert 'ground plane' in compared['board_mode_hint'] and compared['b_minus_a']['fixed_dip_2']['value'] == 2254.0
    assert compared['evidence'] and compared['trust']['status'] == 'ok'


async def test_the_report_shows_suspects_matching_and_the_screen_against_confirm_table(env):
    from trustpcb.review import report
    review = await env.review(SCRIPTS / 'matching_win.json')
    revision = env.store.latest_report_revision(review['id'])
    html = (await report.render_revision(store=env.store, review=review, revision=revision, fmt='html')).decode()
    assert 'Investigation story' in html and 'Matching network' in html and 'matching, screen' in html and 'no solver time' in html
    assert 'Screen against confirm' in html and 'Base, screen' in html and 'Change, confirm' in html and 'Worst S11 in the band' in html
    assert 'tested in simulation (apply it in your own tool and re-measure)' in html and 'L1 2.2 nH to 0.75 pF' in html
    assert 'Matching re-tuned at confirm quality' in html and 'Free tuning' in html and "Screen's parts locked" in html
    snapshot = revision['snapshot']
    assert snapshot['investigations'][0]['confirmation']['outcome'] == 'held'
    assert snapshot['rf']['confirm_matching']['locked'] and snapshot['rf']['confirm_matching']['free'] and snapshot['rf']['meets_target'] is True


async def test_a_screen_win_that_does_not_hold_at_confirm_is_said_so_in_the_finding(env, base_model):
    def fails_at_confirm(scenario):
        """The copper clearing helps on the coarse screen mesh and does nothing on the finer confirm mesh."""
        return base_model({**scenario, 'whatif_ops': []} if scenario['quality'] == 'confirm' else scenario)

    env.lab.model = fails_at_confirm
    env.lab.match_gain['confirm_free'] = 0.2
    env.lab.solve_s = {'screen': 60.0, 'confirm': 1000.0}            # two confirm runs leave too little solver time for a refine round (the refine scenarios cover that)
    review = await env.review(SCRIPTS / 'screen_win_fails_at_confirm.json')
    assert review['end_state'] == 'complete', (review['summary']['end'], errors(env, review))
    assert env.lab.count('run_scenario') == 4                       # base, the copper clearing, and two confirm runs: this winner has its own geometry
    confirmation = env.store.list_investigations(review['id'])[0]['outcome']['confirmation']
    assert confirmation['outcome'] == 'not_held' and confirmation['statement'] == 'The screen improvement did not hold at confirm quality.'
    assert confirmation['confirm_gain_db'] == 0.0 and confirmation['free_gain_db'] == 0.2 and confirmation['matching_at_confirm'].get('locked') is None
    result = contents(env, review, 'confirm_result')[1]
    assert any('did not hold at confirm quality' in n for n in result['notices']) and result['confirmation']['held'] is False
    finding = env.store.get_finding_by_code(review['id'], 'F-001')
    assert 'The screen improvement did not hold at confirm quality.' in finding['not_tested']        # recorded by the server, kept when the agent rewrote not_tested
    assert 'Measurement on a real antenna' in finding['not_tested']
    assert 'Result is from screen-quality simulation runs only; no confirm run supports it.' not in finding['not_tested']
    snapshot = env.store.latest_report_revision(review['id'])['snapshot']
    assert snapshot['rf']['meets_target'] is False


async def test_the_screens_parts_that_fail_at_confirm_are_retuned_and_both_are_reported(env):
    env.lab.match_gain.update({'confirm_locked': 0.4, 'confirm_free': 5.0})
    review = await env.review(SCRIPTS / 'matching_win.json')
    confirmation = env.store.list_investigations(review['id'])[0]['outcome']['confirmation']
    assert confirmation['outcome'] == 'held_after_retune' and 'did not hold at confirm quality with the screen' in confirmation['statement']
    assert confirmation['confirm_gain_db'] == 0.4 and confirmation['free_gain_db'] == 5.0
    finding = env.store.get_finding_by_code(review['id'], 'F-001')
    assert confirmation['statement'] in finding['not_tested']


async def test_early_close_is_refused_naming_what_is_missing_then_allowed(env):
    env.lab.match_gain['screen'] = 0.3                                # matching does not help on this board
    review = await env.review(SCRIPTS / 'early_close_refused.json')
    assert review['end_state'] == 'complete', (review['summary']['end'], errors(env, review))
    closes = contents(env, review, 'close_investigation')
    refused, accepted = closes
    assert refused['error'] == 'investigation_incomplete' and 'matching experiment' in refused['message'] and 'only 1 of 2 required suspects tested' in refused['message']
    assert 'S2' in refused['message'] and 'S3' in refused['message'] and refused['missing'] and refused['experiments_left'] == 4
    assert accepted['outcome'] == 'no_improvement'
    assert errors(env, review) == [('close_investigation', 'investigation_incomplete')]
    assert review['budget']['tool_calls']['4'] == 6 and review['budget']['refused_calls'] == 0         # open, base, shorten, the refused close, matching, the close: the refusal cost a phase 4 call
    assert 'confirm' not in ' '.join(e['payload']['content'] for e in env.events(review['chat_id'], 'tool_result') if e['payload']['name'] == 'confirm_result')
    assert env.lab.count('run_scenario') == 2
