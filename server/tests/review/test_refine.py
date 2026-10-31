"""The refine round: a bounded second attempt after a confirm that missed the target (spec sections 6 to 8)."""
import json
from pathlib import Path

import pytest

from trustpcb.review import budgets

SCRIPTS = Path(__file__).parent / 'scripts'


def contents(env, review, name):
    return [json.loads(e['payload']['content']) for e in env.events(review['chat_id'], 'tool_result') if e['payload']['name'] == name]


def errors(env, review):
    return [(e['payload']['name'], json.loads(e['payload']['content']).get('error')) for e in env.events(review['chat_id'], 'tool_result') if e['payload']['is_error']]


def antenna(base_model, *, shorten_gain=2.0):
    """The copper clearing helps on the screen mesh only; shortening the antenna helps at both qualities (it gives the matching something to work on)."""
    def model(scenario):
        ops = scenario.get('whatif_ops') or []
        copper = [o for o in ops if o['op'] == 'clear_copper_region' and scenario['quality'] == 'screen']
        out = base_model({**scenario, 'whatif_ops': copper})
        if any(o['op'] == 'adjust_antenna_length' for o in ops):
            out = {**out, 'band_worst_s11_db': out['band_worst_s11_db'] - shorten_gain, 'band_mean_s11_db': out['band_mean_s11_db'] - shorten_gain}
        return out
    return model


@pytest.fixture
def refine_env(env, base_model):
    env.lab.model = antenna(base_model)
    env.lab.match_gain['confirm_free'] = 0.2               # the first attempt: free re-tuning barely helps at confirm quality
    return env


async def test_refine_success_states_the_combined_fix(refine_env):
    env = refine_env
    review = await env.review(SCRIPTS / 'refine_success.json')
    assert review['end_state'] == 'complete', (review['summary']['end'], errors(env, review))
    assert errors(env, review) == []
    investigation = env.store.list_investigations(review['id'])[0]
    first = investigation['outcome']['confirmation']
    assert first['outcome'] == 'not_held' and first['meets_target'] is False                      # the first attempt is kept as it was
    (round_,) = investigation['outcome']['refine_rounds']
    assert round_['round'] == 1 and round_['result'] == 'target_met' and round_['candidate'] == 'R7'
    assert round_['candidate_change']['ops'] == [{'op': 'adjust_antenna_length', 'delta_mm': -7.0}] and round_['candidate_change']['parts'][0]['ref'] == 'L1'
    assert round_['confirmation']['outcome'] == 'held' and round_['confirmation']['meets_target'] is True and round_['confirmation']['confirm_gain_db'] == 10.0
    assert round_['screens'][0]['ref'] == 'R6' and 'E-003' in round_['screens'][0]['reason']
    # solver: base, copper, confirm base, confirm copper, one refine screen, one refine confirm. The matching ran for free.
    assert env.lab.count('run_scenario') == 6
    assert review['budget']['confirm_runs'] == {'used': 3, 'limit': 3}                            # the round adds one confirm run to the two Standard allows
    finding = env.store.get_finding_by_code(review['id'], 'F-001')
    assert finding['class'] == 'simulated' and 'shorten the antenna by 7 mm' in finding['recommendation'].replace('Apply in your own tool, then re-measure: ', '') or '7 mm' in finding['recommendation']
    assert any('did not hold' in t for t in finding['not_tested'])                                 # the first attempt stays on the finding
    assert not any('Refine round' in t for t in finding['not_tested'])                             # the second held
    assert review['summary']['refine']['rounds'][0]['result'] == 'target_met' and review['summary']['end']['reason'] == 'completed'
    suspects = env.store.list_suspects(investigation['id'])
    assert suspects[-1]['status'] == 'confirmed' and 'Refine round 1' in suspects[-1]['title']


async def test_the_round_keeps_phases_forward_only_and_says_so_in_events(refine_env):
    env = refine_env
    review = await env.review(SCRIPTS / 'refine_success.json')
    changes = [e['payload'] for e in env.events(review['chat_id'], 'phase_changed')]
    assert [c['phase'] for c in changes] == sorted(c['phase'] for c in changes)                  # never backwards
    (refine,) = [c for c in changes if c.get('substep') == 'refine']
    assert refine['phase'] == 5 and refine['previous'] == 5 and refine['round'] == 1
    notes = [e['payload'] for e in env.events(review['chat_id'], 'investigation_updated') if e['payload'].get('note')]
    assert any('Refine round 1 opened' in n['note'] for n in notes) and any('Refine screen 1 of 3' in n['note'] for n in notes)
    assert any(n['note'].startswith('Refine round 1 ended') for n in notes) and notes[-1]['refining'] is False


async def test_refine_failure_states_both_attempts_honestly(refine_env):
    env = refine_env
    env.lab.match_gain['confirm_locked'] = 0.4                    # at confirm quality the refine candidate's parts do not carry over
    review = await env.review(SCRIPTS / 'refine_failure.json')
    assert review['end_state'] == 'complete', (review['summary']['end'], errors(env, review))
    investigation = env.store.list_investigations(review['id'])[0]
    (round_,) = investigation['outcome']['refine_rounds']
    assert round_['result'] == 'target_not_met' and round_['confirmation']['outcome'] == 'held_smaller' and round_['confirmation']['meets_target'] is False
    finding = env.store.get_finding_by_code(review['id'], 'F-001')
    notes = ' '.join(finding['not_tested'])
    assert 'The screen improvement did not hold at confirm quality.' in notes
    assert 'Refine round: The improvement held at confirm quality but the screen overstated it' in notes and 'target is still not met at confirm quality' in notes
    assert 'neither' in finding['title'].lower() or 'Neither' in finding['title']
    suspects = env.store.list_suspects(investigation['id'])
    assert suspects[-1]['status'] == 'inconclusive'
    assert review['summary']['summary']['source'] == 'agent'


async def test_a_budget_wall_ends_the_round_and_the_report_still_runs(refine_env):
    env = refine_env
    original = env.lab.run_scenario
    runs = []

    async def greedy(**kwargs):
        runs.append(kwargs['scenario']['quality'])
        if len(runs) == 5:                                         # the refine screen eats the rest of the solver budget
            env.lab.solve_s = {**env.lab.solve_s, 'screen': 1900.0}
        return await original(**kwargs)

    env.lab.run_scenario = greedy
    review = await env.review(SCRIPTS / 'refine_budget_wall.json')
    assert review['end_state'] == 'complete_with_limits', review['summary']['end']
    codes = errors(env, review)
    assert ('tune_matching', 'budget_exhausted') in codes and ('confirm_result', 'budget_exhausted') in codes
    assert review['phase'] == '6' and any(l['rule'] == 'budget_wall' and l['budget'] == 'solver_time' for l in review['summary']['limits'])
    (round_,) = env.store.list_investigations(review['id'])[0]['outcome']['refine_rounds']
    assert round_['result'] == 'candidate_not_confirmed' and round_['confirmation'] is None
    assert env.store.latest_report_revision(review['id'])                                         # phase 6 always runs
    assert env.lab.count('run_scenario') == 5


async def test_the_round_does_not_open_when_the_solver_budget_cannot_fit_it(refine_env):
    env = refine_env
    env.lab.solve_s = {'screen': 60.0, 'confirm': 1000.0}
    review = await env.review(SCRIPTS / 'screen_win_fails_at_confirm.json')
    assert review['end_state'] == 'complete', review['summary']['end']
    limits = [l for l in review['summary']['limits'] if l['rule'] == 'refine_skipped']
    assert limits and 'solver time' in limits[0]['detail']
    assert review['summary']['refine']['rounds'] == [] and review['summary']['refine']['skipped'][0]['investigation'] == 'I1'
    assert env.lab.count('run_scenario') == 4


async def test_refine_decline_on_deep_needs_a_reason_and_standard_cannot_decline(refine_env):
    env = refine_env
    review = await env.review(SCRIPTS / 'refine_decline.json', depth='deep')
    assert review['end_state'] == 'complete', (review['summary']['end'], errors(env, review))
    assert review['summary']['refine']['declined'][0]['investigation'] == 'I1' and review['summary']['refine']['rounds'] == []
    assert any(l['rule'] == 'refine_declined' for l in review['summary']['limits']) and env.lab.count('run_scenario') == 4
    # Standard: the same decline is refused while the solver budget allows, and the round then opens
    steps = json.loads((SCRIPTS / 'refine_decline.json').read_text())
    step = next(i for i, x in enumerate(steps) if x.get('tool') == 'advance_phase' and (x.get('args') or {}).get('decline_refine'))
    env.lab.calls.clear()
    review = await env.review(steps[:step + 1] + [{'tool': 'advance_phase'}], depth='standard')
    refused, opened = contents(env, review, 'advance_phase')[-2:]
    assert refused['error'] == 'refine_required' and opened['refine_round_started'] is True and opened['substep'] == 'refine'


async def test_deep_may_run_a_second_round_but_not_a_third(refine_env):
    env = refine_env
    env.lab.match_gain['confirm_locked'] = 0.4
    steps = json.loads((SCRIPTS / 'refine_failure.json').read_text())
    cut = next(i for i, x in enumerate(steps) if x.get('tool') == 'record_finding' and (x['args'].get('update') == 'F-001'))
    second = [{'tool': 'advance_phase'},
              {'tool': 'run_simulation', 'args': {'suspect_id': 'S5', 'label': 'shorten the antenna by 9 mm', 'whatif_ops': [{'op': 'adjust_antenna_length', 'delta_mm': -9.0}],
                                                   'reason': 'The refine confirm E-012 shows the candidate parts do not carry over at confirm quality, and the band centre stays inductive, so shorten further.'}},
              {'tool': 'tune_matching', 'args': {'ref': 'R11'}},
              {'tool': 'confirm_result', 'args': {'investigation_id': 'I1', 'which': 'refine'}},
              {'tool': 'advance_phase'},
              {'tool': 'submit_summary', 'args': {'summary': 'Neither attempt met the target at confirm quality.', 'top_actions': ['Measure the antenna on a real board (F-001).']}}]
    review = await env.review(steps[:cut] + second, depth='deep')
    assert review['end_state'] == 'complete', (review['summary']['end'], errors(env, review))
    rounds = review['summary']['refine']['rounds']
    assert [r['round'] for r in rounds] == [1, 2] and all(r['result'] == 'target_not_met' for r in rounds)
    assert review['phase'] == '6'                       # the final advance_phase found no round left


async def test_screens_need_a_reason_from_confirm_evidence_and_a_change(refine_env):
    env = refine_env
    steps = json.loads((SCRIPTS / 'refine_success.json').read_text())
    start = next(i for i, x in enumerate(steps) if x.get('tool') == 'run_simulation' and x['args'].get('suspect_id') == 'S4')
    ops = [{'op': 'adjust_antenna_length', 'delta_mm': -7.0}]
    probes = [{'tool': 'run_simulation', 'args': {'suspect_id': 'S4', 'whatif_ops': ops}},                                                              # no reason at all
              {'tool': 'run_simulation', 'args': {'suspect_id': 'S4', 'whatif_ops': ops, 'reason': 'This is a long enough reason but it names no evidence at all.'}},
              {'tool': 'run_simulation', 'args': {'suspect_id': 'S4', 'reason': 'The confirm base E-003 stays inductive at the band centre, so try the base again.'}},
              {'tool': 'run_simulation', 'args': {'suspect_id': 'S4', 'whatif_ops': ops, 'reason': 'The confirm base E-003 shows an S11 of -17.3 dB in the band, so shorten it.'}},
              {'tool': 'run_simulation', 'args': {'suspect_id': 'S4', 'whatif_ops': ops, 'reason': 'The confirm base E-001 stays inductive at the band centre, so shorten it.'}}]    # E-001 is a screen run
    review = await env.review(steps[:start] + probes + [{'tool': 'advance_phase'}])
    out = contents(env, review, 'run_simulation')[-5:]
    assert [r.get('error') for r in out] == ['reason_too_short', 'reason_needs_confirm_evidence', 'refine_needs_change', 'number_check_failed', 'reason_needs_confirm_evidence']
    assert env.lab.count('run_scenario') == 4                                                      # none of the probes reached the solver


async def test_a_simulation_is_not_allowed_in_phase_5_before_a_round_opens(refine_env):
    env = refine_env
    steps = json.loads((SCRIPTS / 'refine_success.json').read_text())
    start = next(i for i, x in enumerate(steps) if x.get('tool') == 'run_simulation' and x['args'].get('suspect_id') == 'S4')
    probe = {'tool': 'run_simulation', 'args': {'suspect_id': 'S1', 'whatif_ops': [{'op': 'adjust_antenna_length', 'delta_mm': -2.0}], 'reason': 'The confirm base E-003 stays inductive.'}}
    review = await env.review(steps[:start - 1] + [probe])                                          # still in phase 5, no round yet
    assert contents(env, review, 'run_simulation')[-1]['error'] == 'not_allowed_in_phase'
    refine_confirm = {'tool': 'confirm_result', 'args': {'investigation_id': 'I1', 'which': 'refine'}}
    review = await env.review(steps[:start - 1] + [refine_confirm])
    assert contents(env, review, 'confirm_result')[-1]['error'] == 'refine_not_active'


async def test_standard_cannot_leave_the_round_without_trying_a_combined_candidate(refine_env):
    env = refine_env
    steps = json.loads((SCRIPTS / 'refine_success.json').read_text())
    start = next(i for i, x in enumerate(steps) if x.get('tool') == 'run_simulation' and x['args'].get('suspect_id') == 'S4')
    adv = {'tool': 'advance_phase'}
    review = await env.review(steps[:start] + [adv, steps[start], adv, steps[start + 1], adv, steps[start + 2], adv])
    refusals = contents(env, review, 'advance_phase')[-4:]
    assert refusals[0]['error'] == 'refine_incomplete' and 'run at least one screen what-if' in refusals[0]['missing'][0]
    assert refusals[1]['error'] == 'refine_incomplete' and 'tune_matching' in refusals[1]['missing'][0]
    assert refusals[2]['error'] == 'refine_incomplete' and 'confirm_result' in refusals[2]['missing'][0]
    assert refusals[3]['phase'] == 6                                                           # all three done: the round may end


def test_budget_limits_per_depth():
    assert [budgets.config_for(d).refine_rounds for d in ('quick', 'standard', 'deep')] == [0, 1, 2]
    assert budgets.followup_config('test').refine_rounds == 0
    assert budgets.REFINE_SCREENS == 3


async def test_the_report_shows_both_attempts(refine_env):
    from trustpcb.review import report
    env = refine_env
    review = await env.review(SCRIPTS / 'refine_success.json')
    revision = env.store.latest_report_revision(review['id'])
    html = (await report.render_revision(store=env.store, review=review, revision=revision, fmt='html')).decode()
    assert 'Refine round 1' in html and 'a second attempt after the confirm missed the target' in html and 'Refine round 1: screen against confirm' in html
    assert html.count('Screen against confirm') >= 1 and 'L1 2.2 nH to 0.75 pF' in html
    assert revision['snapshot']['investigations'][0]['refine_rounds'][0]['result'] == 'target_met'
