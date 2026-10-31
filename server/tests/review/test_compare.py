"""Version tracking: fingerprints, classification, dismissal carry-forward and board differences."""
import copy
from pathlib import Path

from trustpcb.review import compare, followup, report

SCRIPTS = Path(__file__).parent / 'scripts'
BRIEF = {'tool': 'submit_board_brief', 'args': {'purpose': 'Battery powered Bluetooth sensor node', 'part_roles': {'U3': 'radio'}, 'risks': [], 'priorities': ['decoupling']}}


def finding_script(candidate, title, what):
    return [BRIEF, {'tool': 'advance_phase'}, {'tool': 'list_candidates'},
            {'tool': 'record_finding', 'args': {'title': title, 'severity': 'medium', 'what': what, 'why_it_matters': 'A long loop weakens decoupling.',
                                                'recommendation': 'Move the capacitor closer in your layout tool.', 'candidate_ids': [candidate]}},
            {'tool': 'advance_phase'}, {'tool': 'submit_summary', 'args': {'summary': 'One issue was recorded.', 'top_actions': ['Move the capacitor closer.']}}]


async def second_version(env, script, candidates=None, board=None):
    version = env.store.create_version(env.project['id'], label='second')
    env.version = version
    if candidates is not None:
        env.lab.candidates = candidates
    if board is not None:
        original = env.lab.load_board

        async def load(*, version_id):
            return board if version_id == version['id'] else await original(version_id=version_id)

        env.lab.load_board = load
    return await env.review(script, depth='quick')


async def test_fixed_still_present_and_new_findings_between_versions(env, sample_board):
    first = await env.review(finding_script('candidate:decoupling:1', 'Capacitor C13 is far from U3', 'C13 sits 5.2 mm from the U3 supply pin.'), depth='quick')
    keepout = env.lab.candidates[3]
    older = env.store.get_finding_by_code(first['id'], 'F-001')
    kept = [c for c in env.lab.candidates if c['rule'] != 'decoupling']
    kept.append({'id': 'candidate:decoupling:7', 'rule': 'decoupling', 'title': 'C14 far', 'source': 'analyzer', 'targets': ['ref:C14', 'ref:U3'], 'measurements': {'distance': {'value': 4.1, 'unit': 'mm'}}, 'marks': [], 'severity_hint': 'low'})
    changed_board = copy.deepcopy(sample_board)
    changed_board.footprint('C13').x += 3.0
    changed_board.vias.pop()
    second = await second_version(env, finding_script('candidate:decoupling:7', 'Capacitor C14 is far from U3', 'C14 sits 4.1 mm from the U3 supply pin.'), kept, changed_board)
    changes = report.version_changes(env.store, second['id'])
    assert [f['title'] for f in changes['fixed']] == ['Capacitor C13 is far from U3'] and [f['title'] for f in changes['new']] == ['Capacitor C14 is far from U3']
    assert changes['against']['version_number'] == 1
    result = await compare.compare_versions(store=env.store, from_id=env.store.get_review(first['id'])['version_id'], to_id=second['version_id'])
    assert result['findings']['fixed'][0]['fingerprint'] == older['fingerprint']
    assert result['from']['number'] == 1 and result['to']['number'] == 2 and result['counts_change']['medium'] == 0
    board = result['board']
    assert board['parts']['moved'][0]['ref'] == 'C13' and board['parts']['moved'][0]['distance_mm'] == 3.0
    assert board['vias']['removed'] == 1 and board['vias']['count_new'] == board['vias']['count_old'] - 1
    assert board['marks']['to'][0]['targets'] == ['ref:C13'] and board['marks']['from']
    snapshot = env.store.latest_report_revision(second['id'])['snapshot']
    assert snapshot['version_changes']['fixed'] and 'Changes since the previous version' in (await report.render_revision(
        store=env.store, review=second, revision=env.store.latest_report_revision(second['id']), fmt='html')).decode()
    assert keepout['rule'] == 'antenna_keepout'


def test_classification_of_old_and_new_findings(env):
    store = env.store
    old_review = store.create_review(env.version['id'], goal={})
    version2 = store.create_version(env.project['id'])
    new_review = store.create_review(version2['id'], goal={})
    evidence = lambda review, value: store.add_evidence(review['id'], 'candidate', 'analyzer', {'kind': 'measured', 'measurements': {'distance': {'value': value, 'unit': 'mm'}}})['id']
    for fp, sev, value in (('a|net:A|', 'low', 4.0), ('b|net:B|', 'low', 4.0), ('c|net:C|', 'medium', 4.0), ('d|net:D|', 'low', 4.0), ('e|net:E|', 'low', 4.0)):
        store.create_finding(old_review['id'], fp, f'old {fp}', sev, 'measured', evidence_ids=[evidence(old_review, value)], marks=[])
    store.update_finding(store.find_finding(old_review['id'], 'e|net:E|')['id'], 'user', 'fine', status='dismissed', dismissed_reason='on purpose')
    store.create_finding(new_review['id'], 'a|net:A|', 'new a', 'low', 'measured', evidence_ids=[evidence(new_review, 4.05)], marks=[])        # barely moved: still present
    store.create_finding(new_review['id'], 'b|net:B|', 'new b', 'high', 'measured', evidence_ids=[evidence(new_review, 4.0)], marks=[])         # worse: changed
    store.create_finding(new_review['id'], 'e|net:E|', 'new e', 'low', 'measured', evidence_ids=[evidence(new_review, 4.0)], marks=[])         # dismissed before: carried
    store.create_finding(new_review['id'], 'f|net:F|', 'new f', 'low', 'measured', marks=[])                                                  # new
    out = compare.classify_findings(store, store.list_findings(old_review['id']), store.list_findings(new_review['id']))
    assert [f['fingerprint'] for f in out['still_present']] == ['a|net:A|'] and [f['fingerprint'] for f in out['changed']] == ['b|net:B|']
    assert out['changed'][0]['changes'] == ['severity low -> high'] and [f['fingerprint'] for f in out['fixed']] == ['c|net:C|', 'd|net:D|']
    assert [f['fingerprint'] for f in out['new']] == ['f|net:F|'] and out['dismissed_carried'][0]['dismissed_on']['dismissed_reason'] == 'on purpose'
    coverage = compare.classify_findings(store, store.list_findings(old_review['id']), store.list_findings(new_review['id']), new_coverage={'a', 'b', 'c'})
    assert [f['fingerprint'] for f in coverage['not_rechecked']] == ['d|net:D|'] and [f['fingerprint'] for f in coverage['fixed']] == ['c|net:C|']


async def test_dismissals_carry_forward_to_the_next_version(env):
    first = await env.review(finding_script('candidate:decoupling:1', 'Capacitor C13 is far from U3', 'C13 sits 5.2 mm from the U3 supply pin.'), depth='quick')
    finding = env.store.get_finding_by_code(first['id'], 'F-001')
    env.store.update_finding(finding['id'], 'user', 'intentional', status='dismissed', dismissed_reason='Shared with C14 on purpose.')
    second = await second_version(env, finding_script('candidate:decoupling:1', 'Capacitor C13 is far from U3', 'C13 sits 5.2 mm from the U3 supply pin.'))
    carried = env.store.get_finding_by_code(second['id'], 'F-001')
    assert carried['status'] == 'dismissed' and carried['dismissed_reason'] == 'Dismissed on v1: Shared with C14 on purpose.'
    states = {c['candidate_id']: c['review_state'] for c in env.store.list_candidates(second['id'])}
    assert states['candidate:decoupling:1'] == 'recorded'
    assert env.store.severity_counts(second['id'])['medium'] == 0                         # a carried dismissal is not an open finding
    snapshot = env.store.latest_report_revision(second['id'])['snapshot']
    assert snapshot['version_changes']['dismissed_carried'][0]['dismissed_on']['dismissed_reason'] == 'Shared with C14 on purpose.'
    assert 'v1' in (await report.render_revision(store=env.store, review=second, revision=env.store.latest_report_revision(second['id']), fmt='html')).decode()


async def test_a_recheck_is_told_what_the_previous_version_found(env):
    await env.review(finding_script('candidate:decoupling:1', 'Capacitor C13 is far from U3', 'C13 sits 5.2 mm from the U3 supply pin.'), depth='quick')
    second = await second_version(env, [{'tool': 'get_status'}])
    request = env.runners[-1].requests[0]
    assert 'You are reviewing version 2' in request.user_text and 'previous version is version 1' in request.user_text and 'F-001' in request.user_text and 'decoupling||ref:C13,ref:U3' in request.user_text
    assert second['end_state'] in ('agent_stopped', 'complete')
    first = env.store.list_reviews(version_id=env.store.list_versions(env.project['id'])[0]['id'])[-1]
    assert second['summary']['previous_review_id'] == first['id'] and second['summary']['previous_version_number'] == 1 and first['id'] in request.user_text
    assert 'Investigate only what changed' in request.user_text and 'fixed, still present or changed' in request.user_text
    status = [e['payload'] for e in env.events(second['chat_id'], 'tool_result') if e['payload']['name'] == 'get_status'][0]
    assert 'recheck' in status['content'] and 'not_recorded_again' in status['content']


async def test_a_recheck_summary_counts_fixed_still_present_and_new(env):
    await env.review(finding_script('candidate:decoupling:1', 'Capacitor C13 is far from U3', 'C13 sits 5.2 mm from the U3 supply pin.'), depth='quick')
    second = await second_version(env, [{'tool': 'get_status'}])                      # the agent records nothing, but the same analyzer result is still reported: still present, not fixed
    assert 'Findings compared with version 1: 0 fixed, 0 improved, 1 still present, 0 worse, 0 new.' in second['summary']['summary']['text']
    changes = report.version_changes(env.store, second['id'])
    assert changes['still_present'][0]['unrecorded'] and changes['counts']['fixed'] == 0 and 'still report these results' in changes['still_present'][0]['note']


async def test_spotlight_and_placement_events(env):
    review = await env.review(finding_script('candidate:decoupling:1', 'Capacitor C13 is far from U3', 'C13 sits 5.2 mm from the U3 supply pin.'), depth='quick')
    thread = followup.start_thread(store=env.store, manager=env.manager, review_id=review['id'], body={})
    env.use([{'tool': 'show_on_board', 'args': {'finding_code': 'F-001', 'label': 'The capacitor in question'}},
             {'tool': 'try_placement', 'args': {'ref': 'C13', 'near': 'U3'}}, {'tool': 'try_placement', 'args': {'ref': 'C99', 'near': 'U3'}}])
    await followup.send_message(store=env.store, manager=env.manager, thread_id=thread['id'], text='What about C13 next to U3?', selected=[], request_id='p')
    await env.wait()
    spotlight = env.events(thread['chat_id'], 'spotlight')[0]['payload']
    assert spotlight['label'] == 'The capacitor in question' and spotlight['resolved']
    ready = env.events(thread['chat_id'], 'whatif_ready')[0]['payload']
    assert ready['label'] == 'What-if, not your board' and ready['ghost'] == {'x': 30.0, 'y': 14.0} and ready['distances'][0]['to'] == 'ref:U3'
    placement = [p for p in env.tool_results(thread['chat_id']) if p['name'] == 'try_placement']
    assert 'Not tested' in placement[0]['content'] and 'unknown_part' in placement[1]['content']
    assert env.store.latest_report_revision(review['id'])['number'] == 1                    # a ghost placement alone never changes the report
