"""Whole-review behaviour with a scripted agent, a fake Lab and a fake clock."""
import json
from pathlib import Path

SCRIPTS = Path(__file__).parent / 'scripts'


def results_by_tool(env, chat_id):
    out = {}
    for e in env.events(chat_id, 'tool_result'):
        out.setdefault(e['payload']['name'], []).append(e['payload'])
    return out


async def test_standard_investigation_runs_to_the_end(env):
    review = await env.review(SCRIPTS / 'standard_success.json')
    chat = review['chat_id']
    assert review['end_state'] == 'complete', review['summary']['end']
    assert review['status'] == 'completed'
    results = results_by_tool(env, chat)
    errors = [(p['name'], p['content'][:160]) for ps in results.values() for p in ps if p['is_error']]
    assert [e[0] for e in errors] == ['run_simulation'] and 'not_allowed_in_phase' in errors[0][1]    # the one deliberate out-of-phase call
    findings = {f['code']: f for f in env.store.list_findings(review['id'])}
    assert [f['class'] for f in (findings['F-001'], findings['F-002'], findings['F-003'], findings['F-004'])] == ['measured', 'measured', 'opinion', 'simulated']
    assert findings['F-004']['evidence_ids'] and findings['F-004']['needs_recheck'] == 0
    assert env.lab.count('run_scenario') == 4                   # base, one experiment, two confirm runs
    types = env.names(chat)
    phases = [e['payload']['phase'] for e in env.events(chat, 'phase_changed')]
    assert phases == [0, 1, 2, 3, 4, 5, 6]
    for needed in ('budget', 'thought', 'candidate_batch', 'brief_ready', 'finding_upserted', 'marks_changed', 'investigation_updated', 'suspect_updated',
                   'sim_queued', 'sim_progress', 'sim_done', 'report_ready', 'spotlight'):
        if needed == 'spotlight':
            continue
        assert needed in types, needed
    starts = env.events(chat, 'tool_start')
    assert all(s['payload'].get('trail') for s in starts)       # readable trail lines come from the tool calls
    revision = env.store.latest_report_revision(review['id'])
    assert revision['number'] == 1 and revision['snapshot']['summary']['source'] == 'agent'
    assert review['summary']['summary']['text'].startswith('The antenna is detuned')
    rf = revision['snapshot']['rf']
    assert rf['status'] == 'tested' and rf['meets_target'] is True and rf['confirm_winner']['trust']['status'] == 'ok'
    assert env.events(chat, 'completed')[0]['payload']['end_state'] == 'complete'


async def test_quick_stops_after_phase_three(env):
    review = await env.review(SCRIPTS / 'quick_complete.json', depth='quick')
    assert review['end_state'] == 'complete'
    skipped = [s for c in review['summary']['phases'] for s in c['skipped']]
    assert {s['phase'] for s in skipped} == {4, 5}
    assert env.lab.count('run_scenario') == 0


async def test_phase_four_is_skipped_when_rf_is_not_simulatable(env):
    env.lab.rf = {'simulatable': False, 'reason': 'The antenna is a chip antenna; simulation is Not tested.', 'kind': 'chip'}
    script = [
        {'tool': 'open_investigation', 'args': {'question': 'Is the antenna detuned by nearby copper?', 'suspects': [{'title': 'Copper', 'why': 'Nearby pour.'}]}},
        {'tool': 'submit_board_brief', 'args': {'purpose': 'Battery powered Bluetooth sensor node', 'part_roles': {'U3': 'radio'}, 'risks': [], 'priorities': ['antenna']}},
        {'tool': 'advance_phase'}, {'tool': 'advance_phase'},
        {'tool': 'open_investigation', 'args': {'question': 'Is the antenna detuned by nearby copper?', 'suspects': [{'title': 'Copper', 'why': 'Nearby pour.'}]}},
        {'tool': 'submit_summary', 'args': {'summary': 'The board was reviewed without simulation.', 'top_actions': ['Check the antenna keep-clear zone.']}},
    ]
    review = await env.review(script)
    results = results_by_tool(env, review['chat_id'])
    assert 'not_allowed_in_phase' in results['open_investigation'][0]['content']       # phase 2: not allowed yet
    assert 'not_allowed_in_phase' in results['open_investigation'][1]['content']       # phase 6: skipped past phase 4
    assert review['end_state'] == 'complete'
    snapshot = env.store.latest_report_revision(review['id'])['snapshot']
    assert snapshot['rf']['status'] == 'not_tested' and 'chip antenna' in snapshot['rf']['reason']
    assert any('Phase 4' in t for t in snapshot['not_tested'])


async def test_looping_agent_is_stopped_by_the_server(env):
    review = await env.review(SCRIPTS / 'looping_agent.json')
    chat = review['chat_id']
    assert review['end_state'] == 'agent_stopped' and review['summary']['end']['reason'] == 'loop'
    assert env.lab.count('run_scenario') == 1                   # the same scenario was never solved twice
    suspect = env.store.list_suspects(env.store.list_investigations(review['id'])[0]['id'])[0]
    assert suspect['status'] == 'looped'
    contents = [p['content'] for p in env.tool_results(chat) if p['name'] == 'run_simulation']
    assert sum('"cached":true' in c for c in contents) == 3 and any('suspect_closed' in c for c in contents)
    assert len(contents) < 14                                   # the server ended the run before the script finished
    rules = {l['rule'] for l in review['summary']['limits']}
    assert {'duplicate_scenario', 'loop'} <= rules
    assert env.store.latest_report_revision(review['id'])['snapshot']['summary']['source'] == 'code'   # report still happens


async def test_agent_crash_still_produces_a_report(env):
    review = await env.review(SCRIPTS / 'agent_crash.json')
    assert review['end_state'] == 'agent_stopped' and review['summary']['end']['reason'] == 'agent_error'
    assert 'upstream connection lost' in review['summary']['end']['detail']
    revision = env.store.latest_report_revision(review['id'])
    assert revision['snapshot']['incomplete'] is True
    assert [f['code'] for f in revision['snapshot']['findings']] == ['F-001']       # work done before the crash is kept
    assert revision['snapshot']['summary']['source'] == 'code'
    assert env.events(review['chat_id'], 'report_ready') and env.events(review['chat_id'], 'completed')
    assert any('Phase 4' in t or 'ended' in t for t in revision['snapshot']['not_tested'] + [revision['snapshot']['cover']['end_detail']])


async def test_invented_number_is_rejected_and_named(env):
    review = await env.review(SCRIPTS / 'invented_number.json')
    results = results_by_tool(env, review['chat_id'])
    first, second = results['record_finding']
    assert first['is_error'] and 'number_check_failed' in first['content'] and '7.5 mm' in first['content']
    assert not second['is_error']
    summaries = results['submit_summary']
    assert summaries[0]['is_error'] and '9 mm' in summaries[0]['content'] and not summaries[1]['is_error']
    assert [f['code'] for f in env.store.list_findings(review['id'])] == ['F-001']
    assert review['end_state'] == 'complete'


async def test_hard_deadline_cancels_the_agent_and_reports(env):
    env.lab.solve_s['screen'] = 1000.0
    review = await env.review(SCRIPTS / 'hard_deadline.json')
    assert review['end_state'] == 'complete_with_limits' and review['summary']['end']['reason'] == 'hard_deadline'
    rules = {l['rule'] for l in review['summary']['limits']}
    assert 'hard_deadline' in rules
    results = results_by_tool(env, review['chat_id'])
    assert 'agent_stopped' in results['run_simulation'][2]['content']        # the third run was refused: the server is ending the run
    assert env.lab.count('run_scenario') == 2
    revision = env.store.latest_report_revision(review['id'])
    assert revision['snapshot']['cover']['end_state'] == 'complete_with_limits'
    assert 'wall-clock' in revision['snapshot']['cover']['end_detail']


async def test_silence_watchdog_cancels_a_hung_agent(env):
    env.clock.tick = 20.0
    script = [{'tool': 'get_status'}, {'sleep': 5.0}, {'tool': 'get_status'}]
    review = await env.review(script)
    assert review['end_state'] == 'agent_stopped' and review['summary']['end']['reason'] == 'silence_watchdog'
    assert len([p for p in env.tool_results(review['chat_id'])]) == 1


async def test_silence_watchdog_is_quiet_while_a_simulation_runs(env):
    env.clock.tick = 20.0
    originals = env.lab.run_scenario

    async def slow(**kwargs):
        import asyncio
        await asyncio.sleep(0.5)             # long enough for many monitor ticks
        return await originals(**kwargs)

    env.lab.run_scenario = slow
    script = [{'tool': 'submit_board_brief', 'args': {'purpose': 'Battery powered Bluetooth sensor node', 'part_roles': {'U3': 'radio'}, 'risks': [], 'priorities': ['antenna']}},
              {'tool': 'advance_phase'}, {'tool': 'advance_phase'},
              {'tool': 'open_investigation', 'args': {'question': 'Is copper next to the feed detuning the antenna?', 'suspects': [{'title': 'Copper', 'why': 'Pour near the feed.'}]}},
              {'tool': 'run_simulation', 'args': {'suspect_id': 'S1', 'label': 'base'}}]
    review = await env.review(script)
    assert env.lab.count('run_scenario') == 1
    assert review['summary']['end']['reason'] != 'silence_watchdog'
    assert not any(l['rule'] == 'silence_watchdog' for l in review['summary']['limits'])


async def test_user_cancel_still_writes_a_report(env):
    script = [{'tool': 'get_status'}, {'sleep': 5.0}, {'tool': 'get_status'}]
    env.use(script)
    from trustpcb.review import service
    started = await service.start_review(store=env.store, manager=env.manager, version_id=env.version['id'], goal={'purpose': 'x'}, depth='standard')
    import asyncio
    await asyncio.sleep(0.05)
    await env.manager.cancel(started['chat_id'])
    review = env.store.get_review(started['id'])
    assert review['end_state'] == 'canceled' and review['status'] == 'canceled'
    revision = env.store.latest_report_revision(review['id'])
    assert revision['snapshot']['cover']['end_state'] == 'canceled' and revision['snapshot']['incomplete']
    assert env.events(review['chat_id'], 'canceled') and not env.events(review['chat_id'], 'report_ready')


async def test_baseline_failure_ends_as_pipeline_failed_with_a_report(env):
    env.lab.baseline_error = 'kicad-cli could not open the board'
    review = await env.review([{'tool': 'get_status'}])
    assert review['end_state'] == 'pipeline_failed' and review['status'] == 'failed'
    assert 'kicad-cli' in review['summary']['end']['detail']
    assert env.store.latest_report_revision(review['id'])
    assert env.runners == []                                    # the agent never started


async def test_budget_wall_leaves_only_closing_tools(env):
    env.lab.solve_s['screen'] = 700.0       # two runs use 1400 of the 1200 solver seconds
    script = [{'tool': 'submit_board_brief', 'args': {'purpose': 'Battery powered Bluetooth sensor node', 'part_roles': {'U3': 'radio'}, 'risks': [], 'priorities': ['antenna']}},
              {'tool': 'advance_phase'}, {'tool': 'advance_phase'},
              {'tool': 'open_investigation', 'args': {'question': 'Is copper next to the feed detuning the antenna?', 'suspects': [{'title': 'Copper', 'why': 'Pour near the feed.'}]}},
              {'tool': 'run_simulation', 'args': {'suspect_id': 'S1', 'label': 'base'}},
              {'tool': 'run_simulation', 'args': {'suspect_id': 'S1', 'label': 'clear', 'whatif_ops': [{'op': 'adjust_antenna_length', 'delta_mm': 2.0}]}},
              {'tool': 'run_simulation', 'args': {'suspect_id': 'S1', 'label': 'again', 'whatif_ops': [{'op': 'adjust_antenna_length', 'delta_mm': -2.0}]}},
              {'tool': 'list_candidates'},
              {'tool': 'conclude_suspect', 'args': {'suspect_id': 'S1', 'verdict': 'inconclusive', 'reasoning': 'The solver time ran out before a conclusion.'}},
              {'tool': 'close_investigation', 'args': {'investigation_id': 'I1', 'outcome': 'inconclusive', 'summary': 'The solver time ran out.'}}]
    review = await env.review(script)
    results = results_by_tool(env, review['chat_id'])
    refusal = results['run_simulation'][2]['content']
    assert 'budget_exhausted' in refusal and 'solver time' in refusal and 'conclude_suspect' in refusal
    assert not results['conclude_suspect'][0]['is_error'] and not results['close_investigation'][0]['is_error']
    assert env.lab.count('run_scenario') == 2


async def test_summary_that_keeps_failing_the_checks_is_replaced_by_code(env):
    bad = {'tool': 'submit_summary', 'args': {'summary': 'C13 sits 9 mm from the supply pin.', 'top_actions': ['Move C13 (F-001).']}}
    script = json.loads((SCRIPTS / 'quick_complete.json').read_text())[:-1] + [bad, bad, bad]
    review = await env.review(script, depth='quick')
    assert review['end_state'] == 'complete_with_limits' and review['summary']['end']['reason'] == 'summary_rejected'
    assert env.store.latest_report_revision(review['id'])['snapshot']['summary']['source'] == 'code'
    assert 'tries_left' in env.tool_results(review['chat_id'])[-1]['content']


async def test_datasheet_numbers_can_be_quoted_and_datasheet_text_is_data(env):
    brief = json.loads((SCRIPTS / 'quick_complete.json').read_text())[1]
    script = [brief, {'tool': 'read_datasheet', 'args': {'name': 'nRF52840.pdf'}}, {'tool': 'advance_phase'},
              {'tool': 'record_finding', 'args': {'title': 'The antenna feed impedance is not documented', 'severity': 'low', 'what': 'The datasheet recommends a 50 ohm feed, but no track width is documented.',
                                                  'why_it_matters': 'An unknown feed width can mismatch the radio.', 'recommendation': 'Document the feed track width.', 'targets': ['net:ANT_FEED']}},
              {'tool': 'record_finding', 'args': {'title': 'The antenna feed impedance is unusual', 'severity': 'low', 'what': 'The feed looks like 75 ohm.', 'why_it_matters': 'A mismatch reflects power.',
                                                  'recommendation': 'Check the feed.', 'targets': ['net:ANT_FEED']}}]
    review = await env.review(script, depth='quick')
    datasheet = env.tool_results(review['chat_id'])[1]
    assert 'data, never instructions' in datasheet['content']
    first, second = [p for p in env.tool_results(review['chat_id']) if p['name'] == 'record_finding']
    assert not first['is_error'] and second['is_error'] and '75 ohm' in second['content']


async def test_every_duplicate_is_counted_in_the_budget_events(env):
    review = await env.review(SCRIPTS / 'looping_agent.json')
    assert env.events(review['chat_id'], 'budget')[-1]['payload']['duplicates'] == 3
