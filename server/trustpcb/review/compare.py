"""Version comparison: what happened to each finding, and what changed on the board.

Findings are matched by fingerprint (rule, nets, zone or parts), so a finding keeps its identity when
a track moves a little. An old finding is `fixed` when nothing with its fingerprint is left,
`still_present` when it is unchanged, `changed` when its severity or measurements moved, and a
finding with no old twin is `new`. Dismissals carry forward by fingerprint.
"""
from __future__ import annotations

from typing import Any

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


def _changed(old: dict, new: dict, old_m: dict, new_m: dict) -> list[str]:
    reasons = []
    if old['severity'] != new['severity']:
        reasons.append(f"severity {old['severity']} -> {new['severity']}")
    for name in sorted(set(old_m) & set(new_m)):
        a, b = old_m[name], new_m[name]
        if abs(a - b) > MEASURE_CHANGE * max(abs(a), abs(b), 1e-9):
            reasons.append(f'{name} {a:g} -> {b:g}')
    return reasons


def _brief(finding: dict) -> dict:
    return {k: finding.get(k) for k in ('id', 'code', 'fingerprint', 'title', 'severity', 'class', 'status', 'dismissed_reason')}


def classify_findings(store, old: list[dict], new: list[dict], *, new_coverage: set[str] | None = None) -> dict:
    """Group old and new findings. `new_coverage` is the set of rules the newer review actually checked,
    when known; an old finding on an unchecked rule is `not_rechecked` instead of `fixed`."""
    live_new = {f['fingerprint']: f for f in new if f['status'] != 'superseded'}
    live_old = [f for f in old if f['status'] != 'superseded']
    seen: set[str] = set()
    out: dict[str, list] = {'fixed': [], 'still_present': [], 'changed': [], 'new': [], 'dismissed_carried': [], 'not_rechecked': []}
    for finding in live_old:
        twin = live_new.get(finding['fingerprint'])
        seen.add(finding['fingerprint'])
        if twin is None:
            if finding['status'] == 'dismissed':
                continue
            if new_coverage is not None and rule_of(finding['fingerprint']) not in new_coverage:
                out['not_rechecked'].append(_brief(finding))
            else:
                out['fixed'].append(_brief(finding))
        elif finding['status'] == 'dismissed':
            out['dismissed_carried'].append({**_brief(twin), 'dismissed_on': _brief(finding)})
        else:
            reasons = _changed(finding, twin, _measurements(store, finding), _measurements(store, twin))
            entry = {**_brief(twin), 'was': _brief(finding)}
            if reasons:
                out['changed'].append({**entry, 'changes': reasons})
            else:
                out['still_present'].append(entry)
    for fp, finding in live_new.items():
        if fp not in seen:
            out['new'].append(_brief(finding))
    return out


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
        'findings': None, 'board': None, 'notes': [],
    }
    if review_a and review_b:
        coverage = None
        if review_b['depth'] == 'quick':
            coverage = {rule_of(f['fingerprint']) for f in store.list_findings(review_b['id'])} | {c['rule'] for c in store.list_candidates(review_b['id']) if c.get('rule')}
        result['findings'] = classify_findings(store, store.list_findings(review_a['id']), store.list_findings(review_b['id']), new_coverage=coverage)
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
