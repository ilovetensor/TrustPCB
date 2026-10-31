"""A recheck leads with what changed since the previous version, and findings keep their identity when the same results are grouped differently."""
import pytest

from trustpcb.review import change, compare, report
from trustpcb.review.service import deterministic_summary


def candidate(rule, n, targets):
    return {'id': f'candidate:{rule}:{n}', 'rule': rule, 'title': f'{rule} {n}', 'source': 'kicad', 'targets': targets, 'measurements': {}, 'marks': [], 'severity_hint': 'medium'}


def two_reviews(env):
    store = env.store
    old = store.create_review(env.version['id'], goal={'target_s11_db': -10, 'band_hz': [2.4e9, 2.4835e9]})
    version2 = store.create_version(env.project['id'])
    new = store.create_review(version2['id'], goal={'target_s11_db': -10, 'band_hz': [2.4e9, 2.4835e9]})
    for review in (old, new):
        store.update_review(review['id'], end_state='complete')
    return old, new


def finding_over(store, review, fingerprint, candidates, title='finding', severity='medium'):
    rows = [store.add_evidence(review['id'], 'candidate', 'kicad', {'kind': 'measured', 'candidate_id': c['id'], 'rule': c['rule'], 'targets': c['targets'], 'measurements': {}}) for c in candidates]
    return store.create_finding(review['id'], fingerprint, title, severity, 'measured', evidence_ids=[r['id'] for r in rows], marks=[])


def test_the_same_results_grouped_differently_are_still_present_not_fixed_and_new(env):
    store = env.store
    old, new = two_reviews(env)
    cands = [candidate('drc_shorting_items', i, [f'track:t{i}', f'pad:U3:{i}']) for i in range(1, 5)]
    for review in (old, new):
        for c in cands:
            store.add_candidate(review['id'], c, 'recorded')
    finding_over(store, old, 'drc_shorting_items|net:A,net:B|ref:U3', cands, 'four shorts')
    finding_over(store, new, 'drc_shorting_items|net:A,net:B,net:C|ref:U3', cands[:3], 'three shorts, grouped differently')
    out = compare.classify_findings(store, store.list_findings(old['id']), store.list_findings(new['id']))
    assert [f['title'] for f in out['still_present']] == ['three shorts, grouped differently'] and out['still_present'][0]['matched_by'] == 'candidates'
    assert out['fixed'] == [] and out['new'] == [] and out['counts'] == {'fixed': 0, 'improved': 0, 'still_present': 1, 'worse': 0, 'new': 0, 'dismissed_carried': 0, 'not_rechecked': 0}


def test_results_the_older_review_cleared_as_harmless_are_not_new_when_recorded_now(env):
    store = env.store
    old, new = two_reviews(env)
    mask = [candidate('drc_solder_mask_bridge', i, [f'track:m{i}', f'pad:J2:{i}']) for i in (1, 2)]
    other = candidate('drc_clearance', 1, ['track:z', 'pad:U3:1'])
    for c in mask:
        store.add_candidate(old['id'], c, 'dismissed')
        store.add_candidate(new['id'], {**c, 'id': c['id'].replace(':1', ':9').replace(':2', ':8')}, 'recorded')       # numbered differently
    store.add_candidate(new['id'], other, 'recorded')
    finding_over(store, new, 'drc_solder_mask_bridge|net:X|ref:J2', [{**c, 'id': c['id'].replace(':1', ':9').replace(':2', ':8')} for c in mask], 'mask bridges')
    finding_over(store, new, 'drc_clearance|net:Y|ref:U3', [other], 'a truly new clearance result')
    out = compare.classify_findings(store, store.list_findings(old['id']), store.list_findings(new['id']), new_review_id=new['id'], old_review_id=old['id'])
    assert [f['title'] for f in out['new']] == ['a truly new clearance result']
    bridged = out['still_present'][0]
    assert bridged['title'] == 'mask bridges' and bridged['was'] is None and 'cleared as harmless' in bridged['note'] and out['counts']['new'] == 1


def test_a_severity_drop_is_an_improvement_and_a_rise_is_worse(env):
    store = env.store
    old, new = two_reviews(env)
    for fp, a, b in (('a|net:A|', 'high', 'medium'), ('b|net:B|', 'low', 'high')):
        store.create_finding(old['id'], fp, fp, a, 'measured', marks=[])
        store.create_finding(new['id'], fp, fp, b, 'measured', marks=[])
    out = compare.classify_findings(store, store.list_findings(old['id']), store.list_findings(new['id']))
    assert {f['fingerprint']: f['change'] for f in out['changed']} == {'a|net:A|': 'improved', 'b|net:B|': 'worse'}
    assert out['counts']['improved'] == 1 and out['counts']['worse'] == 1


def confirm_base(store, review, worst, mean_s11, power_worst, power_mean):
    m = {'as_built_band_worst_s11': worst, 'as_built_band_mean_s11': mean_s11, 'as_built_band_worst_accepted_power_pct': power_worst, 'as_built_band_mean_accepted_power_pct': power_mean}
    return store.add_evidence(review['id'], 'confirm', 'confirm_result', {'kind': 'simulated', 'quality': 'confirm', 'label': 'confirm base', 'trust': {'status': 'ok', 'reasons': []},
                                                                       'measurements': {k: {'value': v, 'unit': 'dB' if 's11' in k else '%'} for k, v in m.items()}, 'metrics': {'basis': 'as_built'}})


def partial_win(store, review, predicted, parts):
    inv = store.create_investigation(review['id'], 'Is the antenna tuned?')
    best = {'mode': 'free_retune', 'ref': 'R6', 'evidence': 'E-009', 'ops': [], 'parts': parts, 'band_worst_s11_db': predicted, 'band_mean_s11_db': -8.48,
            'accepted_power_worst_pct': 73.6, 'accepted_power_mean_pct': 85.2, 'meets_target': False}
    store.update_investigation(inv['id'], status='closed', outcome={'outcome': 'improved', 'finding': 'F-001', 'confirmation': {
        'best': best, 'base': {'ref': 'R4', 'band_worst_s11_db': -0.5, 'accepted_power_worst_pct': 10.9, 'accepted_power_mean_pct': 11.6}, 'best_gain_db': 5.28, 'outcome': 'improved_short_of_target'}})


PARTS = [{'ref': 'C20', 'position': 'shunt', 'kind': 'C', 'value': 3e-13, 'was': 'not fitted', 'now': '0.3 pF'}, {'ref': 'L1', 'position': 'series', 'kind': 'C', 'value': 4.7e-13, 'was': '2.2 nH', 'now': '0.47 pF'}]


def recheck_pair(env, measured=-5.78, new_parts=('0.3 pF', '0.47 pF')):
    store = env.store
    old, new = two_reviews(env)
    confirm_base(store, old, -0.5, -0.53, 10.9, 11.6)
    partial_win(store, old, -5.78, PARTS)
    confirm_base(store, new, measured, -8.48, 73.6, 85.2)
    store.add_evidence(new['id'], 'calculation', 'confirm_result', {'kind': 'simulated', 'quality': 'confirm', 'label': 'confirm matching, free tuning', 'trust': {'status': 'ok'},
                                                                     'parts': [{'ref': 'C20', 'was': new_parts[0], 'now': '0.3 pF'}, {'ref': 'L1', 'was': new_parts[1], 'now': '0.47 pF'}], 'measurements': {}})
    return old, new


def test_the_version_change_compares_the_as_built_result_with_the_previous_prediction(env):
    old, new = recheck_pair(env)
    vc = change.version_change(env.store, new['id'], old['id'])
    assert (vc['from_version'], vc['to_version'], vc['gain_db'], vc['direction']) == (1, 2, 5.28, 'improved')
    assert vc['from']['accepted_power_worst_pct'] == 10.9 and vc['to']['accepted_power_worst_pct'] == 73.6 and vc['to']['accepted_power_mean_pct'] == 85.2
    assert vc['prediction_from_previous']['band_worst_s11_db'] == -5.78 and vc['applied'] is True and vc['held'] is True and vc['difference_db'] == 0.0
    assert vc['target_met'] is False and vc['text'].startswith('Compared with version 1, the as-built worst S11 across the band improved from -0.5 dB to -5.78 dB, a gain of 5.28 dB.')
    assert 'the prediction held' in vc['text'] and 'target of -10 dB is not met' in vc['text']
    assert report.build_snapshot(env.store, new['id'], cause='review')['headline']['kind'] == 'version_change'


def test_a_prediction_that_missed_by_more_than_1_db_did_not_hold_and_unapplied_parts_are_not_tested(env):
    old, new = recheck_pair(env, measured=-3.9)
    vc = change.version_change(env.store, new['id'], old['id'])
    assert vc['held'] is False and vc['difference_db'] == 1.88 and 'did not hold' in vc['text']
    old, new = recheck_pair(env, new_parts=('not fitted', '2.2 nH'))
    vc = change.version_change(env.store, new['id'], old['id'])
    assert vc['applied'] is False and vc['held'] is None and 'prediction was not tested' in vc['text']


def test_no_version_change_without_a_confirmed_base_in_both_reviews(env):
    old, new = two_reviews(env)
    confirm_base(env.store, new, -5.78, -8.48, 73.6, 85.2)
    assert change.version_change(env.store, new['id'], old['id']) is None


def test_the_antenna_finding_that_cites_the_confirmed_base_reads_as_improved(env):
    store = env.store
    old, new = recheck_pair(env)
    store.create_finding(old['id'], 'open_review|net:ANT_FEED|ref:L1', 'Antenna is not tuned', 'high', 'simulated', marks=[])
    base = next(r for r in store.list_evidence(new['id']) if r['values'].get('label') == 'confirm base')
    store.create_finding(new['id'], 'open_review|net:ANT_FEED|ref:L1', 'Antenna is still not tuned', 'high', 'simulated', evidence_ids=[base['id']], marks=[])
    vc = change.version_change(store, new['id'], old['id'])
    out = compare.classify_findings(store, store.list_findings(old['id']), store.list_findings(new['id']), version_change=vc)
    assert out['changed'][0]['change'] == 'improved' and out['counts']['improved'] == 1 and 'from' not in out['changed'][0]['changes'][0] and '-0.5 dB -> -5.78 dB' in out['changed'][0]['changes'][0]


def test_a_recheck_summary_must_lead_with_the_version_change(env):
    old, new = recheck_pair(env)
    vc = change.version_change(env.store, new['id'], old['id'])
    bad = ('Recheck of version 2. Answer to the focus question: no, the antenna is not tuned. The worst S11 is -5.78 dB against the -10 dB target. So this is not a partial win. '
           'Counts: 14 still present, same as in version 2.')
    problem = change.lead_problem(bad, vc)
    assert problem and 'version 1' in problem and vc['text'] in problem
    assert change.lead_problem(vc['text'] + ' The copper is the same.', vc) is None
    assert change.lead_problem('Anything at all.', None) is None
    allowed = change.quantities(vc)
    from trustpcb.review.number_check import check_text
    assert check_text(vc['text'], allowed) == []              # the sentence the server writes passes the number check


def test_the_code_written_summary_of_a_recheck_leads_with_the_version_change(env):
    old, new = recheck_pair(env)
    env.store.update_review(new['id'], summary={})
    vc = change.version_change(env.store, new['id'], old['id'])
    summary = deterministic_summary(env.store, new['id'], 'complete', 'completed', 'tested', extra={'version_change': vc, 'version_changes': report.version_changes(env.store, new['id'])})
    assert summary['text'].startswith('Compared with version 1, the as-built worst S11 across the band improved') and 'Findings compared with version 1:' in summary['text']


def test_a_job_folder_is_found_after_the_data_folder_was_copied(env):
    from trustpcb.review.sims import job_folder
    store = env.store
    folder = store.scenario_dir('abc123-11111111')
    folder.mkdir(parents=True, exist_ok=True)
    assert job_folder(store, {'job_dir': '/old/place/of/the/data/scenarios/abc123-11111111'}) == folder.resolve()
    assert job_folder(store, {'job_dir': '/old/place/scenarios/../../etc'}) is None
    assert job_folder(store, {'job_dir': '/old/place/scenarios/missing-folder'}) is None


def test_the_version_change_names_the_finding_of_the_newer_review_that_cites_the_confirmed_base(env):
    store = env.store
    old, new = recheck_pair(env)
    base = next(r for r in store.list_evidence(new['id']) if r['values'].get('label') == 'confirm base')
    store.create_finding(new['id'], 'zones||x', 'something else', 'low', 'measured', marks=[])
    antenna = store.create_finding(new['id'], 'open_review|net:ANT_FEED|ref:L1', 'Antenna', 'high', 'simulated', evidence_ids=[base['id']], marks=[])
    assert change.version_change(store, new['id'], old['id'])['finding'] == antenna['code']
