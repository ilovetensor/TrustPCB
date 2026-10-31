"""The layout judgement gate: placement and routing must be judged and drawn on the board before phase 3 ends (spec sections 6 and 7)."""
import json

from gate_steps import REASON, dismiss, views
from trustpcb.review import budgets

BRIEF = {'tool': 'submit_board_brief', 'args': {'purpose': 'Battery powered Bluetooth sensor node with a printed antenna', 'part_roles': {'U3': 'radio'}, 'risks': [], 'priorities': ['antenna']}}
ADVANCE = {'tool': 'advance_phase'}
TO_REVIEW = [BRIEF, ADVANCE]
SHORT = 'A shallow corner on a slow track.'
LONG = 'The one shallow corner is on a slow, low-current supply track, so tidying it would change nothing about this board working.'
NON_LAYOUT = ('decoupling', 'antenna_keepout', 'drc_clearance')


def contents(env, review, name):
    return [json.loads(e['payload']['content']) for e in env.events(review['chat_id'], 'tool_result') if e['payload']['name'] == name]


def detours(n=2):
    return [{'id': f'candidate:track_detour:{i}', 'rule': 'track_detour', 'title': f'ANT_CHIP routed {20 + i}.0 mm for a 9.0 mm straight line', 'source': 'analyzer',
             'measurements': {'extra_length': {'value': 11.0 + i, 'unit': 'mm'}}, 'targets': ['track:17bf021d'],
             'marks': [{'type': 'path', 'targets': ['track:17bf021d'], 'label': 'detour'}], 'severity_hint': 'medium'} for i in range(1, n + 1)]


def layout_finding(**extra):
    return {'tool': 'record_finding', 'args': {'title': 'A long detour where a straight route would do', 'severity': 'medium',
                                               'what': 'This track is routed far longer than the straight line between its ends.',
                                               'why_it_matters': 'A long route picks up noise and adds delay on this sensor node.',
                                               'recommendation': 'Reroute the track more directly in your own tool.', **extra}}


def opinion(target='ref:U3', **extra):
    return {'tool': 'record_finding', 'args': {'title': 'The regulator parts are not grouped', 'severity': 'low',
                                               'what': 'In the close-up view the input capacitor sits away from the regulator it serves.',
                                               'why_it_matters': 'A long loop on the power input adds ripple for a battery sensor.',
                                               'recommendation': 'Group the regulator and its capacitors in your layout tool. This is engineering opinion from the view.',
                                               'targets': [target], 'rule': 'power_grouping', **extra}}


def prepare(*dismissed, short_angle=True):
    steps = dismiss(*dismissed)
    steps.append({'tool': 'dismiss_rule_group', 'args': {'rule': 'track_angle', 'reason': SHORT if short_angle else LONG}})
    return steps


async def test_a_short_reason_does_not_satisfy_the_layout_gate_and_a_specific_one_does(env):
    steps = TO_REVIEW + prepare(*NON_LAYOUT) + views(2) + [ADVANCE, {'tool': 'advance_phase', 'args': {'layout_reasons': {'track_angle': 'too short'}}},
                                                       {'tool': 'advance_phase', 'args': {'layout_reasons': {'track_angle': LONG}}}]
    review = await env.review(steps)
    refused, still, passed = contents(env, review, 'advance_phase')[1:4]
    assert refused['error'] == 'layout_review_incomplete'
    assert refused['layout'] == {'findings_recorded': 0, 'findings_needed': 2, 'groups_without_finding_or_reason': [{'rule': 'track_angle', 'results': 1, 'worst': 'low'}], 'short_reasons': []}
    assert 'sketch' in refused['message'] and 'layout_reasons' in refused['message'] and '60 characters' in refused['message']
    assert still['error'] == 'layout_review_incomplete' and still['layout']['short_reasons'] == ['track_angle']
    assert passed['phase'] == 4
    assert review['summary']['layout_review']['reasons']['track_angle'] == LONG
    assert env.events(review['chat_id'], 'budget')[-1]['payload']['tool_calls']['3'] == 4 + 2 + 2        # four dismissals, two views and the two refusals: each refusal cost a phase 3 call


async def test_a_specific_dismissal_of_the_group_counts_as_the_reason(env):
    review = await env.review(TO_REVIEW + prepare(*NON_LAYOUT, short_angle=False) + views(2) + [ADVANCE])
    assert contents(env, review, 'advance_phase')[1]['phase'] == 4
    assert review['summary']['layout_review']['reasons']['track_angle'] == LONG


async def test_findings_from_layout_groups_and_anchored_opinions_pass_the_gate(env):
    env.lab.candidates = env.lab.candidates + detours(2)
    steps = TO_REVIEW + dismiss(*NON_LAYOUT) + views(2) + [
        layout_finding(rule_group='track_detour',
                       marks=[{'type': 'sketch', 'targets': ['point:20,14', 'point:29,14'], 'label': 'a more direct route, for the engineer to apply'}]),
        {'tool': 'dismiss_rule_group', 'args': {'rule': 'track_angle', 'reason': SHORT}},      # a short reason is fine once two findings judge the layout
        ADVANCE, opinion(), ADVANCE]
    review = await env.review(steps)
    gate = contents(env, review, 'advance_phase')
    assert gate[1]['error'] == 'layout_review_incomplete' and gate[1]['layout']['findings_recorded'] == 1 and [g['rule'] for g in gate[1]['layout']['groups_without_finding_or_reason']] == ['track_angle']
    assert gate[2]['phase'] == 4
    finding = env.store.get_finding_by_code(review['id'], 'F-001')
    kinds = [m['type'] for m in finding['marks']]
    assert kinds[0] == 'pin' and 'path' in kinds and 'sketch' in kinds                       # the analyzer's own path mark and the agent's suggested route
    path = next(m for m in finding['marks'] if m['type'] == 'path')
    assert path['resolved'] and path['label'] == 'detour'
    second = env.store.get_finding_by_code(review['id'], 'F-002')
    assert second['class'] == 'opinion' and second['marks'][0]['resolved']


async def test_analyzer_marks_and_zone_regions_are_attached_automatically(env):
    steps = TO_REVIEW + [
        {'tool': 'record_finding', 'args': {'title': 'Ground pour inside the antenna clearance', 'severity': 'high', 'what': 'Copper sits inside the antenna clearance zone.',
                                            'why_it_matters': 'Copper beside the antenna detunes it.', 'recommendation': 'Review the pour near the antenna in your own tool.',
                                            'rule_group': 'antenna_keepout'}},
        {'tool': 'record_finding', 'args': {'title': 'C13 is far from the U3 supply pin', 'severity': 'medium', 'what': 'The capacitor sits away from the pin it serves.',
                                            'why_it_matters': 'A long loop adds ripple on the radio supply.', 'recommendation': 'Move the capacitor closer in your own tool.',
                                            'candidate_ids': ['candidate:decoupling:1']}}]
    review = await env.review(steps)
    keepout = env.store.get_finding_by_code(review['id'], 'F-001')
    assert [m['type'] for m in keepout['marks']] == ['pin', 'object', 'region']
    region = keepout['marks'][2]
    assert region['targets'] == ['zone:46abab45'] and region['resolved'] and region['resolved'][0].get('bbox')
    decoupling = env.store.get_finding_by_code(review['id'], 'F-002')
    assert [m['type'] for m in decoupling['marks']] == ['pin', 'link'] and decoupling['marks'][1]['targets'] == ['ref:C13', 'ref:U3']
    events = [e['payload'] for e in env.events(review['chat_id'], 'marks_changed')]
    assert events[-1]['marks'][1]['resolved']                                                      # the board picture gets geometry with the event


async def test_an_unanchored_or_out_of_view_opinion_does_not_count(env):
    steps = TO_REVIEW + prepare(*NON_LAYOUT) + views(2) + [
        opinion(target='ref:H1', title='The mounting hole is far from everything', rule='mounting'),                      # H1 is nowhere near either close-up view
        opinion(title='The board looks crowded overall', rule='crowding', targets=[], scope='board'),                     # a whole-board remark has no anchor
        ADVANCE]
    review = await env.review(steps)
    assert contents(env, review, 'advance_phase')[1]['error'] == 'layout_review_incomplete'
    assert contents(env, review, 'advance_phase')[1]['layout']['findings_recorded'] == 0
    assert len(env.store.list_findings(review['id'])) == 2


async def test_quick_needs_one_layout_finding(env):
    quick_steps = TO_REVIEW + prepare(*NON_LAYOUT) + views(1) + [ADVANCE, opinion(), ADVANCE]
    review = await env.review(quick_steps, depth='quick')
    first, second = contents(env, review, 'advance_phase')[1:3]
    assert first['error'] == 'layout_review_incomplete' and first['layout']['findings_needed'] == 1
    assert second['phase'] == 6 or second['phase'] == 4
    assert budgets.MIN_LAYOUT_FINDINGS == {'quick': 1, 'standard': 2, 'deep': 2}


async def test_a_budget_wall_lets_the_run_leave_and_records_the_gap(env):
    review = await env.review(TO_REVIEW + prepare(*NON_LAYOUT) + views(2) + [{'usage': {'input_tokens': 4_100_000, 'output_tokens': 20}}, ADVANCE])
    passed = contents(env, review, 'advance_phase')[1]
    assert passed.get('error') is None and passed['coverage_waived']['layout_groups_missing'][0]['rule'] == 'track_angle'
    assert review['summary']['layout_review']['waived']['findings'] == 0
    assert any(l['rule'] == 'coverage_waived' and 'layout judgement incomplete' in l['detail'] for l in review['summary']['limits'])


async def test_status_in_phase_three_shows_what_the_layout_gate_still_wants(env):
    review = await env.review(TO_REVIEW + [{'tool': 'get_status'}] + dismiss('track_angle'))
    status = contents(env, review, 'get_status')[-1]
    assert status['layout_review'] == {'findings_recorded': 0, 'findings_needed': 2, 'groups_without_finding_or_reason': [{'rule': 'track_angle', 'results': 1, 'worst': 'low'}]}
