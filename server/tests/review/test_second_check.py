"""Regression tests for the second independent fact-check of two reviews (an nRF52840 node and a no-radio USB LDO board).

Each wrong statement below is an exact quote from that check; the candidate and evidence rows carry the values it measured.
"""
import asyncio
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from gate_steps import clear

from trustpcb import prompts
from trustpcb.review import classwidth, lint, restate
from trustpcb.review import evidence as ev
from trustpcb.review.findings import FindingEngine, FindingRejected, fixed_dip_caveat, untested_line
from trustpcb.review.investigate import InvestigationMixin
from trustpcb.review.tools import dumps
from trustpcb.storage import Store

ROOT = Path(__file__).resolve().parents[3]
BRIEF = {'tool': 'submit_board_brief', 'args': {'purpose': 'Battery powered Bluetooth sensor node with a printed antenna', 'part_roles': {'U3': 'radio'}, 'risks': [],
                                               'priorities': ['antenna']}}
TO_REVIEW = [BRIEF, {'tool': 'advance_phase'}]
GOOD = dict(title='Power tracks take long detours', severity='medium', what='VBUS_RAW is routed far longer than the straight line.',
            why_it_matters='A long path adds resistance between the connector and the fuse.', recommendation='Route it more directly in your own tool.')


def contents(env, review, name):
    return [json.loads(e['payload']['content']) for e in env.events(review['chat_id'], 'tool_result') if e['payload']['name'] == name]


# ---- the exact RF class candidate of the check (ANT_CHIP, E-060) ------------------------------------------------------------------


def netclass_candidate(**over):
    base = {'id': 'candidate:netclass_width:1', 'rule': 'netclass_width', 'source': 'analyzer', 'severity_hint': 'low', 'net': 'ANT_CHIP',
            'title': 'ANT_CHIP is routed 0.15, 0.3 mm wide; its net class RF asks 3.0548 mm',
            'measurements': {'class_track_width': {'value': 3.0548, 'unit': 'mm'}, 'narrowest_width': {'value': 0.15, 'unit': 'mm'}, 'widest_narrow_width': {'value': 0.3, 'unit': 'mm'},
                             'narrow_length': {'value': 9.1, 'unit': 'mm'}, 'net_length': {'value': 9.1, 'unit': 'mm'}, 'class_width_z0_estimate': {'value': 47.8, 'unit': 'ohm'},
                             'width_for_target': {'value': 2.835, 'unit': 'mm'}},
            'targets': ['track:0806707e', 'track:4d9f9952'], 'marks': [], 'net_class': {'name': 'RF', 'track_width_mm': 3.0548}}
    return {**base, **over}


def impedance_candidate():
    return {'id': 'candidate:impedance:1', 'rule': 'impedance', 'source': 'analyzer', 'severity_hint': 'low', 'title': 'ANT_CHIP section estimates 123 ohm',
            'measurements': {'estimated_z0': {'value': 123.0, 'unit': 'ohm'}, 'width': {'value': 0.3, 'unit': 'mm'}, 'target': {'value': 50.0, 'unit': 'ohm'}},
            'targets': ['track:0806707e'], 'marks': []}


REFUSED_REASON = ('The RF class asks 3.0548 mm, impossible for a 0.3 mm feed to a small pad; that is a class setting artifact. The feed impedance is covered in the impedance finding.')


def test_an_rf_class_near_the_target_is_the_designers_intent():
    facts = classwidth.intent_facts(netclass_candidate())
    assert facts and facts['class_width'] == 3.0548 and facts['z0'] == 47.8 and facts['need'] == 2.835 and facts['target'] == 50.0
    # a class far from the target, a class that is not RF (no estimate) and another rule are not
    assert classwidth.intent_facts(netclass_candidate(measurements={**netclass_candidate()['measurements'], 'class_width_z0_estimate': {'value': 58.0, 'unit': 'ohm'}})) is None
    assert classwidth.intent_facts(netclass_candidate(measurements={'class_track_width': {'value': 0.5, 'unit': 'mm'}})) is None
    assert classwidth.intent_facts(netclass_candidate(rule='impedance')) is None
    # the analyzer's own target is used when it is there
    assert classwidth.intent_facts(netclass_candidate(measurements={**netclass_candidate()['measurements'], 'target_impedance': {'value': 75.0, 'unit': 'ohm'}})) is None


def test_the_refusal_names_the_class_the_estimate_and_the_width_for_the_target():
    message = classwidth.refusal([classwidth.intent_facts(netclass_candidate())], 'F-003')
    for part in ('3.0548 mm', '47.8 ohm', '2.835 mm', 'class setting artifact', 'F-003', 'assumed stackup', '0.15 to 0.3 mm'):
        assert part in message


def test_the_finding_text_gets_the_width_a_taper_and_the_stackup_condition():
    out = classwidth.carry({'what': 'The feed is narrow.', 'recommendation': 'Check S11.'}, [classwidth.intent_facts(netclass_candidate(stackup_assumptions={'epsilon_r': 4.5, 'dielectric_height_mm': 1.53}))])
    assert '3.0548 mm' in out['what'] and '47.8 ohm' in out['what'] and '2.835 mm would give 50 ohm' in out['what'] and 'er 4.5, dielectric 1.53 mm' in out['what']
    rec = out['recommendation']
    assert rec.startswith('Check S11.') and 'cannot connect straight to a fine-pitch pad' in rec and 'taper' in rec and 'thinner dielectric' in rec and 'assumed stackup' in rec
    assert classwidth.carry(out, [classwidth.intent_facts(netclass_candidate())]) is out         # a text that has it is left alone


async def test_dismissing_the_rf_class_is_refused_and_the_impedance_finding_carries_the_width(env):
    env.lab.candidates = env.lab.candidates + [netclass_candidate(), impedance_candidate()]
    impedance = {'tool': 'record_finding', 'args': {'title': 'The RF feed is far above 50 ohm (estimate)', 'severity': 'low', 'rule_group': 'impedance', 'what': 'The ANT_CHIP section is estimated at 123 ohm.',
                                                    'why_it_matters': 'A feed this far from 50 ohm mismatches the antenna.', 'recommendation': 'Confirm the real stackup first.'}}
    steps = TO_REVIEW + [{'tool': 'dismiss_rule_group', 'args': {'rule': 'netclass_width', 'reason': REFUSED_REASON}}, impedance,
                         {'tool': 'dismiss_candidate', 'args': {'candidate_ids': ['candidate:netclass_width:1'], 'reason': REFUSED_REASON}},
                         {'tool': 'record_finding', 'args': {'update': 'F-001', 'candidate_ids': ['candidate:netclass_width:1'], 'title': 'The RF feed is far above 50 ohm (estimate)', 'severity': 'low',
                                                             'what': 'The ANT_CHIP section is estimated at 123 ohm.', 'why_it_matters': 'A feed this far from 50 ohm mismatches the antenna.',
                                                             'recommendation': 'Confirm the real stackup first.'}}] + clear()
    review = await env.review(steps)
    group = contents(env, review, 'dismiss_rule_group')[0]
    assert group['error'] == 'class_width_is_intent' and 'class setting artifact' in group['message'] and group['width_for_target_mm'] == 2.835
    refused_single = contents(env, review, 'dismiss_candidate')[0]
    assert refused_single['error'] == 'class_width_is_intent' and 'F-001' in refused_single['message']       # steered to the finding that already covers the impedance group
    finding = env.store.get_finding_by_code(review['id'], 'F-001')
    assert '2.835 mm would give 50 ohm' in finding['what'] and 'taper' in finding['recommendation'] and 'assumed stackup' in finding['recommendation']
    states = {r['candidate_id']: r['review_state'] for r in env.store.list_candidates(review['id'])}
    assert states['candidate:netclass_width:1'] == 'recorded'


async def test_a_power_class_netclass_result_can_still_be_dismissed(env):
    power = netclass_candidate(id='candidate:netclass_width:2', net='VBUS_RAW', title='VBUS_RAW is routed 0.25 mm wide; its net class POWER asks 0.5 mm',
                               measurements={'class_track_width': {'value': 0.5, 'unit': 'mm'}, 'narrowest_width': {'value': 0.25, 'unit': 'mm'}, 'widest_narrow_width': {'value': 0.25, 'unit': 'mm'}},
                               net_class={'name': 'POWER'})
    env.lab.candidates = env.lab.candidates + [power]
    review = await env.review(TO_REVIEW + [{'tool': 'dismiss_rule_group', 'args': {'rule': 'netclass_width', 'reason': 'VBUS_RAW at 0.25 mm is covered by the detour finding.'}}] + clear())
    assert contents(env, review, 'dismiss_rule_group')[0].get('dismissed_rule') == 'netclass_width'


# ---- the schematic state tool and parity facts (no-radio review) -------------------------------------------------------------------


async def test_read_schematic_state_lists_no_connect_markers_dnp_flags_and_nets(env):
    steps = [{'tool': 'read_schematic', 'args': {'kind': 'state', 'refs': ['J1', 'C19', 'U2', 'Z99']}}, BRIEF]
    review = await env.review(steps)
    state = contents(env, review, 'read_schematic')[0]
    parts = {p['ref']: p for p in state['parts']}
    assert state['schematic'] == 'nrf52840_rf_iot_node.kicad_sch' and state['dnp_parts_in_schematic'] == ['C19', 'C20']
    assert parts['C19']['dnp_in_schematic'] is True and parts['U2']['dnp_in_schematic'] is False
    assert {'A8', 'B8'} <= set(parts['J1']['pins_with_no_connect_marker'])
    assert parts['U2']['pins_declared_no_connect_by_symbol'] == ['4'] and {p['pin']: p['net'] for p in parts['U2']['pins']}['1'] == 'VBUS_F'
    assert parts['Z99']['error']


def test_parity_candidates_reach_the_agent_with_what_the_schematic_has(tmp_path):
    """BaselineLab.list_candidates adds the schematic facts to parity records of a stored version."""
    from trustpcb.review.lab import BaselineLab

    sample = ROOT / 'samples' / 'usb-ldo-led'
    if not (sample / 'usb_ldo_led_node.kicad_pcb').is_file():
        pytest.skip('the usb-ldo-led sample is not in this checkout')
    store = Store(tmp_path / 'data')
    version = store.create_version(store.create_project('p')['id'])
    vdir = Path(store.version_dir(version['id']))
    shutil.copytree(sample, vdir / 'project')
    (vdir / 'artifacts').mkdir(exist_ok=True)
    record = {'id': 'candidate:parity_net_conflict:1', 'rule': 'parity_net_conflict', 'source': 'kicad', 'title': 'Pad missing net given by schematic (unconnected-(J1-D+-PadA6))',
              'measurements': {}, 'targets': ['pad:J1:A6'], 'severity_hint': 'info', 'details': ['Pad A6 [<no net>] of J1 on F.Cu']}
    (vdir / 'artifacts' / 'candidates.json').write_text(json.dumps([record]))
    found = asyncio.run(BaselineLab(store).list_candidates(version_id=version['id']))
    parity = next(c for c in found if c['rule'] == 'parity_net_conflict')
    assert 'already has a no_connect marker on J1 pin A6' in parity['schematic_state']['text'] and parity['details'][-1] == parity['schematic_state']['text']
    evidence = ev.candidate_values(parity)
    assert evidence['schematic_state']['no_connect'][0] == {'ref': 'J1', 'pin': 'A6', 'by': 'marker'}
    from trustpcb.review.handlers import Handlers
    assert Handlers.candidate_card(None, parity)['schematic'].startswith('The schematic already has a no_connect marker')


# ---- 4a. the stale reason of S1 ------------------------------------------------------------------------------------------------------


S1_REASON = ('One point tested: shortening by 5 mm made the worst in-band S11 about 0.5 dB worse than the base on screen runs (R2 vs R1). The base is inductive at band centre, '
             'but a second point (lengthening) was not run because matching already reached the target on the screen run.')
CONFIRM = {'outcome': 'improved_short_of_target', 'hold': 'held_smaller', 'base': {'band_worst_s11_db': -0.5, 'evidence': 'E-203'}, 'best': {'band_worst_s11_db': -5.78, 'evidence': 'E-205'},
           'best_gain_db': 5.28, 'base_evidence': 'E-203'}


def entry(reason, experiments):
    return {'outcome': {'winner': 'R3', 'summary': 'screen text'}, 'suspects': [{'code': 'S1', 'title': 'Antenna length detuned', 'status': 'inconclusive', 'reason': reason, 'experiments': experiments}]}


def test_s1_keeps_what_was_tested_and_drops_the_screen_reason_for_skipping():
    out = restate.restate_entry(entry(S1_REASON, [{'ref': 'R2', 'kind': 'whatif', 'ok': True, 'hash': 'a'}]), -10.0, [CONFIRM])
    s1 = out['suspects'][0]
    assert 'One point tested' in s1['reason'] and 'a second point (lengthening) was not run.' in s1['reason']
    assert 'because matching already reached the target' not in s1['reason'] and 'did not survive the confirm run' in s1['reason']
    assert 'simulated short of the -10 dB target' in s1['reason'] and '-5.78 dB, E-205' in s1['reason'] and 'untested points stay open' in s1['reason']
    assert s1['screen_reason'] == S1_REASON and s1['status'] == 'inconclusive'


def test_a_suspect_never_tested_still_gets_the_generic_restatement_and_a_clean_reason_is_left_alone():
    generic = restate.restate_entry(entry('Skipped because the target was met on the screen run.', []), -10.0, [CONFIRM])['suspects'][0]['reason']
    assert generic.startswith('Not settled.') and 'was not tested and is not ruled out' in generic
    clean = 'One point tested: shortening by 5 mm made the worst in-band S11 about 0.5 dB worse (R2 vs R1).'
    assert restate.restate_entry(entry(clean, [{'ref': 'R2', 'kind': 'whatif', 'ok': True}]), -10.0, [CONFIRM])['suspects'][0]['reason'] == clean


# ---- wording checks, refused once ---------------------------------------------------------------------------------------------------


@pytest.fixture
def kit(tmp_path, sample_board):
    store = Store(tmp_path / 'data')
    version = store.create_version(store.create_project('p')['id'])
    review = store.create_review(version['id'], goal={'target_s11_db': -10.0, 'band_hz': [2.4e9, 2.4835e9]})
    engine = FindingEngine(store, review['id'], sample_board, lambda: ev.context_quantities({'target_s11_db': -10.0, 'band_hz': [2.4e9, 2.4835e9]}, [], sample_board, None))
    row = store.add_evidence(review['id'], ev.CANDIDATE, 'analyzer', {'kind': 'measured', 'candidate_id': 'candidate:track_detour:1', 'rule': 'track_detour', 'title': 'VBUS_RAW detour',
                                                                      'targets': ['net:VBUS_RAW'], 'measurements': {'extra_length': {'value': 31.35, 'unit': 'mm'}}})
    return SimpleNamespace(store=store, review=review, engine=engine, row=row, board=sample_board)


def record(kit, **args):
    args.setdefault('evidence_ids', [kit.row['code']])
    args.setdefault('scope', 'board')
    return kit.engine.record({**GOOD, **args}, candidates=args.pop('candidates', {}))


def refused(kit, **args):
    with pytest.raises(FindingRejected) as info:
        record(kit, **args)
    return info.value


F009 = 'A long, narrow VBUS_RAW path adds resistance and inductance between the USB-C input and the regulator, and also feeds the long decoupling loops already found.'


def test_an_unsupported_causal_sentence_is_refused_once_with_a_clear_message_then_kept_as_not_tested(kit):
    error = refused(kit, why_it_matters=F009)
    assert error.code == 'causal_claim_unsupported' and 'feeds' in error.message and kit.row['code'] in error.message and 'refuses this once' in error.message
    assert error.data['refused_once'] is True and error.data['field'] == 'why_it_matters'
    out = record(kit, why_it_matters=F009)                                  # the same sentence again is not refused: no jam
    assert out['status'] == 'created'
    assert any(t.startswith('Not backed by cited evidence: "A long, narrow VBUS_RAW path') for t in out['finding']['not_tested'])
    assert F009 in out['finding']['why_it_matters']


def test_a_cited_hedged_or_wired_causal_sentence_is_not_refused(kit):
    code = kit.row['code']
    for sentence in (f'The long path also feeds the decoupling loops ({code}).',
                     'The long path may feed the decoupling loops.',
                     'The long path is likely to hurt the supply, and the loops affect the radio.',
                     'The AP2112K-3.3 feeds only an LED, so load steps are small.',
                     'Keep the antenna feed short and clear of the keep-out.',
                     'Nothing here causes a short.'):
        assert record(kit, why_it_matters=sentence)['status'] in ('created', 'grouped'), sentence       # accepted (the same issue groups with the first)


def test_the_unhedged_clause_is_judged_alone(kit):
    f008 = 'USB and the crystal lines are likely to radiate and pick up noise, and ANT_CHIP crossing a gap affects the antenna feed.'
    error = refused(kit, why_it_matters=f008)
    assert error.code == 'causal_claim_unsupported' and 'affects' in error.message


C19 = 'C19 is marked DNP as a tuning slot.'


def test_one_dnp_part_of_several_and_a_guessed_purpose_are_both_named(kit):
    problems = lint.dnp_problems({'what': C19}, kit.board)
    assert {p.code for p in problems} == {'dnp_incomplete', 'dnp_intent_claim'}
    incomplete = next(p for p in problems if p.code == 'dnp_incomplete')
    assert incomplete.data == {'named': ['C19'], 'missing': ['C20']} and 'C19 and C20' in incomplete.message
    intent = next(p for p in problems if p.code == 'dnp_intent_claim')
    assert 'tuning slot' in intent.message and 'not why' in intent.message
    assert lint.dnp_problems({'what': 'C19 and C20 are marked DNP, so the pads are open stubs.'}, kit.board) == []
    assert lint.dnp_problems({'what': 'All the DNP parts are listed in the brief.'}, kit.board) == []


def test_the_dnp_wording_is_refused_once_in_a_finding(kit):
    error = refused(kit, what=C19)
    assert error.code in ('dnp_incomplete', 'dnp_intent_claim') and error.data['refused_once'] is True
    record(kit, what=C19)                                      # sent again, it stays


async def test_the_brief_and_a_dismissal_reason_get_the_same_one_time_refusal(env):
    risky = {'tool': 'submit_board_brief', 'args': {'purpose': 'Battery powered Bluetooth sensor node with a printed antenna', 'part_roles': {'U3': 'radio'},
                                                    'risks': [{'text': C19, 'targets': ['ref:C19']}], 'priorities': ['antenna']}}
    review = await env.review([risky, risky, {'tool': 'advance_phase'}] + clear())
    first, second = contents(env, review, 'submit_board_brief')
    assert first['error'] in ('dnp_incomplete', 'dnp_intent_claim') and 'refuses this once' in first['message'] and second.get('saved') is True


async def test_a_dismissal_reason_with_a_guessed_purpose_is_refused_once(env):
    reason = 'Both C19 and C20 are marked DNP as tuning slots for the antenna match, which is harmless here.'
    review = await env.review(TO_REVIEW + [{'tool': 'dismiss_rule_group', 'args': {'rule': 'track_angle', 'reason': reason}}, {'tool': 'dismiss_rule_group', 'args': {'rule': 'track_angle', 'reason': reason}}] + clear())
    first, second = contents(env, review, 'dismiss_rule_group')[:2]
    assert first['error'] == 'dnp_intent_claim' and second.get('dismissed_rule') == 'track_angle'


F004 = 'Keep the feed width constant and as short as possible. Check S11 across the band by simulation, using L1 and the C19/C20 pads for matching.'


def test_keep_the_width_next_to_a_width_for_the_target_is_refused_once(kit):
    kit.store.add_evidence(kit.review['id'], ev.CANDIDATE, 'analyzer', {'kind': 'measured', 'candidate_id': 'candidate:impedance:1', 'rule': 'impedance', 'title': 'ANT_CHIP 123 ohm', 'targets': ['net:ANT_CHIP'],
                                                                          'measurements': {'width_for_target': {'value': 2.835, 'unit': 'mm'}}})
    error = refused(kit, recommendation=F004)
    assert error.code == 'width_advice_conflict' and '2.835 mm' in error.message and 'uniform at the width chosen' in error.message
    assert record(kit, recommendation=F004)['status'] == 'created'
    assert lint.width_conflict_problems({'recommendation': 'Keep the width uniform at the width chosen for the target impedance.'}, []) == []


# ---- 6. severity ---------------------------------------------------------------------------------------------------------------------


def impedance_hint(severity='medium'):
    return {'candidate:impedance:3': {'id': 'candidate:impedance:3', 'rule': 'impedance', 'title': 'ANT_FEED estimates 123 ohm', 'source': 'analyzer', 'severity_hint': severity,
                                      'measurements': {'z0': {'value': 123.0, 'unit': 'ohm'}}, 'targets': ['ref:C13'], 'marks': []}}


def test_a_severity_other_than_the_analyzers_hint_needs_a_reason_and_is_never_blocked(kit):
    known = impedance_hint('medium')
    args = dict(severity='high', candidate_ids=['candidate:impedance:3'], evidence_ids=[], scope='targets', candidates=known)
    error = refused(kit, **args)
    assert error.code == 'severity_without_reason' and 'worst hint' in error.message and 'medium' in error.message and error.data == {**error.data, 'usual': 'medium', 'chosen': 'high'}
    reason = 'The 2.4 GHz feed is the radio path, and 123 ohm is more than twice the target.'
    out = record(kit, **args, severity_reason=reason)
    assert out['finding']['severity'] == 'high' and f"Severity high, the analyzers' worst hint is medium: {reason}" in out['finding']['assumptions']


def test_the_analyzers_hint_needs_no_reason_and_a_lower_severity_does(kit):
    known = impedance_hint('medium')
    assert record(kit, severity='medium', candidate_ids=['candidate:impedance:3'], evidence_ids=[], scope='targets', candidates=known)['finding']['assumptions'] == []
    lower = refused(kit, severity='low', candidate_ids=['candidate:impedance:3'], evidence_ids=[], scope='targets', candidates=known, title='Another title')
    assert lower.code == 'severity_without_reason' and lower.data['chosen'] == 'low'


def test_an_update_that_leaves_the_severity_alone_is_not_questioned(kit):
    known = impedance_hint('medium')
    created = record(kit, severity='high', candidate_ids=['candidate:impedance:3'], evidence_ids=[], scope='targets', candidates=known,
                     severity_reason='The 2.4 GHz feed is the radio path and the estimate is far off.')
    code = created['finding']['code']
    out = kit.engine.record({'update': code, 'what': 'The feed is estimated at 123 ohm against 50.'}, candidates=known)
    assert out['status'] == 'updated' and out['finding']['severity'] == 'high'


# ---- 8. simulation caveat, not tested, counts --------------------------------------------------------------------------------------------


def sim_row(store, review, ref, label, dips, quality='confirm'):
    values = {'kind': 'simulated', 'quality': quality, 'trust': {'status': 'ok', 'reasons': []}, 'label': label, 'ref': ref,
              'measurements': {'s11_band_worst': {'value': -1.3, 'unit': 'dB'}, **{f'fixed_dip_{i}': {'value': mhz, 'unit': 'MHz'} for i, mhz in enumerate(dips, 1)}}}
    return store.add_evidence(review['id'], ev.SIMULATION, 'run_simulation', values)


def test_the_antenna_finding_states_that_the_dips_do_not_track_length(kit):
    row = sim_row(kit.store, kit.review, 'R2', 'shorten 4mm', [2253.7])
    sim_row(kit.store, kit.review, 'R3', 'shorten 8mm', [2253.7])
    out = record(kit, evidence_ids=[row['code']], title='Antenna length was tested at one point', what='The simulated antenna has a worst in-band S11 of -1.3 dB.')
    caveat = next(t for t in out['finding']['not_tested'] if 'do not track antenna length' in t)
    assert '2253.7, 2253.7 MHz' in caveat and 'shorten 4mm' in caveat and 'not established' in caveat
    assert fixed_dip_caveat(kit.engine.evidence_rows(), []) is None                    # a finding that does not stand on a simulation says nothing about it


def test_no_caveat_without_fixed_dip_data(kit):
    row = sim_row(kit.store, kit.review, 'R2', 'shorten 5mm', [])
    out = record(kit, evidence_ids=[row['code']], title='One length point', what='The simulated antenna has a worst in-band S11 of -1.3 dB.')
    assert not any('track antenna length' in t for t in out['finding']['not_tested'])


def test_not_tested_holds_what_is_untested_not_the_result_sentence():
    result = 'The improvement held at confirm quality but the screen overstated it, and the tuning target is not met. This is a partial win: the best confirm-quality parts are in the finding.'
    line = untested_line(result)
    assert 'partial win' not in line and 'held' not in line and 'not relied on' in line and 'built board were not tested' in line
    assert untested_line('Refine round: ' + result).startswith('Refine round: Screen-quality numbers are not relied on')
    assert untested_line('The screen improvement did not hold at confirm quality.') == 'The screen improvement did not hold at confirm quality.'


class _Summary(InvestigationMixin):
    def __init__(self, candidates):
        self.s = SimpleNamespace(candidates=candidates)


def test_a_count_of_all_results_must_match_the_review():
    candidates = {f'c{i}': {'source': 'analyzer' if i < 16 else 'kicad'} for i in range(41)}
    problem = _Summary(candidates).result_count_problem({'summary': 'I reviewed all 37 analyzer results and found no serious fault.'})
    assert problem and '41 results in all' in problem and '16 from the analyzers' in problem and '25 from KiCad' in problem
    for fine in ('I reviewed all 41 results.', 'I reviewed all 25 KiCad results.', 'Three results were reviewed.'):
        assert _Summary(candidates).result_count_problem({'summary': fine}) is None


# ---- the prompts -------------------------------------------------------------------------------------------------------------------------


def test_phase_briefs_fit_the_tool_result_cap_with_the_new_rules():
    values = dict(phase2_calls=20, phase3_calls=70, phase4_calls=40, phase5_calls=20, images=12, region_views=3, layout_findings=2, layout_reason_chars=60, investigations=2, experiments=6,
                  confirm_runs=2, refine_rounds=1, refine_screens=3, refine_matching=2, target_db=-10, band='2.4 to 2.4835 GHz', wall_minutes=45, depth='standard')
    for phase in (2, 3, 4, 5, 6):
        result = dumps({'phase': phase, 'name': 'review', 'skipped': [], 'brief': prompts.phase_brief(phase, **values), 'unreviewed_candidates': 3, 'auto_grouped_info_groups': [{'rule': 'parity', 'count': 25}]})
        assert len(result) < 8000, (phase, len(result))
    three = prompts.phase_brief(3, **values)
    assert 'kind `state`' in three and 'pin name' in three and 'put it in the impedance finding' in three and 'severity_reason' in three
    system = prompts.system_prompt(depth='standard', wall_minutes=45, region_views=3, layout_findings=2)
    for text in ('pin name the candidate gives', 'already has', 'do not say why they are unfitted', 'not a class setting artifact', 'Give one width plan',
                 'needs evidence', 'severity_reason', 'own bus pull-ups', 'not a plug size', 'all 37 results'):
        assert text in system, text
