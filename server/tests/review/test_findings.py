import pytest

from trustpcb.review import evidence as ev
from trustpcb.review.findings import FindingEngine, FindingRejected, edit_claims
from trustpcb.review.fingerprint import TargetIndex, fingerprint
from trustpcb.storage import Store

GOOD = dict(title='Decoupling capacitor C13 is far from U3', severity='medium', what='C13 is 5.2 mm from the U3 supply pin.',
            why_it_matters='A long loop weakens decoupling.', recommendation='Move C13 closer to the U3 supply pin.')


@pytest.fixture
def kit(tmp_path, sample_board):
    store = Store(tmp_path / 'data')
    project = store.create_project('p')
    version = store.create_version(project['id'])
    review = store.create_review(version['id'], goal={'target_s11_db': -10})
    engine = FindingEngine(store, review['id'], sample_board, lambda: ev.context_quantities({'target_s11_db': -10, 'band_hz': [2.4e9, 2.4835e9]}, [], sample_board, None))
    cand = {'id': 'candidate:decoupling:1', 'rule': 'decoupling', 'title': 'C13 far', 'source': 'analyzer', 'targets': ['ref:C13', 'ref:U3'],
            'measurements': {'distance': {'value': 5.2, 'unit': 'mm'}}}
    return type('Kit', (), {'store': store, 'review': review, 'engine': engine, 'cand': cand, 'candidates': {cand['id']: cand}, 'board': sample_board})


def sim(kit, *, quality='screen', trust='ok', measurements=None, **extra):
    values = {'kind': 'simulated', 'quality': quality, 'trust': {'status': trust, 'reasons': []}, 'measurements': measurements or {
        'resonance': {'value': 2447.0, 'unit': 'MHz'}, 'delta_s11': {'value': -9.3, 'unit': 'dB'}}, **extra}
    return kit.store.add_evidence(kit.review['id'], ev.SIMULATION if quality == 'screen' else ev.CONFIRM, 'run_simulation', values)


def record(kit, **args):
    return kit.engine.record({**GOOD, **args}, candidates=kit.candidates)


def reject(kit, **args):
    with pytest.raises(FindingRejected) as info:
        record(kit, **args)
    return info.value


def test_class_comes_from_the_evidence(kit):
    assert record(kit, candidate_ids=['candidate:decoupling:1'])['finding']['class'] == 'measured'
    row = sim(kit, quality='confirm')
    out = record(kit, title='Copper detunes the antenna', what='The antenna resonates at 2447 MHz.', evidence_ids=[row['code']], targets=['net:ANT_CHIP'], rule='antenna_detune')
    assert out['finding']['class'] == 'simulated'
    opinion = record(kit, title='Crystal placement looks odd', what='The crystal seems close to the edge.', targets=['ref:Y1'])
    assert opinion['finding']['class'] == 'opinion'


def test_opinion_cannot_be_critical(kit):
    error = reject(kit, severity='critical', title='Looks bad', what='It looks bad.', targets=['ref:Y1'])
    assert error.code == 'severity_not_supported'
    assert record(kit, severity='critical', candidate_ids=['candidate:decoupling:1'])['finding']['severity'] == 'critical'      # measured may be


def test_failed_trust_cannot_support_a_finding(kit):
    row = sim(kit, trust='fail', quality='confirm')
    error = reject(kit, evidence_ids=[row['code']], targets=['net:ANT_CHIP'], what='The antenna is detuned.')
    assert error.code == 'trust_failed' and row['code'] in error.message


def test_screen_runs_are_compare_only(kit):
    row = sim(kit)
    absolute = reject(kit, evidence_ids=[row['code']], targets=['net:ANT_CHIP'], what='In the simulation the antenna resonates at 2447 MHz.')
    assert absolute.code == 'number_check_failed' and absolute.data['reason'] == 'needs_confirm_run'
    out = record(kit, evidence_ids=[row['code']], targets=['net:ANT_CHIP'], what='In the simulation the return loss improves by 9.3 dB compared with the base.', rule='antenna_detune')
    assert 'screen-quality' in out['finding']['not_tested'][0]                       # the server records the limit
    confirmed = sim(kit, quality='confirm')
    out = kit.engine.record({**GOOD, 'update': out['finding']['code'], 'evidence_ids': [confirmed['code']], 'what': 'Confirmed: the antenna resonates at 2447 MHz.'}, candidates={})
    assert out['status'] == 'updated' and out['finding']['not_tested'] == []        # a confirm run supports it now
    assert len(kit.store.finding_history(out['finding']['id'])) >= 2                # old text stays in the history


def test_number_check_rejects_and_names_the_bad_number(kit):
    error = reject(kit, what='C13 is 7.5 mm from the U3 supply pin.', candidate_ids=['candidate:decoupling:1'])
    assert error.code == 'number_check_failed' and '7.5 mm' in error.message and error.data['field'] == 'what'
    assert reject(kit, recommendation='Move C13 to within 8 mm of U3.', candidate_ids=['candidate:decoupling:1']).data['number'] == '8 mm'
    assert record(kit, what='C13 is about 5 mm from the supply pin; the band is 2.4 to 2.4835 GHz.', candidate_ids=['candidate:decoupling:1'])      # rounding and setup facts


def test_anchor_check(kit):
    assert reject(kit, targets=['track:doesnotexist']).code == 'anchor_unresolved'
    assert reject(kit, candidate_ids=['candidate:decoupling:1'], marks=[{'type': 'object', 'targets': ['ref:NOPE']}]).code == 'anchor_unresolved'
    assert reject(kit).code == 'needs_anchor'
    wide = record(kit, scope='board', title='The board has no ground stitching', what='There is no stitching around the edge.')
    assert wide['finding']['marks'] == []
    mark = record(kit, targets=['ref:C13'], what='C13 looks far from the supply pin.')['finding']['marks']
    assert mark[0]['type'] == 'pin' and mark[0]['resolved'][0]['kind'] == 'polygon'         # a pin is added with the geometry resolved by the server


def test_recommendations_cannot_claim_an_edit(kit):
    for claim in ('I moved C13 closer to U3.', 'C13 has been moved next to U3.', 'The fix was applied to your board.', 'We removed the pour.', 'Here is the fixed layout.'):
        assert reject(kit, recommendation=claim, candidate_ids=['candidate:decoupling:1']).code == 'edit_claim', claim
    assert edit_claims('Move C13 closer to U3.') == []
    assert edit_claims('In the simulation the copper was removed and S11 improved.') == []


def test_whatif_placement_is_not_evidence(kit):
    row = kit.store.add_evidence(kit.review['id'], ev.PLACEMENT, 'try_placement', {'kind': 'whatif'})
    assert reject(kit, evidence_ids=[row['code']], targets=['ref:C13']).code == 'evidence_not_allowed'


def test_duplicates_are_grouped(kit):
    first = record(kit, candidate_ids=['candidate:decoupling:1'])
    second = record(kit, candidate_ids=['candidate:decoupling:1'], severity='high')
    assert second['status'] == 'grouped' and second['finding']['id'] == first['finding']['id'] and second['finding']['severity'] == 'high'
    assert len(kit.store.list_findings(kit.review['id'])) == 1


def test_fingerprint_survives_coordinate_changes(kit):
    index = TargetIndex(kit.board)
    nets = {}
    for track in kit.board.tracks:
        if track.net == 'ANT_CHIP':
            nets.setdefault(track.net, []).append(track.id)
    first, second = nets['ANT_CHIP'][:2]
    assert fingerprint('track_detour', [first], index) == fingerprint('track_detour', [second], index) == 'track_detour|net:ANT_CHIP|'
    assert fingerprint('decoupling', ['pad:C13:1', 'ref:U3'], index).startswith('decoupling|')


def test_dismissals_carry_forward_by_fingerprint(kit):
    fp = fingerprint('decoupling', ['ref:C13', 'ref:U3'], TargetIndex(kit.board))
    kit.engine.previous = {'dismissed': {fp: {'reason': 'Intentional: shared with C14', 'version_number': 1}}}
    out = record(kit, candidate_ids=['candidate:decoupling:1'])
    assert out['finding']['status'] == 'dismissed' and 'Dismissed on v1' in out['finding']['dismissed_reason']
