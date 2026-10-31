"""Conclusions follow the evidence: the confirm verdict, number binding, ranges, counts, stale screen text, part roles and ruled-out suspects.

Each wrong quote below is an exact sentence from the independent fact-check of a real review. The rows are built to the same shape as that review's.
"""
import pytest
from gate_steps import clear

from trustpcb.review import evidence as ev
from trustpcb.review import report, restate, roles, verdict
from trustpcb.review.findings import MAX_CLAIM_TRIES, FindingEngine, FindingRejected
from trustpcb.storage import Store

GOOD = dict(title='The antenna misses the target', severity='high', what='Simulated, the worst in-band S11 is far from the target.', why_it_matters='Most power is reflected in simulation.',
            recommendation='Try the parts in your own tool and measure on a built board.')
TARGET = -10.0


def measure(worst, mean=None):
    return {'s11_band_worst': {'value': worst, 'unit': 'dB'}, 'as_built_band_worst_s11': {'value': worst, 'unit': 'dB'}, 's11_band_mean': {'value': mean if mean is not None else worst, 'unit': 'dB'}}


@pytest.fixture
def kit(tmp_path, sample_board):
    return build_kit(tmp_path, sample_board)


def build_kit(tmp_path, sample_board, hold='held_smaller', outcome='improved_short_of_target', held=True):
    store = Store(tmp_path / 'data')
    version = store.create_version(store.create_project('p')['id'])
    review = store.create_review(version['id'], goal={'target_s11_db': TARGET, 'band_hz': [2.4e9, 2.4835e9]})
    engine = FindingEngine(store, review['id'], sample_board, lambda: ev.context_quantities({'target_s11_db': TARGET, 'band_hz': [2.4e9, 2.4835e9]}, [], sample_board, None))
    engine.target_db = TARGET
    rid = review['id']

    def add(kind, label, ref, worst, **extra):
        values = {'kind': 'simulated', 'quality': 'confirm', 'trust': {'status': 'ok', 'reasons': []}, 'label': label, 'ref': ref, 'measurements': measure(worst), **extra}
        return store.add_evidence(rid, kind, 'confirm_result', values)

    base = add(ev.CONFIRM, 'confirm base', 'R4', -0.5)
    free = add(ev.CALCULATION, 'confirm matching, free tuning', 'R6', -5.78, experiment='matching', applied_to='R4', parts=[{'ref': 'C20', 'now': '0.3 pF'}])
    geometry = add(ev.CONFIRM, 'confirm refined candidate', 'R10', -0.45, whatif_ops=[{'op': 'adjust_antenna_length', 'delta_mm': -4.0}])
    locked = add(ev.CALCULATION, 'confirm matching, parts locked', 'R11', -2.06, experiment='matching', applied_to='R10', parts=[{'ref': 'C20', 'now': '36 nH'}])
    refine_free = add(ev.CALCULATION, 'confirm matching, free tuning', 'R12', -5.27, experiment='matching', applied_to='R10', parts=[{'ref': 'C20', 'now': '0.36 pF'}])

    def confirmation(best_row, best_worst, base_worst=-0.5):
        return {'outcome': outcome, 'hold': hold, 'statement': 'The improvement held at confirm quality but the screen overstated it, and the tuning target is not met.', 'held': held,
                'meets_target': False, 'base_evidence': base['code'], 'best': {'evidence': best_row['code'], 'band_worst_s11_db': best_worst, 'meets_target': False, 'parts': []},
                'base': {'band_worst_s11_db': base_worst}, 'best_gain_db': round(base_worst - best_worst, 2) * -1 if best_worst > base_worst else round(base_worst - best_worst, 2),
                'matching_at_confirm': {'locked': {'ref': 'R11', 'band_worst_s11_db': -2.06, 'meets_target': False}, 'free': {'ref': 'R12', 'band_worst_s11_db': -5.27, 'meets_target': False}}}
    first = add(ev.CALCULATION, 'confirm comparison', None, -5.78, confirmation=confirmation(free, -5.78))
    refine = add(ev.CALCULATION, 'refine confirm comparison', None, -5.27, confirmation=confirmation(refine_free, -5.27))
    rows = dict(base=base, free=free, geometry=geometry, locked=locked, refine_free=refine_free, first=first, refine=refine)
    return type('Kit', (), {'store': store, 'review': review, 'engine': engine, 'rows': rows, 'board': sample_board,
                            'codes': lambda self, *names: [rows[n]['code'] for n in names]})()


def record(kit, **args):
    args.setdefault('evidence_ids', kit.codes(*kit.rows))
    args.setdefault('scope', 'board')
    return kit.engine.record({**GOOD, **args}, candidates={})


def refused(kit, **args):
    with pytest.raises(FindingRejected) as info:
        record(kit, **args)
    return info.value


# ---- item 2: the refine outcome is not misreported -------------------------------------------------------------------------------

def test_the_refine_quote_from_the_fact_check_is_refused(kit):
    wrong = ('Refine attempt (%s): a 4 mm shorter trace with matching parts did not hold at confirm quality. Worst S11 was -0.45 dB, about the same as the base, so the screen overstated it.'
             % kit.rows['geometry']['code'])
    error = refused(kit, what=wrong)
    assert error.code == 'contradicts_confirm' and 'held' in error.message and kit.rows['first']['code'] in error.message
    assert error.data['verdict']['held'] is True and error.data['verdict']['meets_target'] is False


def test_the_minus_045_number_is_tied_to_the_geometry_run_not_to_matching_parts(kit):
    error = refused(kit, what='A second attempt combining a 4 mm shorter trace with new matching parts reached a worst S11 of -0.45 dB, about the same as the base.')
    assert error.code == 'number_from_other_run'
    assert kit.rows['locked']['code'] in error.message and kit.rows['refine_free']['code'] in error.message and '-2.06' in error.message and '-5.27' in error.message


def test_prose_written_from_the_verdict_is_accepted(kit):
    out = record(kit, what=f'Refine attempt: a 4 mm shorter trace with matching parts, simulated, gives -2.06 dB with the screen parts locked ({kit.rows["locked"]["code"]}) and -5.27 dB re-tuned '
                          f'({kit.rows["refine_free"]["code"]}). The improvement held, smaller than the screen showed, and the -10 dB target is not met.')
    assert out['status'] == 'created'


def test_a_finding_cannot_say_the_target_was_met_when_confirm_missed_it(kit):
    error = refused(kit, why_it_matters='The matching change met the target in the confirm run.')
    assert error.code == 'contradicts_confirm' and 'not met' in error.message
    record(kit, why_it_matters='The screen run reached the target, but the confirm run did not; the target is not met.')       # labelled as a screen result, with the confirm result beside it


def test_did_hold_is_refused_when_the_comparison_says_it_did_not(tmp_path, sample_board):
    kit = build_kit(tmp_path, sample_board, hold='not_held', outcome='not_held', held=False)
    error = refused(kit, what='The improvement held at confirm quality.')
    assert error.code == 'contradicts_confirm'
    record(kit, what='The screen improvement did not hold at confirm quality.')       # that one is what the verdict says


# ---- item 3: binding, ranges and counts ----------------------------------------------------------------------------------------------

def test_a_number_must_come_from_the_row_its_sentence_cites(kit):
    base, free = kit.rows['base']['code'], kit.rows['free']['code']
    error = refused(kit, what=f'At confirm the worst in-band S11 is -5.78 dB, simulated ({base}).')
    assert error.code == 'number_not_in_cited_evidence' and free in error.message and base in error.message
    record(kit, what=f'At confirm the worst in-band S11 is -5.78 dB, simulated ({free}).')


def make_rows(kit, values, key, targets_of=None):
    rows = []
    for i, value in enumerate(values):
        targets = targets_of(i) if targets_of else [f'ref:C{i + 1}']
        rows.append(kit.store.add_evidence(kit.review['id'], ev.CANDIDATE, 'analyzer', {'kind': 'measured', 'candidate_id': f'candidate:{key}:{i}', 'rule': key, 'title': f'{key} {i}',
                                                                                      'targets': targets, 'measurements': {'distance': {'value': value, 'unit': 'mm'}}}))
    return rows


DISTANCES = [5.72, 9.04, 9.05, 9.86, 10.76, 12.92, 16.29]


def test_a_range_must_match_the_smallest_and_largest_cited_value(kit):
    rows = make_rows(kit, DISTANCES, 'decoupling')
    ids = [r['code'] for r in rows]
    error = refused(kit, title='Decoupling capacitors sit 10 to 16 mm from the pins they serve', evidence_ids=ids)
    assert error.code == 'range_mismatch' and '5.72' in error.message and '16.29' in error.message
    out = record(kit, title='Decoupling capacitors sit 5.72 to 16.29 mm from the pins they serve', evidence_ids=ids)
    assert out['status'] == 'created'
    record(kit, title='Four capacitors are far from the pins', evidence_ids=ids, what=f'These four sit 9.04 to 10.76 mm from the pins ({ids[1]} to {ids[4]}).')    # a cited subset is judged on its own rows


def test_records_are_not_signals(kit):
    by_net = {}
    for t in kit.board.tracks:
        if t.net:
            by_net.setdefault(t.net, []).append(t)
    nets = [n for n, ts in by_net.items() if len(ts) >= 4][:3]
    chosen = [t for n in nets for t in by_net[n][:4]]
    rows = make_rows(kit, [1.0] * len(chosen), 'return_path', targets_of=lambda i: [chosen[i].id])
    ids = [r['code'] for r in rows]
    assert len({t.net for t in chosen}) == 3
    error = refused(kit, title=f'{len(chosen)} signals run over gaps in ground', evidence_ids=ids)
    assert error.code == 'count_mismatch' and '3 distinct nets' in error.message and 'gap crossings' in error.message
    record(kit, title=f'{len(chosen)} gap crossings on 3 nets run over gaps in ground', evidence_ids=ids)


def test_records_are_not_vias(kit):
    vias = kit.board.vias[:5]
    rows = make_rows(kit, [0.0] * 8, 'drc_hole_clearance', targets_of=lambda i: [vias[i % 5].id])
    error = refused(kit, title='8 vias sit on or against tracks', evidence_ids=[r['code'] for r in rows])
    assert error.code == 'count_mismatch' and '5 distinct vias' in error.message
    record(kit, title='5 vias sit on or against tracks, in 8 hole-clearance records', evidence_ids=[r['code'] for r in rows])


def test_an_unsupported_sentence_gets_two_retries_and_then_is_removed(kit):
    rows = make_rows(kit, DISTANCES, 'decoupling')
    ids = [r['code'] for r in rows]
    what = 'The capacitors sit 10 to 16 mm from the pins. The caps are in two rows.'
    for left in (MAX_CLAIM_TRIES, MAX_CLAIM_TRIES - 1):
        error = refused(kit, evidence_ids=ids, what=what)
        assert error.code == 'range_mismatch' and error.data['tries_left'] == left
    out = record(kit, evidence_ids=ids, what=what)
    assert '10 to 16' not in out['finding']['what'] and 'two rows' in out['finding']['what']
    assert any('removed a sentence' in n for n in out['prepared'].notes)


def test_citing_a_code_that_does_not_exist_is_refused(kit):
    assert refused(kit, what='The worst S11 is -0.5 dB (E-999), simulated.').code == 'unknown_evidence_citation'


# ---- item 1: stale screen conclusions ----------------------------------------------------------------------------------------------------

def test_the_verdict_is_read_from_the_confirm_comparisons(kit):
    rounds = verdict.rounds_from_rows(kit.store.list_evidence(kit.review['id']))
    assert [r['refine'] for r in rounds] == [False, True] and not any(r['meets_target'] for r in rounds) and all(r['held'] for r in rounds)


STALE_I1 = {'outcome': 'improved', 'finding': 'F-012', 'winner': 'R3',
            'summary': 'Screen results: the as-built antenna misses the target; shortening by 8 mm did not help; a matching change improved worst in-band S11 by 13.2 dB against the base and met the target. '
                       'Dielectric assumption not tested. Needs a confirm run.'}
STALE_S2 = 'Screen result: the as-built match gives worst in-band S11 near -1.3 dB. A matching change improved the in-band worst S11 by 13.2 dB against the base and met the -10 dB target on the screen run. Needs a confirm run.'
STALE_S3 = 'Not tested. The target was met by matching, so further optimising is not allowed. The assumed dielectric constant remains an unknown and the confirm run uses the same assumption.'


def stored_investigation(kit):
    confirmation = kit.rows['first']['values']['confirmation']
    refine = kit.rows['refine']['values']['confirmation']
    inv = kit.store.create_investigation(kit.review['id'], 'Does the antenna reach -10 dB?') if hasattr(kit.store, 'create_investigation') else None
    kit.store.update_investigation(inv['id'], status='closed', outcome={**STALE_I1, 'confirmation': confirmation, 'refine_rounds': [{'round': 1, 'confirmation': refine, 'result': 'target_not_met'}]})
    s1 = kit.store.add_suspect(inv['id'], 'Antenna length is off', status='ruled_out', data={'code': 'S1', 'experiments': [{'ref': 'R2', 'kind': 'whatif', 'ok': True, 'hash': 'a'}]})
    kit.store.update_suspect(s1['id'], conclusion={'status': 'ruled_out', 'reason': 'shortening by 8 mm made the match slightly worse, so length is not the lever.'})
    s2 = kit.store.add_suspect(inv['id'], 'Matching network is wrong', status='confirmed', data={'code': 'S2', 'experiments': [{'ref': 'R3', 'kind': 'matching', 'ok': True, 'hash': 'b'}]})
    kit.store.update_suspect(s2['id'], conclusion={'status': 'confirmed', 'reason': STALE_S2})
    s3 = kit.store.add_suspect(inv['id'], 'Assumed dielectric constant shifts tuning', status='inconclusive', data={'code': 'S3', 'experiments': []})
    kit.store.update_suspect(s3['id'], conclusion={'status': 'inconclusive', 'reason': STALE_S3})
    return inv


def entry_of(kit):
    stored_investigation(kit)
    return {s['code']: s for e in report._investigations(kit.store, kit.review['id']) for s in e['suspects']}, report._investigations(kit.store, kit.review['id'])[0]


def test_the_report_conclusion_follows_the_confirm_result(kit):
    suspects, inv = entry_of(kit)
    outcome = inv['outcome']
    assert 'met the target' not in outcome['summary'] and 'target is not met' in outcome['summary'] and kit.rows['base']['code'] in outcome['summary']       # I1
    assert 'met the target' in outcome['screen_summary'] and outcome['confirm_label'] == 'improved, target not met'                                      # kept as history
    assert 'met the' not in suspects['S2']['reason'] and 'confirmed' not in suspects['S2']['reason'].lower().replace('confirm run', '') and 'target is not met' in suspects['S2']['reason']   # S2
    assert suspects['S2']['screen_reason'] == STALE_S2
    assert 'further optimising is not allowed' not in suspects['S3']['reason'] and 'not ruled out' in suspects['S3']['reason'] and suspects['S3']['screen_reason'] == STALE_S3   # S3
    assert 'screen_reason' not in suspects['S1']                                                                                                         # untouched
    assert 'one point' in suspects['S1']['caveat'].lower() or '1 test point' in suspects['S1']['caveat']                                                   # I.4 flagged for old reviews


def test_the_html_report_labels_the_screen_text_as_history(kit):
    stored_investigation(kit)
    snapshot = {'investigations': report._investigations(kit.store, kit.review['id'])}
    text = ' '.join(f'{i["outcome"]["summary"]} {i["outcome"].get("screen_summary", "")} ' + ' '.join(s['reason'] or '' for s in i['suspects']) for i in snapshot['investigations'])
    assert 'target is not met' in text
    assert all(restate.SCREEN_HISTORY not in (s['reason'] or '') for i in snapshot['investigations'] for s in i['suspects'])


def test_restating_is_idempotent_and_does_nothing_when_confirm_met_the_target(kit):
    _, inv = entry_of(kit)
    again = restate.restate_entry(inv, TARGET)
    assert again['outcome']['summary'] == inv['outcome']['summary'] and again['outcome']['screen_summary'] == inv['outcome']['screen_summary']
    met = {**inv, 'confirmation': {**inv['confirmation'], 'meets_target': True}, 'refine_rounds': []}
    met['outcome'] = {**STALE_I1, 'confirmation': met['confirmation']}
    assert restate.restate_entry(met, TARGET) is met


def test_the_screen_phase_cannot_call_a_screen_result_final(kit):
    for text in ('Not tested. The target was met by matching, so further optimising is not allowed.', 'The matching change met the -10 dB target.'):
        found = verdict.problems({'reasoning': text}, [])
        assert found and found[0].code == 'screen_result_called_final'
    assert not verdict.problems({'reasoning': 'On the screen run the matching change reached the -10 dB target (comparison only; a confirm run is needed).'}, [])


# ---- the summary says simulated ----------------------------------------------------------------------------------------------------------

def test_a_summary_paragraph_with_s11_must_say_simulated():
    text = 'At confirm quality the worst in-band S11 is -0.5 dB against the -10 dB target, and worst accepted power is 10.9 %.'
    assert 'simulated' in restate.simulated_problem({'summary': text})
    assert restate.simulated_problem({'summary': text.replace('At confirm quality', 'In simulation at confirm quality')}) is None
    assert restate.simulated_problem({'summary': 'Two shorts sit near U3.\n\n' + text + ' This is simulated.'}) is None


# ---- item 4: part roles ------------------------------------------------------------------------------------------------------------------

def test_brief_roles_that_contradict_the_symbol_are_found(sample_board):
    wrong = roles.conflicts(sample_board, {'U1': 'regulator', 'U2': 'sensor', 'U3': 'radio', 'U4': 'sensor', 'C5': 'decoupling'})
    assert {w['ref']: w['suggested'] for w in wrong} == {'U1': 'protection', 'U2': 'power'} or {w['ref'] for w in wrong} == {'U1', 'U2'}
    assert 'USBLC6-2SC6' in wrong[0]['message']


async def test_the_brief_is_refused_twice_then_corrected(env):
    brief = {'tool': 'submit_board_brief', 'args': {'purpose': 'Battery powered Bluetooth sensor node', 'part_roles': {'U3': 'radio', 'U1': 'regulator', 'U2': 'sensor'}, 'risks': [], 'priorities': ['antenna']}}
    review = await env.review([brief, brief, brief, {'tool': 'advance_phase'}])
    results = [e['payload']['content'] for e in env.events(review['chat_id'], 'tool_result') if e['payload']['name'] == 'submit_board_brief']
    assert 'role_contradicts_symbol' in results[0] and 'USBLC6-2SC6' in results[0] and 'role_contradicts_symbol' in results[1]
    assert '"saved":true' in results[2] and 'roles_corrected' in results[2]
    saved = env.store.get_review(review['id'])['summary']['brief']['part_roles']
    assert saved['U1'] != 'regulator' and saved['U2'] != 'sensor' and saved['U3'] == 'radio'
    assert any(l['rule'] == 'part_roles_corrected' for l in env.store.get_review(review['id'])['summary']['limits'])


# ---- item 5: a suspect is ruled out only by two points or a trend -------------------------------------------------------------------------

OPEN = {'tool': 'open_investigation', 'args': {'question': 'Is the length the lever?', 'suspects': [{'title': 'Antenna length', 'why': 'A length error shifts the resonance.'},
                                                                                              {'title': 'Matching network', 'why': 'Parts are off.'}]}}
BRIEF = {'tool': 'submit_board_brief', 'args': {'purpose': 'Battery powered Bluetooth sensor node', 'part_roles': {'U3': 'radio'}, 'risks': [], 'priorities': ['antenna']}}
PRE = [BRIEF, {'tool': 'advance_phase'}] + clear() + [{'tool': 'advance_phase'}, OPEN, {'tool': 'run_simulation', 'args': {'suspect_id': 'S1', 'label': 'base'}}]


def cut(mm):
    return {'tool': 'run_simulation', 'args': {'suspect_id': 'S1', 'label': f'length {mm}', 'whatif_ops': [{'op': 'adjust_antenna_length', 'delta_mm': mm}]}}


def conclude(reasoning):
    return {'tool': 'conclude_suspect', 'args': {'suspect_id': 'S1', 'verdict': 'ruled_out', 'reasoning': reasoning}}


async def test_ruling_out_from_one_point_is_refused_and_two_points_are_accepted(env):
    one = await env.review(PRE + [cut(-8.0), conclude('Length is not the lever.')])
    result = [e['payload']['content'] for e in env.events(one['chat_id'], 'tool_result') if e['payload']['name'] == 'conclude_suspect'][0]
    assert 'ruled_out_needs_two_points' in result and '1 trusted test point' in result
    two = await env.review(PRE + [cut(-8.0), cut(12.0), conclude('Neither length point improved the in-band result against the base.')])
    result = [e['payload']['content'] for e in env.events(two['chat_id'], 'tool_result') if e['payload']['name'] == 'conclude_suspect'][0]
    assert '"status":"ruled_out"' in result


# ---- items 1 and 2 in a live review: the conclusions follow the confirm result as it happens ---------------------------------------------------

async def test_a_live_review_rewrites_the_screen_conclusions_after_confirm(env, base_model):
    import json
    from pathlib import Path

    from test_refine import antenna
    from test_partial_win import DECLINE, first_steps

    env.lab.model = antenna(base_model)
    env.lab.match_gain['confirm_free'] = 5.0
    review = await env.review(first_steps() + [DECLINE], depth='deep')
    inv = env.store.list_investigations(review['id'])[0]
    outcome = inv['outcome']
    assert outcome['confirm_label'] == 'improved, target not met' and outcome['screen_summary'] and 'target is not met' in outcome['summary']
    results = [json.loads(e['payload']['content']) for e in env.events(review['chat_id'], 'tool_result') if e['payload']['name'] == 'confirm_result']
    last = [r for r in results if 'verdict' in r][-1]['verdict']
    assert last['meets_target'] is False and last['outcome'] == 'improved_short_of_target' and last['rounds'][-1]['evidence']
    snapshot_suspects = {s['code']: s for e in report._investigations(env.store, review['id']) for s in e['suspects']}
    assert snapshot_suspects                                                        # the report builder runs on the live data without error


def test_analyzer_statistics_explain_ranges_and_counts(kit):
    stats = {'class_summary': {'pin_class': 'DEC', 'count': 7, 'min_mm': 5.72, 'max_mm': 16.29}, 'rule_summary': {'gap_events': 41, 'nets_with_gaps': 9}}
    rows = [kit.store.add_evidence(kit.review['id'], ev.CANDIDATE, 'analyzer', {'kind': 'measured', 'candidate_id': f'candidate:decoupling:{i}', 'rule': 'decoupling', 'title': f'cap {i}',
                                                                                'targets': [f'ref:C{i + 1}'], 'measurements': {'distance': {'value': d, 'unit': 'mm'}}, 'statistics': stats})
            for i, d in enumerate(DISTANCES)]
    ids = [r['code'] for r in rows]
    out = record(kit, evidence_ids=ids, title='The DEC capacitors sit 5.72 to 16.29 mm from their pins', what='There are 41 gap events on 9 nets.')
    assert out['status'] == 'created'
    assert refused(kit, evidence_ids=ids, title='The DEC capacitors sit 10 to 16 mm from their pins').code == 'range_mismatch'
