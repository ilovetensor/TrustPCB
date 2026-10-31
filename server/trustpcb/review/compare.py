"""Version comparison: what happened to each finding, and what changed on the board.

Findings are matched by fingerprint (rule, nets, zone or parts), and when the same results were grouped differently, by the fingerprints of the
results themselves (change.py). Each finding of the newer review has a `change` against the older one: fixed, improved, still_present, worse or new.
An old finding is `fixed` only when nothing with its fingerprint and none of its results is left; a finding with no old twin is `new` only when its
results are new too. Dismissals carry forward by fingerprint.
"""
from __future__ import annotations

from typing import Any

from . import change
from .fingerprint import rule_of

MEASURE_CHANGE = 0.05      # relative change in a measurement that counts as "changed"
MOVE_MM = 0.05             # a part moved by more than this is listed as moved


def _measurements(store, finding: dict) -> dict[str, float]:
    out: dict[str, float] = {}
    for evidence_id in finding.get('evidence_ids') or []:
        row = store.get_evidence(evidence_id)
        if not row:
            continue
        for name, item in ((row.get('values') or {}).get('measurements') or {}).items():
            if isinstance(item, dict) and isinstance(item.get('value'), (int, float)):
                out.setdefault(name, float(item['value']))
    return out


def _changed(old: dict, new: dict, old_m: dict, new_m: dict) -> tuple[list[str], str | None]:
    """What moved between two versions of a finding, and whether that is better or worse (None when the direction is not known)."""
    reasons, votes = [], []
    if old['severity'] != new['severity']:
        reasons.append(f"severity {old['severity']} -> {new['severity']}")
        votes.append('improved' if change.SEVERITY_RANK[new['severity']] > change.SEVERITY_RANK[old['severity']] else 'worse')
    for name in sorted(set(old_m) & set(new_m)):
        a, b = old_m[name], new_m[name]
        if abs(a - b) > MEASURE_CHANGE * max(abs(a), abs(b), 1e-9):
            reasons.append(f'{name} {a:g} -> {b:g}')
            vote = change.metric_direction(name, a, b)
            if vote:
                votes.append(vote)
    direction = None
    if votes and len(set(votes)) == 1:
        direction = votes[0]
    elif old['severity'] != new['severity']:
        direction = votes[0]      # the severity decides when the measurements disagree
    return reasons, direction


def _brief(finding: dict) -> dict:
    return {k: finding.get(k) for k in ('id', 'code', 'fingerprint', 'title', 'severity', 'class', 'status', 'dismissed_reason')}


CHANGE_STATES = ('fixed', 'improved', 'still_present', 'worse', 'new')


def _state_text(states: list[str]) -> str:
    kinds = sorted(set(states))
    return {'dismissed': 'cleared as harmless', 'recorded': 'recorded as a finding', 'unreviewed': 'not reviewed', 'new': 'not reviewed', 'grouped': 'grouped'}.get(kinds[0], kinds[0]) if len(kinds) == 1 else 'in several states'


def classify_findings(store, old: list[dict], new: list[dict], *, new_coverage: set[str] | None = None, version_change: dict | None = None,
                      old_review_id: str | None = None, new_review_id: str | None = None) -> dict:
    """Group old and new findings and say what happened to each (`change`: fixed, improved, still_present, worse or new).

    An old finding is matched to a new one by fingerprint first. A finding's fingerprint depends on which results it groups, so when the
    agent groups the same results differently the fingerprints differ; the results themselves (candidate fingerprints: rule, nets, parts)
    still match, and a finding whose results mostly reappear is the same issue (`matched_by: candidates`). An old finding with no twin is
    `fixed` only when its results are gone too; a new finding whose results the older review already had is `still_present` with a note, not `new`.

    `new_coverage` is the set of rules the newer review actually checked, when known; an old finding on an unchecked rule is `not_rechecked`
    instead of `fixed`. `version_change` (see change.py) lets the finding that cites the confirmed antenna base read as improved or worse.
    """
    old_review_id = old_review_id or next((f['review_id'] for f in old if f.get('review_id')), None)
    new_review_id = new_review_id or next((f['review_id'] for f in new if f.get('review_id')), None)
    old_cands, new_cands = change.prints_pair(store, old_review_id, new_review_id)
    old_of = {f['id']: change.finding_prints(store, f, old_cands) for f in old}
    new_of = {f['id']: change.finding_prints(store, f, new_cands) for f in new}
    new_all = {c['print'] for c in new_cands.values()}
    old_judged = {c['print'] for c in old_cands.values() if c['state'] in ('recorded', 'dismissed', 'grouped')}      # results the older review looked at
    live_new = {f['fingerprint']: f for f in new if f['status'] != 'superseded'}
    live_old = [f for f in old if f['status'] != 'superseded']
    antenna_ids = _antenna_evidence(store, new_review_id, version_change)
    out: dict[str, list] = {'fixed': [], 'still_present': [], 'changed': [], 'new': [], 'dismissed_carried': [], 'not_rechecked': []}
    claimed: set[str] = set()                      # new findings that an old finding matched
    pairs: list[tuple[dict, dict, str]] = []
    unmatched: list[dict] = []
    for finding in live_old:                       # 1. the same fingerprint
        twin = live_new.get(finding['fingerprint'])
        if twin is not None:
            claimed.add(twin['id'])
            pairs.append((finding, twin, 'fingerprint'))
        else:
            unmatched.append(finding)
    for finding in unmatched[:]:                   # 2. the same results, grouped differently
        mine = old_of.get(finding['id']) or set()
        best, share = None, 0.0
        for twin in live_new.values():
            if twin['fingerprint'] in {p[1]['fingerprint'] for p in pairs if p[2] == 'fingerprint'}:
                continue
            both = len(mine & (new_of.get(twin['id']) or set()))
            if mine and both / len(mine) > share:
                best, share = twin, both / len(mine)
        if best is not None and share >= change.OVERLAP:
            claimed.add(best['id'])
            pairs.append((finding, best, 'candidates'))
            unmatched.remove(finding)
    for finding, twin, how in pairs:
        extra = {'matched_by': 'candidates', 'candidates_matched': len((old_of.get(finding['id']) or set()) & (new_of.get(twin['id']) or set())),
                 'candidates_before': len(old_of.get(finding['id']) or ())} if how == 'candidates' else {}
        if finding['status'] == 'dismissed':
            out['dismissed_carried'].append({**_brief(twin), 'dismissed_on': _brief(finding)})
            continue
        reasons, direction = _changed(finding, twin, _measurements(store, finding), _measurements(store, twin))
        if antenna_ids and set(twin.get('evidence_ids') or []) & antenna_ids and version_change['direction'] in ('improved', 'worse'):
            vc = version_change
            reasons = [f'worst in-band S11 as built {vc["from"]["band_worst_s11_db"]:g} dB -> {vc["to"]["band_worst_s11_db"]:g} dB (version {vc["from_version"]} to {vc["to_version"]})']
            if None not in (vc['from'].get('accepted_power_worst_pct'), vc['to'].get('accepted_power_worst_pct')):
                reasons.append(f'accepted power at the worst point of the band {vc["from"]["accepted_power_worst_pct"]:g} % -> {vc["to"]["accepted_power_worst_pct"]:g} %')
            direction = vc['direction']
        entry = {**_brief(twin), 'was': _brief(finding), **extra}
        if reasons:
            out['changed'].append({**entry, 'changes': reasons, 'change': direction or 'still_present'})
        else:
            out['still_present'].append({**entry, 'change': 'still_present'})
    for finding in unmatched:                      # 3. nothing groups these results now: gone, or still reported without a finding
        if finding['status'] == 'dismissed':
            continue
        mine = old_of.get(finding['id']) or set()
        again = mine & new_all
        if mine and len(again) / len(mine) >= change.OVERLAP:
            states = [c['state'] for c in new_cands.values() if c['print'] in again]
            out['still_present'].append({**_brief(finding), 'was': _brief(finding), 'change': 'still_present', 'unrecorded': True, 'matched_by': 'candidates',
                                         'candidates_matched': len(again), 'candidates_before': len(mine),
                                         'note': f'The checks still report these results ({_state_text(states)}) but this review has no finding that covers them.'})
        elif new_coverage is not None and rule_of(finding['fingerprint']) not in new_coverage:
            out['not_rechecked'].append({**_brief(finding), 'change': 'not_rechecked'})
        else:
            out['fixed'].append({**_brief(finding), 'change': 'fixed'})
    for fp, finding in live_new.items():
        if finding['id'] in claimed:
            continue
        mine = new_of.get(finding['id']) or set()
        before = mine & old_judged
        if mine and len(before) / len(mine) >= change.OVERLAP:      # the older review already had these results: not new
            states = [c['state'] for c in old_cands.values() if c['print'] in before and c['state'] in ('recorded', 'dismissed', 'grouped')]
            out['still_present'].append({**_brief(finding), 'was': None, 'change': 'still_present', 'matched_by': 'candidates', 'candidates_matched': len(before),
                                         'candidates_before': len(mine), 'note': f'The checks already reported these results on the older version, where they were {_state_text(states)}. This review records them as a finding.'})
        else:
            out['new'].append({**_brief(finding), 'change': 'new'})
    out['counts'] = counts_of(out)
    return out


def counts_of(classified: dict) -> dict:
    counts = {state: 0 for state in CHANGE_STATES}
    counts['fixed'] = len(classified['fixed'])
    counts['new'] = len(classified['new'])
    for item in [*classified['still_present'], *classified['changed']]:
        counts[item['change'] if item['change'] in ('improved', 'worse') else 'still_present'] += 1
    counts['dismissed_carried'] = len(classified['dismissed_carried'])
    counts['not_rechecked'] = len(classified['not_rechecked'])
    return counts


def _antenna_evidence(store, review_id: str | None, vc: dict | None) -> set[str]:
    """The ids of the evidence row (the confirmed base) that the antenna finding of the newer review cites."""
    if not vc or not review_id:
        return set()
    code = (vc.get('to') or {}).get('evidence')
    return {r['id'] for r in store.list_evidence(review_id) if r['code'] == code}


def previous_context(store, version_id: str) -> dict | None:
    """What a recheck starts from: the latest finished review of the previous version of the same project."""
    version = store.get_version(version_id)
    if not version:
        return None
    for older in reversed([v for v in store.list_versions(version['project_id']) if v['number'] < version['number']]):
        reviews = [r for r in store.list_reviews(version_id=older['id']) if r.get('end_state')]
        if reviews:
            review = reviews[-1]
            findings = store.list_findings(review['id'])
            dismissed = {f['fingerprint']: {'reason': f['dismissed_reason'] or 'dismissed', 'version_number': older['number']}
                         for f in findings if f['status'] == 'dismissed'}
            return {'review': review, 'version': older, 'findings': findings, 'dismissed': dismissed,
                    'fingerprints': {f['fingerprint']: f for f in findings}, 'version_number': older['number']}
    return None


# ---- board differences -------------------------------------------------------------------

def _sig_track(t: Any) -> tuple:
    a, b = sorted([(round(t.start[0], 2), round(t.start[1], 2)), (round(t.end[0], 2), round(t.end[1], 2))])
    return (t.net, t.layer, a, b, round(t.width, 3))


def _sig_via(v: Any) -> tuple:
    return (v.net, round(v.x, 2), round(v.y, 2), round(v.diameter, 3))


def board_difference(old: Any, new: Any) -> dict:
    """Copper and part differences between two boards, with marks the UI can draw on either one."""
    old_fp = {f.ref: f for f in old.footprints}
    new_fp = {f.ref: f for f in new.footprints}
    parts: dict[str, list] = {'added': [], 'removed': [], 'moved': [], 'value_changed': []}
    for ref in sorted(new_fp.keys() - old_fp.keys()):
        parts['added'].append({'ref': ref, 'value': new_fp[ref].value})
    for ref in sorted(old_fp.keys() - new_fp.keys()):
        parts['removed'].append({'ref': ref, 'value': old_fp[ref].value})
    for ref in sorted(old_fp.keys() & new_fp.keys()):
        a, b = old_fp[ref], new_fp[ref]
        distance = ((a.x - b.x) ** 2 + (a.y - b.y) ** 2) ** 0.5
        if distance > MOVE_MM or a.side != b.side or abs((a.rotation - b.rotation + 180) % 360 - 180) > 0.5:
            parts['moved'].append({'ref': ref, 'from': [a.x, a.y], 'to': [b.x, b.y], 'distance_mm': round(distance, 3), 'rotated': a.rotation != b.rotation, 'side_changed': a.side != b.side})
        if a.value != b.value:
            parts['value_changed'].append({'ref': ref, 'from': a.value, 'to': b.value})
    old_tracks = {_sig_track(t): t for t in old.tracks}
    new_tracks = {_sig_track(t): t for t in new.tracks}
    added_tracks = [new_tracks[s] for s in new_tracks.keys() - old_tracks.keys()]
    removed_tracks = [old_tracks[s] for s in old_tracks.keys() - new_tracks.keys()]
    old_vias = {_sig_via(v): v for v in old.vias}
    new_vias = {_sig_via(v): v for v in new.vias}
    added_vias = [new_vias[s] for s in new_vias.keys() - old_vias.keys()]
    removed_vias = [old_vias[s] for s in old_vias.keys() - new_vias.keys()]

    def by_net(items) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in items:
            counts[item.net or '(no net)'] = counts.get(item.net or '(no net)', 0) + 1
        return counts

    nets_old, nets_new = set(old.net_names()), set(new.net_names())
    marks_new = [{'type': 'object', 'targets': [t.id for t in added_tracks[:60]] + [v.id for v in added_vias[:30]] + [f'ref:{p["ref"]}' for p in parts['added']], 'label': 'added'}]
    marks_old = [{'type': 'object', 'targets': [t.id for t in removed_tracks[:60]] + [v.id for v in removed_vias[:30]] + [f'ref:{p["ref"]}' for p in parts['removed']], 'label': 'removed'}]
    marks_new.append({'type': 'object', 'targets': [f'ref:{p["ref"]}' for p in parts['moved']], 'label': 'moved'})
    return {
        'parts': parts,
        'tracks': {'added': len(added_tracks), 'removed': len(removed_tracks), 'added_by_net': by_net(added_tracks), 'removed_by_net': by_net(removed_tracks),
                   'length_added_mm': round(sum(t.length for t in added_tracks), 2), 'length_removed_mm': round(sum(t.length for t in removed_tracks), 2)},
        'vias': {'added': len(added_vias), 'removed': len(removed_vias), 'count_old': len(old.vias), 'count_new': len(new.vias)},
        'nets': {'added': sorted(nets_new - nets_old), 'removed': sorted(nets_old - nets_new)},
        'zones': {'old': len(old.zones), 'new': len(new.zones)},
        'marks': {'to': [m for m in marks_new if m['targets']], 'from': [m for m in marks_old if m['targets']]},
    }


def _latest_review(store, version_id: str) -> dict | None:
    reviews = [r for r in store.list_reviews(version_id=version_id) if r.get('end_state')]
    return reviews[-1] if reviews else None


async def compare_versions(*, store, from_id: str, to_id: str) -> dict:
    """GET /api/compare: the finding classification and board differences between two versions."""
    from . import service
    older, newer = store.get_version(from_id), store.get_version(to_id)
    if not older or not newer:
        raise ValueError('Version not found')
    if older['project_id'] != newer['project_id']:
        raise ValueError('Both versions must belong to the same project')
    review_a, review_b = _latest_review(store, from_id), _latest_review(store, to_id)
    result: dict[str, Any] = {
        'from': {'version_id': from_id, 'number': older['number'], 'review_id': review_a and review_a['id'], 'counts': review_a and store.severity_counts(review_a['id'])},
        'to': {'version_id': to_id, 'number': newer['number'], 'review_id': review_b and review_b['id'], 'counts': review_b and store.severity_counts(review_b['id'])},
        'findings': None, 'version_change': None, 'board': None, 'notes': [],
    }
    if review_a and review_b:
        coverage = None
        if review_b['depth'] == 'quick':
            coverage = {rule_of(f['fingerprint']) for f in store.list_findings(review_b['id'])} | {c['rule'] for c in store.list_candidates(review_b['id']) if c.get('rule')}
        vc = change.version_change(store, review_b['id'], review_a['id'])
        result['version_change'] = vc
        result['findings'] = classify_findings(store, store.list_findings(review_a['id']), store.list_findings(review_b['id']), new_coverage=coverage, version_change=vc,
                                               old_review_id=review_a['id'], new_review_id=review_b['id'])
        result['counts_change'] = {s: (result['to']['counts'] or {}).get(s, 0) - (result['from']['counts'] or {}).get(s, 0) for s in result['to']['counts'] or {}}
    else:
        result['notes'].append('Both versions need a finished review to compare findings.')
    try:
        lab = service.get_lab(store)
        board_a, board_b = await lab.load_board(version_id=from_id), await lab.load_board(version_id=to_id)
        result['board'] = board_difference(board_a, board_b)
    except Exception as exc:
        result['notes'].append(f'Board differences are not available: {str(exc)[:200]}')
    return result
