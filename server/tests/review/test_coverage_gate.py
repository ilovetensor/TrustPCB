"""The phase 3 gates the server enforces: every rule group resolved, the open review looked at, and no way to trap the run."""
import json


from gate_steps import DEFAULT_RULES, REASON, clear, dismiss, views
from trustpcb.review import budgets, report
from trustpcb.review.lab import LabUnavailable

BRIEF = {'tool': 'submit_board_brief', 'args': {'purpose': 'Battery powered Bluetooth sensor node with a printed antenna', 'part_roles': {'U3': 'radio'}, 'risks': [],
                                               'priorities': ['antenna']}}
ADVANCE = {'tool': 'advance_phase'}
TO_REVIEW = [BRIEF, ADVANCE]


def contents(env, review, name):
    return [e['payload']['content'] for e in env.events(review['chat_id'], 'tool_result') if e['payload']['name'] == name]


def states(env, review):
    counts = {}
    for row in env.store.list_candidates(review['id']):
        counts.setdefault(row['rule'], {}).setdefault(row['review_state'], 0)
        counts[row['rule']][row['review_state']] += 1
    return counts


def return_path(n=43):
    return [{'id': f'candidate:return_path:{i}', 'rule': 'return_path', 'title': f'Fast net crosses a gap in ground {i}', 'source': 'analyzer',
             'measurements': {'gap_length': {'value': 1.0 + i * 0.5, 'unit': 'mm'}}, 'targets': [f'point:{20 + i * 0.4:g},14'], 'marks': [],
             'severity_hint': 'medium' if i % 2 else 'high', 'position': [20 + i * 0.4, 14]} for i in range(1, n + 1)]


def finding(**extra):
    return {'tool': 'record_finding', 'args': {'title': 'The fast signals lose their ground return across the board', 'severity': 'high',
                                               'what': 'Many fast tracks cross gaps in the ground pour beneath them.', 'why_it_matters': 'The radio sensor node will radiate and pick up noise where return current has no path.',
                                               'recommendation': 'Close the gaps in the ground pour under these tracks in your layout tool.', **extra}}


# ---- coverage ---------------------------------------------------------------------------------------

async def test_leaving_phase_three_needs_every_rule_group_resolved(env):
    review = await env.review(TO_REVIEW + [ADVANCE] + clear() + [ADVANCE])
    refused, passed = contents(env, review, 'advance_phase')[1:3]
    data = json.loads(refused)
    assert data['error'] == 'coverage_incomplete'
    assert [g['rule'] for g in data['unresolved_rule_groups']] == ['antenna_keepout', 'decoupling', 'drc_clearance', 'track_angle']      # worst first
    assert data['unresolved_rule_groups'][0]['worst'] == 'high' and data['region_views'] == {'taken': 0, 'needed': 2}
    assert '"phase":4' in passed.replace(' ', '')
    assert states(env, review) == {'decoupling': {'dismissed': 2}, 'track_angle': {'dismissed': 1}, 'antenna_keepout': {'dismissed': 1}, 'drc_clearance': {'dismissed': 1}}
    # the refusal used a phase 3 call: 1 refusal + 4 dismissals + 2 views
    assert env.events(review['chat_id'], 'budget')[-1]['payload']['tool_calls']['3'] == 7


async def test_one_finding_can_cover_a_whole_rule_group(env):
    env.lab.candidates = env.lab.candidates + return_path(43)
    steps = TO_REVIEW + [finding(rule_group='return_path'), ADVANCE] + dismiss(*DEFAULT_RULES) + views(2) + [ADVANCE]
    review = await env.review(steps)
    result = json.loads(contents(env, review, 'record_finding')[0])
    assert result['status'] == 'created' and result['candidates_covered'] == 43
    assert states(env, review)['return_path'] == {'recorded': 43}
    assert len(env.store.list_findings(review['id'])) == 1
    row = env.store.list_findings(review['id'])[0]
    assert len(row['evidence_ids']) == 43 and row['class'] == 'measured' and row['marks']


async def test_a_rule_group_can_be_split_into_several_findings(env):
    env.lab.candidates = env.lab.candidates + return_path(6)
    first = finding(candidate_ids=['candidate:return_path:1', 'candidate:return_path:2'], title='Two fast tracks lose their ground return', targets=['point:20.4,14'])
    second = finding(rule_group='return_path', title='The other fast tracks lose their ground return', severity='medium')
    review = await env.review(TO_REVIEW + [first, second])
    assert len(env.store.list_findings(review['id'])) == 2
    assert states(env, review)['return_path'] == {'recorded': 6}


async def test_a_dismissed_group_needs_a_real_reason_when_it_is_risky(env):
    steps = TO_REVIEW + [{'tool': 'dismiss_rule_group', 'args': {'rule': 'antenna_keepout', 'reason': 'looks fine'}},
                         {'tool': 'dismiss_rule_group', 'args': {'rule': 'no_such_rule', 'reason': 'It does not exist at all.'}},
                         {'tool': 'dismiss_rule_group', 'args': {'rule': 'track_angle', 'reason': 'A shallow corner on a slow track.'}},
                         {'tool': 'dismiss_rule_group', 'args': {'rule': 'track_angle', 'reason': 'A shallow corner on a slow track.'}}]
    review = await env.review(steps)
    short, unknown, ok_, again = contents(env, review, 'dismiss_rule_group')
    assert 'reason_too_short' in short and 'unknown_rule' in unknown and '"dismissed_rule":"track_angle"' in ok_ and 'already_resolved' in again
    assert states(env, review)['antenna_keepout'] == {'unreviewed': 1}        # never resolved: the report lists it as unreviewed
    assert states(env, review)['track_angle'] == {'dismissed': 1}


async def test_info_only_groups_are_grouped_by_code_at_the_gate(env):
    parity = [{'id': f'candidate:parity:{i}', 'rule': 'parity', 'title': 'Footprint differs from the library', 'source': 'kicad', 'measurements': {}, 'targets': ['ref:C13'],
               'marks': [], 'severity_hint': 'info'} for i in range(1, 6)]
    env.lab.candidates = env.lab.candidates + parity
    review = await env.review(TO_REVIEW + clear() + [ADVANCE])
    passed = json.loads(contents(env, review, 'advance_phase')[1])
    assert passed['phase'] == 4 and passed['auto_grouped_info_groups'] == [{'rule': 'parity', 'count': 5}]
    assert states(env, review)['parity'] == {'grouped': 5}
    snapshot = env.store.latest_report_revision(review['id'])['snapshot']
    assert any('parity' in item and 'not reviewed one by one' in item for item in snapshot['not_tested'])


# ---- open review ------------------------------------------------------------------------------------

async def test_the_open_review_needs_region_views(env):
    full = {'tool': 'view_board', 'args': {'view': 'top'}}
    one = {'tool': 'view_board', 'args': {'around': 'ref:U3', 'margin_mm': 6}}
    same = {'tool': 'view_board', 'args': {'around': 'ref:U3', 'margin_mm': 6}}
    other = {'tool': 'view_board', 'args': {'around': 'net:ANT_FEED', 'margin_mm': 3}}
    review = await env.review(TO_REVIEW + dismiss(*DEFAULT_RULES) + [full, ADVANCE, one, same, ADVANCE, other, ADVANCE])
    advances = [json.loads(c) if c.startswith('{"error"') else c for c in contents(env, review, 'advance_phase')]
    assert advances[1]['error'] == 'open_review_incomplete' and advances[1]['region_views'] == {'taken': 0, 'needed': 2}
    assert advances[2]['error'] == 'open_review_incomplete' and advances[2]['region_views'] == {'taken': 1, 'needed': 2}
    assert '"phase":4' in advances[3].replace(' ', '')
    seen = [json.loads(c)['region_views']['counted'] for c in contents(env, review, 'view_board')]
    assert seen == [False, True, False, True]          # a whole-board view and a repeat of the same window do not count


async def test_quick_needs_one_region_view(env):
    one = {'tool': 'view_board', 'args': {'around': 'ref:U3'}}
    review = await env.review(TO_REVIEW + dismiss(*DEFAULT_RULES) + [ADVANCE, one, ADVANCE], depth='quick')
    first, second = contents(env, review, 'advance_phase')[1:3]
    assert json.loads(first)['region_views'] == {'taken': 0, 'needed': 1} and '"phase":6' in second.replace(' ', '')


async def test_view_board_presets(env, sample_board):
    u3 = sample_board.resolve('ref:U3').to_dict()['bbox']
    steps = TO_REVIEW + [
        {'tool': 'view_board', 'args': {'around': 'ref:U3', 'margin_mm': 3}},
        {'tool': 'view_board', 'args': {'around': 'ref:U3', 'region': [0, 0, 5, 5]}},
        {'tool': 'view_board', 'args': {'around': 'ref:NOPE'}},
        {'tool': 'view_board', 'args': {'around': 'zone_role:radio', 'annotate': True, 'view': 'top'}},
        {'tool': 'view_board', 'args': {'around': 'candidate:decoupling:1', 'annotate': True}},
        {'tool': 'view_board', 'args': {'region': [10, 5, 40, 30], 'annotate': True}},
    ]
    env.lab.candidates[0]['position'] = [(u3[0] + u3[2]) / 2, (u3[1] + u3[3]) / 2]
    review = await env.review(steps)
    results = [json.loads(c) for c in contents(env, review, 'view_board')]
    calls = [c for c in env.lab.calls if c[0] == 'view_board']
    region = calls[0][2]
    assert region[0] <= u3[0] - 3 + 1e-6 and region[2] >= u3[2] + 3 - 1e-6 and region[1] <= u3[1] - 3 + 1e-6 and region[3] >= u3[3] + 3 - 1e-6
    assert results[1]['error'] == 'invalid_view' and results[2]['error'] == 'anchor_unresolved'
    assert results[0]['region_views']['counted'] is True
    annotated = calls[-1][3]
    assert annotated and all('number' in m for m in annotated) and results[-1]['marks'][0]['number'] == 1
    assert {m['candidate'] for m in results[-1]['marks']} >= {'candidate:decoupling:1'}


# ---- the gate can never trap the run ------------------------------------------------------------------

async def test_each_refusal_counts_against_the_phase_budget_and_the_run_still_ends(env):
    review = await env.review(TO_REVIEW + [ADVANCE] * 40)
    results = contents(env, review, 'advance_phase')
    refusals = [r for r in results if 'coverage_incomplete' in r]
    assert 1 <= len(refusals) <= budgets.REFUSAL_STREAK_LIMIT
    assert review['end_state'] == 'agent_stopped' and review['summary']['end']['reason'] == 'loop'
    assert env.store.latest_report_revision(review['id'])                      # the report still happens
    assert env.events(review['chat_id'], 'budget')[-1]['payload']['tool_calls']['3'] == len(refusals)


async def test_when_the_phase_calls_run_out_the_gate_gives_way(env, monkeypatch):
    config = budgets.CONFIGS['standard']
    monkeypatch.setitem(budgets.CONFIGS, 'standard', config.with_(phase_calls={**config.phase_calls, 3: 4}))
    review = await env.review(TO_REVIEW + [{'tool': 'list_candidates'}, {'tool': 'list_candidates'}, {'tool': 'list_candidates'}, ADVANCE, ADVANCE])
    refused, waived = contents(env, review, 'advance_phase')[1:3]
    assert 'coverage_incomplete' in refused                 # the refusal itself took the fourth and last call
    assert '"phase":4' in waived.replace(' ', '') and 'coverage_waived' in waived
    rules = {l['rule'] for l in review['summary']['limits']}
    assert {'coverage_waived', 'budget_wall'} <= rules and review['end_state'] == 'complete_with_limits'
    snapshot = env.store.latest_report_revision(review['id'])['snapshot']
    assert any('coverage' in t.lower() for t in snapshot['not_tested']) and snapshot['candidates']['unreviewed'] == 5
    assert {g['rule'] for g in review['summary']['unreviewed_groups']} == set(DEFAULT_RULES)


async def test_a_budget_wall_does_not_block_the_closing_path(env):
    review = await env.review(TO_REVIEW + [{'usage': {'input_tokens': 4_100_000, 'output_tokens': 20}}, ADVANCE])
    passed = contents(env, review, 'advance_phase')[1]
    assert 'error' not in passed[:12] and 'coverage_waived' in passed


async def test_no_region_views_are_demanded_when_the_board_cannot_be_drawn(env):
    async def broken(**kwargs):
        raise LabUnavailable('The renderer is not installed.')

    env.lab.view_board = broken
    review = await env.review(TO_REVIEW + [{'tool': 'view_board', 'args': {'around': 'ref:U3'}}] + dismiss(*DEFAULT_RULES) + [ADVANCE])
    assert 'unavailable' in contents(env, review, 'view_board')[0]
    assert '"phase":4' in contents(env, review, 'advance_phase')[1].replace(' ', '')


# ---- how it reads ---------------------------------------------------------------------------------------

async def test_the_code_written_summary_reads_like_a_note_from_an_engineer(env):
    env.lab.candidates = env.lab.candidates + return_path(8)
    crash = {'fail': 'upstream connection lost'}
    review = await env.review(TO_REVIEW + [finding(rule_group='return_path'), crash])
    summary = env.store.latest_report_revision(review['id'])['snapshot']['summary']
    assert summary['source'] == 'code'
    text = summary['text']
    assert 'Battery powered Bluetooth sensor node' in text and '1 open finding(s): 1 high' in text and 'groups the checks that share one cause' in text
    assert 'F-001 (high) The fast signals lose their ground return' in text and 'radio sensor node will radiate' in text
    assert 'Not reviewed:' in text and 'decoupling' in text and 'RF behaviour was not tested' in text and 'ended early' in text
    assert summary['top_actions'] == ['F-001: Close the gaps in the ground pour under these tracks in your layout tool.']


async def test_grouped_findings_and_cleared_checks_read_well_in_the_report(env):
    env.lab.candidates = env.lab.candidates + return_path(20)
    steps = TO_REVIEW + [finding(rule_group='return_path')] + dismiss(*DEFAULT_RULES) + views(2) + [ADVANCE, ADVANCE]
    review = await env.review(steps, depth='quick')
    snapshot = env.store.latest_report_revision(review['id'])['snapshot']
    assert snapshot['review_work']['region_views'] == 2 and len(snapshot['review_work']['dismissals']) == 4
    html = (await report.render_revision(store=env.store, review=review, revision=env.store.latest_report_revision(review['id']), fmt='html')).decode()
    assert 'Checked and cleared' in html and REASON[:30] in html and 'and 12 more results grouped under this finding' in html
    assert 'close-up board view' in html


# ---- RF stills ----------------------------------------------------------------------------------------------

async def html_of(env, review):
    return (await report.render_revision(store=env.store, review=review, revision=env.store.latest_report_revision(review['id']), fmt='html')).decode()


async def test_the_report_shows_field_and_pattern_stills_when_rf_results_exist(env):
    review = await env.review(__import__('pathlib').Path(__file__).parent / 'scripts' / 'standard_success.json')
    html = await html_of(env, review)
    assert 'Field still' in html and 'Radiation pattern' in html and html.count('data:image/png;base64,') >= 2
    kinds = [c[1] for c in env.lab.calls if c[0] == 'view_result']
    assert 'field' in kinds and 'pattern' in kinds


async def test_a_still_the_lab_cannot_draw_is_reported_as_not_tested(env):
    review = await env.review(__import__('pathlib').Path(__file__).parent / 'scripts' / 'standard_success.json')
    original = env.lab.view_result

    async def no_pattern(*, review_id, result, kind, params):
        if kind == 'pattern':
            raise LabUnavailable('The far-field result is missing.')
        return await original(review_id=review_id, result=result, kind=kind, params=params)

    env.lab.view_result = no_pattern
    html = await html_of(env, review)
    assert 'Field still' in html and 'Radiation pattern</h3>' not in html
    assert 'Radiation-pattern still: The far-field result is missing' in html and 'Not tested' in html


async def test_without_rf_results_the_report_says_clearly_what_was_not_tested(env):
    review = await env.review(TO_REVIEW + clear() + [ADVANCE, {'tool': 'submit_summary', 'args': {'summary': 'The board was reviewed without simulation.', 'top_actions': ['Review the antenna by measurement.']}}], depth='quick')
    html = await html_of(env, review)
    section = html.split('<h2>RF result</h2>')[1].split('<h2>')[0]
    assert 'Not tested' in section and 'radiation pattern' in section and 'S11 curve' in section and 'engineering opinion' in section
    assert 'data:image' not in section
