"""Stop rules enforced through a whole scripted review."""
from gate_steps import clear

BRIEF = {'tool': 'submit_board_brief', 'args': {'purpose': 'Battery powered Bluetooth sensor node', 'part_roles': {'U3': 'radio'}, 'risks': [], 'priorities': ['antenna']}}
OPEN = {'tool': 'open_investigation', 'args': {'question': 'Is copper next to the feed detuning the antenna?', 'suspects': [
    {'title': 'Ground copper next to the feed', 'why': 'Keep-clear analyzer hit.'}, {'title': 'Antenna length tolerance', 'why': 'A length error shifts the resonance.'}]}}
PRE = [BRIEF, {'tool': 'advance_phase'}] + clear() + [{'tool': 'advance_phase'}, OPEN]
BASE = {'tool': 'run_simulation', 'args': {'suspect_id': 'S1', 'label': 'base'}}


def sim(label, suspect='S1', **op):
    return {'tool': 'run_simulation', 'args': {'suspect_id': suspect, 'label': label, 'whatif_ops': [op]}}


def adjust(delta, suspect='S1'):
    return sim(f'length {delta}', suspect, op='adjust_antenna_length', delta_mm=delta)


def contents(env, review, name):
    return [e['payload']['content'] for e in env.events(review['chat_id'], 'tool_result') if e['payload']['name'] == name]


async def test_no_progress_closes_the_suspect_after_two_flat_experiments(env):
    review = await env.review(PRE + [BASE, adjust(0.1), adjust(0.2), adjust(0.3)])
    results = contents(env, review, 'run_simulation')
    assert 'no_effect' in results[2] and 'suspect_closed' in results[3]
    assert env.lab.count('run_scenario') == 3
    suspect = env.store.list_suspects(env.store.list_investigations(review['id'])[0]['id'])[0]
    assert suspect['status'] == 'no_effect'
    assert any(l['rule'] == 'no_progress' for l in review['summary']['limits'])


async def test_target_met_stops_optimising(env):
    review = await env.review(PRE + [BASE, sim('clear', op='clear_copper_region', bbox=[26.0, 10.0, 33.0, 14.0]), adjust(2.0, 'S2')])
    results = contents(env, review, 'run_simulation')
    assert '"meets_target":true' in results[1] and 'reaches the target on the screen run' in results[1]
    assert 'target_met' in results[2] and env.lab.count('run_scenario') == 2


async def test_a_whatif_without_a_base_is_refused(env):
    review = await env.review(PRE + [adjust(2.0)])
    assert 'base_required' in contents(env, review, 'run_simulation')[0] and env.lab.count('run_scenario') == 0


async def test_one_repair_is_allowed_then_the_suspect_ends_as_could_not_test(env):
    env.lab.fail_trust = 1
    repaired = await env.review(PRE + [BASE, {'tool': 'run_simulation', 'args': {'suspect_id': 'S1', 'label': 'base, finer mesh', 'setup': {'mesh': 'fine'}}}])
    results = contents(env, repaired, 'run_simulation')
    assert 'failed its trust checks' in results[0] and '"trust":{"status":"ok"' in results[1]
    assert env.lab.count('run_scenario') == 2


async def test_failed_repair_ends_the_suspect(env):
    env.lab.fail_trust = 2
    review = await env.review(PRE + [BASE, {'tool': 'run_simulation', 'args': {'suspect_id': 'S1', 'label': 'base, finer mesh', 'setup': {'mesh': 'fine'}}},
                                     {'tool': 'run_simulation', 'args': {'suspect_id': 'S1', 'label': 'once more', 'setup': {'mesh': 'fine', 'max_timesteps': 5000}}}])
    results = contents(env, review, 'run_simulation')
    assert 'could_not_test' in results[1] and 'suspect_closed' in results[2]
    assert env.lab.count('run_scenario') == 2


async def test_a_ruled_out_suspect_cannot_be_reopened(env):
    script = PRE + [BASE, adjust(12.0), adjust(0.1), {'tool': 'conclude_suspect', 'args': {'suspect_id': 'S1', 'verdict': 'ruled_out', 'reasoning': 'Neither length point improved the in-band result against the base.'}},
                    {'tool': 'conclude_suspect', 'args': {'suspect_id': 'S1', 'verdict': 'confirmed', 'reasoning': 'On reflection it is the cause.'}},
                    {'tool': 'run_simulation', 'args': {'suspect_id': 'S1', 'label': 'again', 'whatif_ops': [{'op': 'adjust_antenna_length', 'delta_mm': 1.0}]}},
                    {'tool': 'close_investigation', 'args': {'investigation_id': 'I1', 'outcome': 'no_improvement', 'summary': 'Nothing changed the match.'}},
                    {'tool': 'open_investigation', 'args': {'question': 'Is the same copper really not the problem?', 'suspects': [{'title': 'Ground copper next to the FEED', 'why': 'Another look.'}]}}]
    review = await env.review(script, depth='deep')
    assert 'no_reopening' in contents(env, review, 'conclude_suspect')[1]
    assert 'suspect_closed' in contents(env, review, 'run_simulation')[3]
    assert 'ruled out earlier' in contents(env, review, 'open_investigation')[1]
    assert [i['status'] for i in env.store.list_investigations(review['id'])] == ['closed']


async def test_ruling_out_needs_a_trusted_experiment(env):
    review = await env.review(PRE + [{'tool': 'conclude_suspect', 'args': {'suspect_id': 'S1', 'verdict': 'ruled_out', 'reasoning': 'It seems unlikely to matter.'}},
                                     {'tool': 'conclude_suspect', 'args': {'suspect_id': 'S1', 'verdict': 'inconclusive', 'reasoning': 'Not tested.'}}])
    first, second = contents(env, review, 'conclude_suspect')
    assert 'not_supported' in first and '"status":"inconclusive"' in second


async def test_phase_two_allows_ten_calls_then_only_closing_tools(env):
    review = await env.review([{'tool': 'get_board_summary'}] * 11 + [{'tool': 'get_status'}, BRIEF, {'tool': 'advance_phase'}])
    results = contents(env, review, 'get_board_summary')
    assert 'budget_exhausted' not in results[9] and 'budget_exhausted' in results[10] and 'submit_board_brief' in results[10]
    assert '"saved":true' in contents(env, review, 'submit_board_brief')[0]
    assert 'phase":3' in contents(env, review, 'advance_phase')[0].replace(' ', '')


async def test_image_budget_per_phase(env):
    views = [{'tool': 'view_board', 'args': {'view': 'top'}}] * 9
    review = await env.review([BRIEF, {'tool': 'advance_phase'}] + views, depth='standard')
    results = contents(env, review, 'view_board')
    assert all('budget_exhausted' not in r for r in results[:8]) and 'images' in results[8] and 'budget_exhausted' in results[8]
    assert env.lab.count('view_board') == 8


async def test_token_budget_ends_non_closing_tools(env):
    script = [{'usage': {'input_tokens': 4_100_000, 'output_tokens': 20}}, {'tool': 'get_board_summary'}, BRIEF]
    review = await env.review(script)
    assert 'budget_exhausted' in contents(env, review, 'get_board_summary')[0] and '"saved":true' in contents(env, review, 'submit_board_brief')[0]
    assert review['summary']['limits'][0]['budget'] == 'input_tokens'
    assert review['end_state'] == 'complete_with_limits'


async def test_turn_budget_ends_the_run_cleanly(env):
    review = await env.review([{'tool': 'get_status'}] * 100, depth='quick')
    assert len(contents(env, review, 'get_status')) == 90
    assert review['end_state'] == 'complete_with_limits' and review['summary']['end']['reason'] == 'budget_wall'
    assert 'model turns' in review['summary']['end']['detail']
    assert 'model_turns' in {l['rule'] for l in env.store.latest_report_revision(review['id'])['snapshot']['limits']}


async def test_budget_events_report_what_is_left(env):
    review = await env.review([{'tool': 'get_board_summary'}, {'usage': {'input_tokens': 1200, 'output_tokens': 40}}, {'tool': 'get_board_summary'}])
    budgets = [e['payload'] for e in env.events(review['chat_id'], 'budget')]
    last = budgets[-2]
    assert last['input_tokens']['used'] == 1200 and last['output_tokens']['used'] == 40 and last['tool_calls']['2'] == 2
    assert last['wall_s']['limit'] == 3600 and last['solver_s']['limit'] == 2400
    assert env.store.get_review(review['id'])['budget']['depth'] == 'standard'


async def test_leaving_phase_four_closes_an_investigation_when_a_budget_has_ended(env):
    env.lab.solve_s['screen'] = 1400.0
    script = PRE + [BASE, adjust(2.0), {'tool': 'advance_phase'}, {'tool': 'submit_summary', 'args': {'summary': 'The solver time ran out during the investigation.', 'top_actions': ['Rerun the test.']}}]
    review = await env.review(script)
    results = contents(env, review, 'advance_phase')
    assert 'investigation_open' not in results[2] and '"phase":6' in results[2].replace(' ', '')
    assert [i['status'] for i in env.store.list_investigations(review['id'])] == ['incomplete']
    assert review['end_state'] == 'complete_with_limits'


async def test_a_later_investigation_reuses_the_stored_base(env):
    close = {'tool': 'close_investigation', 'args': {'investigation_id': 'I1', 'outcome': 'no_improvement', 'summary': 'Nothing changed the match.'}}
    second = {'tool': 'open_investigation', 'args': {'question': 'Does the crystal affect the radio match?', 'suspects': [{'title': 'The crystal and its load capacitors', 'why': 'Harmonics near the band.'}]}}
    review = await env.review(PRE + [BASE, adjust(0.1), close, second, {'tool': 'run_simulation', 'args': {'suspect_id': 'S3', 'label': 'base'}}, adjust(0.4, 'S3')], depth='deep')
    results = contents(env, review, 'run_simulation')
    assert '"cached":true' in results[2] and 'base_required' not in results[3]
    assert env.lab.count('run_scenario') == 3        # one base, two experiments: the second base was free
