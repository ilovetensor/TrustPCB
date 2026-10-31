"""Follow-up conversations: levels, approval, revisions, assumptions and sessions (spec section 17)."""
import asyncio
import json
from pathlib import Path

import pytest

from trustpcb.review import followup, report
from trustpcb.runners.base import RunRequest
from trustpcb.runners.scripted import ScriptedRunner
from trustpcb.sessions import SessionConflict

SCRIPTS = Path(__file__).parent / 'scripts'


async def finished_review(env, script='quick_complete.json', depth='quick'):
    return await env.review(SCRIPTS / script, depth=depth)


def new_thread(env, review):
    return followup.start_thread(store=env.store, manager=env.manager, review_id=review['id'], body={})


async def say(env, thread, text, script, selected=None, request_id=None, wait=True):
    env.use(script)
    out = await followup.send_message(store=env.store, manager=env.manager, thread_id=thread['id'], text=text, selected=selected or [], request_id=request_id or f'm{len(env.store.events(thread["chat_id"]))}')
    if wait:
        await env.wait()
    return out


def results(env, thread, name=None):
    rows = [e['payload'] for e in env.events(thread['chat_id'], 'tool_result')]
    return [r for r in rows if name is None or r['name'] == name]


async def respond_when_asked(env, thread, allow):
    for _ in range(400):
        pending = [e for e in env.events(thread['chat_id'], 'test') if e['payload']['request_id'] in env.manager.requests]
        if pending:
            return await env.manager.respond(pending[-1]['payload']['request_id'], allow=allow)
        await asyncio.sleep(0.01)
    raise AssertionError('no test approval was requested')


async def test_followup_finds_a_new_issue_and_creates_revision_two(env):
    review = await finished_review(env)
    first = env.store.get_report_revision(review['id'], 1)
    snapshot_before = json.dumps(first['snapshot'], sort_keys=True)
    thread = new_thread(env, review)
    assert thread['level'] == 'answer'
    await say(env, thread, 'Is C14 too close to U3? It looks far from the supply pin.', SCRIPTS / 'followup_new_issue.json', selected=[{'type': 'part', 'id': 'ref:C14'}])
    findings = {f['code']: f for f in env.store.list_findings(review['id'])}
    new = findings['F-002']
    assert new['source'] == 'followup' and new['thread_id'] == thread['id'] and new['class'] == 'measured'
    assert new['marks'][0]['type'] == 'pin' and new['marks'][0]['resolved']
    revision = env.store.latest_report_revision(review['id'])
    assert revision['number'] == 2 and 'follow-up' in revision['cause']
    changes = revision['snapshot']['changes_since_last']
    assert [f['code'] for f in changes['new_findings']] == ['F-002'] and changes['new_findings'][0]['found_in_followup']
    assert json.dumps(env.store.get_report_revision(review['id'], 1)['snapshot'], sort_keys=True) == snapshot_before        # revision 1 never changes
    types = env.names(thread['chat_id'])
    for needed in ('followup_started', 'finding_changed', 'marks_changed', 'report_revised', 'text_delta', 'completed'):
        assert needed in types, needed
    started = env.events(thread['chat_id'], 'followup_started')[0]['payload']
    assert started['selected'] == [{'type': 'part', 'id': 'ref:C14'}]
    assert 'Selected by the user' in env.runners[-1].requests[0].user_text and 'ref:C14' in env.runners[-1].requests[0].user_text
    html = (await report.render_revision(store=env.store, review=review, revision=revision, fmt='html')).decode()
    assert 'Changes since revision r1' in html and 'Found in follow-up' in html and 'C14' in html
    assert 'Changes since revision' not in (await report.render_revision(store=env.store, review=review, revision=env.store.get_report_revision(review['id'], 1), fmt='html')).decode()


async def test_answering_from_saved_data_changes_nothing(env):
    review = await finished_review(env)
    thread = new_thread(env, review)
    await say(env, thread, 'Why is F-001 a problem?', [{'tool': 'get_finding', 'args': {'code': 'F-001'}}, {'tool': 'get_evidence', 'args': {'code': 'E-001'}}, {'text': 'Because the loop is long.'}])
    assert env.store.latest_report_revision(review['id'])['number'] == 1
    assert not env.events(thread['chat_id'], 'report_revised')
    assert '"code":"F-001"' in results(env, thread, 'get_finding')[0]['content']


async def test_answer_level_allows_six_calls_and_check_level_fifteen(env):
    review = await finished_review(env)
    thread = new_thread(env, review)
    await say(env, thread, 'Tell me everything.', [{'tool': 'get_finding', 'args': {'code': 'F-001'}}] * 7)
    rows = results(env, thread, 'get_finding')
    assert not any(r['is_error'] for r in rows[:6]) and 'budget_exhausted' in rows[6]['content'] and 'answer level allows 6' in rows[6]['content']
    thread2 = new_thread(env, review)
    script = [{'tool': 'get_finding', 'args': {'code': 'F-001'}}] * 5 + [{'tool': 'measure', 'args': {'kind': 'distance', 'a': 'ref:C13', 'b': 'ref:U3'}}] + \
             [{'tool': 'get_finding', 'args': {'code': 'F-001'}}] * 10
    await say(env, thread2, 'Check it all.', script, request_id='second')
    assert env.events(thread2['chat_id'], 'followup_started')[-1]['payload']['level'] == 'check'
    calls = results(env, thread2)
    assert len(calls) == 16 and not any(c['is_error'] for c in calls[:15]) and 'budget_exhausted' in calls[15]['content'] and 'check level allows 15' in calls[15]['content']
    assert env.store.get_thread(thread2['id'])['level'] == 'check'


async def test_simulation_tools_are_closed_until_a_test_is_approved(env):
    review = await finished_review(env)
    thread = new_thread(env, review)
    await say(env, thread, 'Simulate it.', [{'tool': 'run_simulation', 'args': {'suspect_id': 'S1'}}, {'tool': 'open_investigation', 'args': {'question': 'Does copper hurt the antenna?', 'suspects': [{'title': 'Copper', 'why': 'Near.'}]}}])
    assert all('not_allowed_in_phase' in r['content'] for r in results(env, thread)) and env.lab.count('run_scenario') == 0


async def test_declined_test_runs_nothing(env):
    review = await finished_review(env)
    thread = new_thread(env, review)
    script = [{'tool': 'propose_test', 'args': {'title': 'Clear the copper near the antenna', 'why': 'To see if the pour detunes it.', 'estimated_seconds': 120}},
              {'tool': 'run_simulation', 'args': {'suspect_id': 'S1'}}, {'text': 'Understood, no test.'}]
    task = asyncio.ensure_future(say(env, thread, 'Does this copper hurt the antenna?', script))
    await respond_when_asked(env, thread, allow=False)
    await task
    proposed = env.events(thread['chat_id'], 'test_proposed')[0]['payload']
    assert proposed['title'] == 'Clear the copper near the antenna' and proposed['estimated_seconds'] == 120
    assert env.events(thread['chat_id'], 'test_decided')[0]['payload']['allow'] is False
    assert 'test_declined' in results(env, thread, 'propose_test')[0]['content']
    assert env.lab.count('run_scenario') == 0 and env.store.latest_report_revision(review['id'])['number'] == 1


async def test_approved_test_runs_within_its_limits_and_creates_a_revision(env):
    review = await finished_review(env)
    thread = new_thread(env, review)
    clear = {'op': 'clear_copper_region', 'bbox': [26.0, 10.0, 33.0, 14.0]}
    script = [
        {'tool': 'propose_test', 'args': {'title': 'Clear the copper near the antenna', 'why': 'To see if the pour detunes it.', 'estimated_seconds': 180}},
        {'tool': 'open_investigation', 'args': {'question': 'Does the copper near the feed hurt the antenna?', 'suspects': [{'title': 'Ground copper next to the feed', 'why': 'Keep-clear hit.'}]}},
        {'tool': 'open_investigation', 'args': {'question': 'A second question while the first is open?', 'suspects': [{'title': 'Something else', 'why': 'No.'}]}},
        {'tool': 'run_simulation', 'args': {'suspect_id': 'S1', 'label': 'base'}},
        {'tool': 'run_simulation', 'args': {'suspect_id': 'S1', 'label': 'clear', 'whatif_ops': [clear]}},
        {'tool': 'propose_finding_change', 'args': {'action': 'new', 'title': 'Ground copper next to the feed detunes the antenna', 'severity': 'high',
                                                    'what': 'In the simulation, clearing the copper next to the feed improves the return loss by 9.3 dB compared with the base run.',
                                                    'why_it_matters': 'A detuned antenna wastes transmit power.', 'recommendation': 'Review the ground copper next to the antenna feed in your layout tool.',
                                                    'evidence_ids': ['E-003'], 'targets': ['net:ANT_CHIP'], 'rule': 'antenna_detune'}},
        {'text': 'Yes: in the simulation the copper detunes the antenna.'}]
    task = asyncio.ensure_future(say(env, thread, 'Does the copper near the feed hurt the antenna?', script))
    await respond_when_asked(env, thread, allow=True)
    await task
    calls = {i: r for i, r in enumerate(results(env, thread))}
    assert 'budget_exhausted' in calls[2]['content'] and 'investigation' in calls[2]['content']
    assert env.lab.count('run_scenario') == 2
    new = env.store.get_finding_by_code(review['id'], 'F-002')
    assert new['class'] == 'simulated' and new['source'] == 'followup' and any('screen-quality' in t for t in new['not_tested'])
    revision = env.store.latest_report_revision(review['id'])
    assert revision['number'] == 2 and revision['snapshot']['changes_since_last']['new_findings'][0]['code'] == 'F-002'
    assert env.events(thread['chat_id'], 'test_decided')[0]['payload']['allow'] is True
    assert env.store.get_thread(thread['id'])['level'] == 'test'


async def test_test_level_stops_at_three_experiments(env):
    review = await finished_review(env)
    thread = new_thread(env, review)
    steps = [{'tool': 'propose_test', 'args': {'title': 'Sensitivity', 'why': 'See how much the stack matters.', 'estimated_seconds': 60}},
             {'tool': 'open_investigation', 'args': {'question': 'How sensitive is the antenna to the stackup?', 'suspects': [{'title': 'Permittivity', 'why': 'Unknown material.'}]}},
             {'tool': 'run_simulation', 'args': {'suspect_id': 'S1', 'label': 'base'}}]
    steps += [{'tool': 'run_simulation', 'args': {'suspect_id': 'S1', 'label': f'e{eps}', 'whatif_ops': [{'op': 'set_material', 'epsilon_r': eps}]}} for eps in (3.9, 4.2, 4.7, 4.1)]
    task = asyncio.ensure_future(say(env, thread, 'How sensitive is it to the material?', steps))
    await respond_when_asked(env, thread, allow=True)
    await task
    runs = results(env, thread, 'run_simulation')
    assert [('budget_exhausted' in r['content']) for r in runs] == [False, False, False, False, True]      # base + three experiments, the fourth is refused
    assert env.lab.count('run_scenario') == 4


async def test_dismissal_keeps_the_finding_with_the_users_reason(env):
    review = await finished_review(env)
    thread = new_thread(env, review)
    await say(env, thread, 'C13 is shared with C14 on purpose; this is not a problem.', [
        {'tool': 'propose_finding_change', 'args': {'action': 'dismiss', 'finding_code': 'F-001', 'reason': 'C13 is shared with C14 on purpose, as the vendor reference design does.'}}, {'text': 'Agreed.'}])
    finding = env.store.get_finding_by_code(review['id'], 'F-001')
    assert finding['status'] == 'dismissed' and 'vendor reference design' in finding['dismissed_reason']
    history = env.store.finding_history(finding['id'])
    assert history[-1]['source'] == 'user' and history[-1]['before']['status'] == 'open'
    snapshot = env.store.latest_report_revision(review['id'])['snapshot']
    assert snapshot['changes_since_last']['dismissed'][0]['code'] == 'F-001'
    assert [f['code'] for f in snapshot['findings']] == ['F-001']                  # dismissed findings stay listed
    assert env.store.severity_counts(review['id'])['medium'] == 0                  # but no longer count as open


async def test_a_wrong_finding_is_superseded_not_deleted(env):
    review = await finished_review(env)
    thread = new_thread(env, review)
    await say(env, thread, 'F-001 is wrong: C13 is on the other side of U3.', [
        {'tool': 'measure', 'args': {'kind': 'distance', 'a': 'ref:C14', 'b': 'ref:U3'}},
        {'tool': 'propose_finding_change', 'args': {'action': 'supersede', 'finding_code': 'F-001', 'reason': 'The user showed the right capacitor.', 'title': 'Decoupling capacitor C14 is far from U3',
                                                    'severity': 'low', 'what': 'C14 is 5.54 mm from U3.', 'why_it_matters': 'A long loop weakens decoupling.',
                                                    'recommendation': 'Move C14 closer to the U3 supply pin.', 'evidence_ids': ['E-002'], 'targets': ['ref:C14', 'ref:U3'], 'rule': 'decoupling'}}])
    old, new = env.store.get_finding_by_code(review['id'], 'F-001'), env.store.get_finding_by_code(review['id'], 'F-002')
    assert old['status'] == 'superseded' and old['superseded_by'] == new['id'] and new['status'] == 'open'
    changes = env.store.latest_report_revision(review['id'])['snapshot']['changes_since_last']
    assert changes['superseded'][0]['code'] == 'F-001' and changes['new_findings'][0]['code'] == 'F-002'


async def test_a_rejected_change_creates_no_revision(env):
    review = await finished_review(env)
    thread = new_thread(env, review)
    await say(env, thread, 'Add a finding about C14.', [
        {'tool': 'propose_finding_change', 'args': {'action': 'new', 'title': 'C14 is far', 'severity': 'low', 'what': 'C14 is 9 mm from U3.', 'why_it_matters': 'Weaker decoupling.',
                                                    'recommendation': 'Move C14 closer.', 'targets': ['ref:C14']}}])
    assert 'number_check_failed' in results(env, thread)[0]['content']
    assert env.store.latest_report_revision(review['id'])['number'] == 1 and len(env.store.list_findings(review['id'])) == 1


async def test_chat_only_opinion_needs_the_add_to_report_request(env):
    review = await finished_review(env)
    thread = new_thread(env, review)
    args = {'action': 'new', 'title': 'The crystal may be close to the edge', 'severity': 'low', 'what': 'The crystal looks close to the board edge.', 'why_it_matters': 'Edges are mechanically stressed.',
            'recommendation': 'Consider moving the crystal inward.', 'targets': ['ref:Y1'], 'opinion': True}
    await say(env, thread, 'Is the crystal placement fine?', [{'tool': 'propose_finding_change', 'args': args}])
    assert 'opinion_stays_in_chat' in results(env, thread)[0]['content'] and env.store.latest_report_revision(review['id'])['number'] == 1
    thread2 = new_thread(env, review)
    await say(env, thread2, 'Please add it to the report as your opinion.', [{'tool': 'propose_finding_change', 'args': args}], request_id='again')
    finding = env.store.get_finding_by_code(review['id'], 'F-002')
    assert finding['class'] == 'opinion' and env.store.latest_report_revision(review['id'])['number'] == 2


async def test_user_facts_become_assumptions_and_mark_simulated_findings(env):
    review = await finished_review(env, 'standard_success.json', 'standard')
    thread = new_thread(env, review)
    await say(env, thread, 'The material is Rogers 4350.', [
        {'tool': 'record_assumption', 'args': {'name': 'dielectric material', 'value': 'Rogers 4350', 'note': 'Stated by the engineer'}}, {'text': 'Noted; the simulated finding needs a rerun.'}])
    assumption = next(a for a in env.store.list_assumptions(review['id']) if a['name'] == 'dielectric_material')
    assert assumption['source'] == 'user_provided' and assumption['thread_id'] == thread['id']
    findings = {f['code']: f for f in env.store.list_findings(review['id'])}
    assert findings['F-004']['status'] == 'needs_recheck' and findings['F-004']['needs_recheck'] == 1
    assert findings['F-001']['status'] == 'open'                                    # a measured finding about decoupling does not depend on the material
    event = env.events(thread['chat_id'], 'assumption_changed')[0]['payload']
    assert event['affected'] == [{'code': 'F-004', 'status': 'needs_recheck'}]
    snapshot = env.store.latest_report_revision(review['id'])['snapshot']
    assert snapshot['changes_since_last']['needs_recheck'][0]['code'] == 'F-004' and snapshot['changes_since_last']['assumptions'][0]['source'] == 'user_provided'
    assert 'Needs recheck' in (await report.render_revision(store=env.store, review=review, revision=env.store.latest_report_revision(review['id']), fmt='html')).decode()


async def test_measured_findings_are_rechecked_by_code_when_an_assumption_changes(env):
    review = await finished_review(env)
    thread = new_thread(env, review)
    # an impedance candidate depends on the stackup; the fake analyzer reproduces it, so the flag clears
    env.lab.candidates.append({'id': 'candidate:impedance:1', 'rule': 'impedance', 'title': 'Track impedance is off target', 'source': 'analyzer', 'targets': ['net:ANT_FEED'],
                               'measurements': {'impedance': {'value': 61.0, 'unit': 'ohm'}}, 'marks': [], 'severity_hint': 'medium'})
    await say(env, thread, 'Record the impedance one.', [{'tool': 'run_analyzer', 'args': {'rule': 'impedance'}}, {'tool': 'propose_finding_change', 'args': {
        'action': 'new', 'title': 'Feed track impedance is off target', 'severity': 'medium', 'what': 'The feed track is about 61 ohm.', 'why_it_matters': 'A mismatch reflects power.',
        'recommendation': 'Check the feed track width against the stackup.', 'candidate_ids': ['candidate:impedance:1']}}])
    thread2 = new_thread(env, review)
    await say(env, thread2, 'The board is 1.0 mm thick, not 1.6.', [{'tool': 'record_assumption', 'args': {'name': 'board thickness', 'value': '1.0 mm'}}], request_id='fact')
    finding = env.store.get_finding_by_code(review['id'], 'F-002')
    assert finding['status'] == 'open' and finding['needs_recheck'] == 0
    assert any('rechecked by code' in (h['reason'] or '') for h in env.store.finding_history(finding['id']))


async def test_unsupported_and_edit_requests_get_server_notes(env):
    review = await finished_review(env)
    thread = new_thread(env, review)
    await say(env, thread, 'Run a heat simulation and then fix it for me.', [{'text': 'TrustPCB does not edit boards and has no thermal analysis yet.'}])
    sent = env.runners[-1].requests[0].user_text
    assert 'never edits a design' in sent and 'not available yet' in sent
    snapshot = env.store.latest_report_revision(review['id'])['snapshot']
    assert snapshot['revision'] == 2 and any('Thermal' in t for t in snapshot['not_tested'])               # only the Not tested list changed
    assert snapshot['changes_since_last']['not_tested_added'] and not snapshot['changes_since_last']['new_findings']


async def test_a_session_that_cannot_resume_is_rebuilt_from_the_digest(env):
    review = await finished_review(env)
    thread = new_thread(env, review)
    await say(env, thread, 'First question.', [{'text': 'First answer.'}])
    assert env.store.get_thread(thread['id'])['session_id']

    class Refusing(ScriptedRunner):
        async def run(self, request: RunRequest, emit, interact):
            if request.session_id:
                raise RuntimeError('session not found')
            await super().run(request, emit, interact)

    seen = []
    env.manager.runner_factory = lambda profile: seen.append(Refusing([{'text': 'Second answer.'}])) or seen[-1]
    await followup.send_message(store=env.store, manager=env.manager, thread_id=thread['id'], text='Second question.', selected=[], request_id='second')
    await env.wait()
    states = [e['payload'] for e in env.events(thread['chat_id'], 'status')]
    assert any(s.get('state') == 'session_rebuilt' for s in states)
    assert env.events(thread['chat_id'], 'completed')[-1]
    rebuilt = seen[-1].requests[0]
    assert rebuilt.session_id is None and 'Digest of the finished review' in rebuilt.user_text and 'First question.' in rebuilt.user_text


async def test_a_resumed_session_is_not_sent_the_digest_again(env):
    review = await finished_review(env)
    thread = new_thread(env, review)
    await say(env, thread, 'First question.', [{'text': 'First answer.'}])
    first = env.runners[-1].requests[0]
    assert 'Digest of the finished review' in first.user_text and first.session_id is None
    await say(env, thread, 'Second question.', [{'text': 'Second answer.'}], request_id='again')
    second = env.runners[-1].requests[0]
    assert second.session_id and 'Digest of the finished review' not in second.user_text


async def test_two_runs_that_find_nothing_end_with_no_further_evidence(env):
    review = await finished_review(env)
    thread = new_thread(env, review)
    probe = [{'tool': 'measure', 'args': {'kind': 'distance', 'a': 'ref:C13', 'b': 'ref:U3'}}, {'text': 'Nothing new.'}]
    await say(env, thread, 'Check C13 again.', probe, request_id='a')
    await say(env, thread, 'Check C13 once more.', probe, request_id='b')
    await say(env, thread, 'And again?', probe, request_id='c')
    assert 'no_further_evidence' in results(env, thread, 'measure')[-1]['content']
    assert env.store.latest_report_revision(review['id'])['number'] == 1


async def test_threads_need_a_finished_review_and_keep_its_profile(env):
    review = await finished_review(env)
    thread = new_thread(env, review)
    assert env.store.get_chat(thread['chat_id'])['runner'] == review['profile']['runner']
    running = env.store.create_review(env.version['id'], goal={}, depth='quick')
    with pytest.raises(ValueError):
        followup.start_thread(store=env.store, manager=env.manager, review_id=running['id'], body={})
    with pytest.raises(ValueError):
        followup.start_thread(store=env.store, manager=env.manager, review_id=review['id'], body={'level': 'admin'})
    with pytest.raises(ValueError):
        await followup.send_message(store=env.store, manager=env.manager, thread_id=new_thread(env, running if False else review)['id'], text='  ', selected=[], request_id='x')


async def test_one_run_at_a_time_and_each_message_runs_once(env):
    review = await finished_review(env)
    thread = new_thread(env, review)
    env.use([{'sleep': 0.3}, {'text': 'slow'}])
    first = await followup.send_message(store=env.store, manager=env.manager, thread_id=thread['id'], text='Slow question.', selected=[], request_id='one')
    with pytest.raises(SessionConflict):
        await followup.send_message(store=env.store, manager=env.manager, thread_id=thread['id'], text='Another.', selected=[], request_id='two')
    again = await followup.send_message(store=env.store, manager=env.manager, thread_id=thread['id'], text='Slow question.', selected=[], request_id='one')
    assert again['duplicate'] is True and again['turn_id'] == first['turn_id']
    await env.wait()
    assert len(env.runners) == 2          # the review's runner and exactly one follow-up runner


async def test_the_token_limit_closes_a_conversation(env):
    review = await finished_review(env)
    thread = new_thread(env, review)
    env.store.add_thread_tokens(thread['id'], 1_400_000)
    out = await say(env, thread, 'Still room.', [{'text': 'ok'}])
    assert out['near_limit'] is True
    env.store.add_thread_tokens(thread['id'], 200_000)
    with pytest.raises(ValueError, match='token limit'):
        await followup.send_message(store=env.store, manager=env.manager, thread_id=thread['id'], text='Too late.', selected=[], request_id='late')


async def test_cancel_keeps_verified_changes_in_an_incomplete_revision(env):
    review = await finished_review(env)
    thread = new_thread(env, review)
    env.use([{'tool': 'propose_finding_change', 'args': {'action': 'dismiss', 'finding_code': 'F-001', 'reason': 'It is intentional in this design.'}}, {'sleep': 5.0}, {'text': 'never'}])
    await followup.send_message(store=env.store, manager=env.manager, thread_id=thread['id'], text='Dismiss F-001 and wait.', selected=[], request_id='c')
    await asyncio.sleep(0.1)
    await env.manager.cancel(thread['chat_id'])
    revision = env.store.latest_report_revision(review['id'])
    assert revision['number'] == 2 and revision['snapshot']['incomplete'] is True
    assert env.events(thread['chat_id'], 'canceled') and not env.events(thread['chat_id'], 'report_revised')


# ---- end_followup: the user always gets a reply, and a second call changes nothing ----------------------------------

def streamed(env, thread):
    return ''.join(e['payload']['text'] for e in env.events(thread['chat_id'], 'text_delta'))


async def test_end_followup_without_any_reply_is_refused_then_accepted_with_one(env):
    review = await finished_review(env)
    thread = new_thread(env, review)
    await say(env, thread, 'Is C14 near U3?', [
        {'tool': 'get_finding', 'args': {'code': 'F-001'}},
        {'text': 'Let me look at that.'},                                    # narration before a working tool does not count as the reply
        {'tool': 'measure', 'args': {'kind': 'distance', 'a': 'ref:C14', 'b': 'ref:U3'}},
        {'tool': 'end_followup', 'args': {}},
        {'tool': 'end_followup', 'args': {'reply': 'Too short'}},
        {'tool': 'end_followup', 'args': {'reply': 'C14 is close to U3, but the edge to edge distance is in the measure result above.'}},
    ])
    ends = results(env, thread, 'end_followup')
    assert [json.loads(r['content']).get('error') for r in ends] == ['reply_required', 'reply_required', None]
    assert 'C14 is close to U3' in streamed(env, thread)
    assert json.loads(ends[2]['content'])['ended'] is True


async def test_end_followup_reply_numbers_are_checked(env):
    review = await finished_review(env)
    thread = new_thread(env, review)
    await say(env, thread, 'What is the S11?', [
        {'tool': 'end_followup', 'args': {'reply': 'The worst S11 across the band is -17.3 dB with the change.'}},
        {'tool': 'end_followup', 'args': {'reply': 'I could not measure the S11 here, so there is no number to give you.'}},
    ])
    first, second = [json.loads(r['content']) for r in results(env, thread, 'end_followup')]
    assert first['error'] == 'number_check_failed' and second.get('ended') is True
    assert '-17.3' not in streamed(env, thread) and 'no number to give you' in streamed(env, thread)


async def test_end_followup_needs_no_reply_when_the_answer_was_streamed_after_the_last_tool(env):
    review = await finished_review(env)
    thread = new_thread(env, review)
    await say(env, thread, 'Hello', [{'tool': 'get_finding', 'args': {'code': 'F-001'}}, {'text': 'F-001 stands as written in the report.'}, {'tool': 'end_followup', 'args': {}}])
    (end,) = [json.loads(r['content']) for r in results(env, thread, 'end_followup')]
    assert end['ended'] is True and streamed(env, thread) == 'F-001 stands as written in the report.'


async def test_a_second_end_followup_is_a_harmless_no_op(env):
    review = await finished_review(env)
    thread = new_thread(env, review)
    await say(env, thread, 'Hello', [{'text': 'Nothing to change; the finding stands.'}, {'tool': 'end_followup', 'args': {'reply': 'Ignored: text was streamed already.'}},
                                    {'tool': 'end_followup', 'args': {'reply': 'A different reply that must not appear anywhere.'}}])
    first, second = [json.loads(r['content']) for r in results(env, thread, 'end_followup')]
    assert first['ended'] is True and second == {'ended': True, 'already_ended': True}
    assert streamed(env, thread) == 'Nothing to change; the finding stands.'
    assert env.names(thread['chat_id']).count('text_delta') == len(env.events(thread['chat_id'], 'text_delta'))
    assert not env.events(thread['chat_id'], 'report_revised')


# ---- try_placement beside a pad or a net ----------------------------------------------------------------------------

def test_placement_beside_a_pad_and_a_net(sample_board):
    from trustpcb.review.placement import try_placement
    pad = next(p for p in sample_board.pads if p.ref == 'U3' and p.net)
    out = try_placement(sample_board, 'C13', pad.id)
    assert out['target_pad'] == pad.id and out['placed'].startswith(f'beside {pad.id}')
    assert out['distance_to_target_mm'] == next(r for r in out['distances'] if r['to'] == pad.id)['distance_mm'] and out['distance_to_target_mm'] < 5.0
    assert out['ghost']['ring'] and out['rf_effect'] == 'Not tested'
    net = next(p for p in sample_board.pads if p.net and p.ref != 'C13' and p.ref != 'U3' and not p.net.startswith('GND'))
    by_net = try_placement(sample_board, 'C13', f'net:{net.net}')
    assert by_net['target_pad'].startswith('pad:') and f'net:{net.net}' in by_net['placed'] and 'distance_to_target_mm' in by_net
    with pytest.raises(ValueError, match='Pad pad:U3:NOPE is not on this board'):
        try_placement(sample_board, 'C13', 'pad:U3:NOPE')
    with pytest.raises(ValueError, match='no pad on another part'):
        try_placement(sample_board, 'C13', 'net:NO_SUCH_NET')
    assert try_placement(sample_board, 'C13', 'U3')['placed'].startswith('beside U3')          # a part ref still works


async def test_try_placement_accepts_pad_and_net_ids_through_the_tool(env):
    review = await finished_review(env)
    thread = new_thread(env, review)
    await say(env, thread, 'What if C13 sat beside the U3 pad?', [
        {'tool': 'try_placement', 'args': {'ref': 'C13', 'near': 'pad:U3:A8'}},
        {'tool': 'try_placement', 'args': {'ref': 'C13', 'near': 'net:ANT_FEED'}},
        {'text': 'The ghost fits beside that pad; its radio effect is Not tested.'}, {'tool': 'end_followup', 'args': {}}])
    placements = results(env, thread, 'try_placement')
    assert [p['is_error'] for p in placements] == [False, False]
    assert [c[2] for c in env.lab.calls if c[0] == 'try_placement'] == ['pad:U3:A8', 'net:ANT_FEED']
