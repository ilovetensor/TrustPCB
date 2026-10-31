"""What changed since the previous version, from stored data only.

Two things live here.

`version_change` is the headline of a recheck: the as-built result across the band in the previous version against this one (worst S11, accepted
power worst and mean), the prediction the previous review made for the fix, and whether this version's result is consistent with that prediction within 1 dB.
Every number is read from stored confirm evidence. Nothing is computed by a model.

Honesty rule: when the antenna copper is unchanged, this version's number is the SAME solver run finalised with this version's matching parts. It is
not an independent result, and it is simulated, never measured. Such a result is `consistent` (or not) with the prediction. Only when the geometry
differs and a separate confirm run was done is the prediction said to have `held`.

Candidate fingerprints let findings keep their identity when the agent groups the same results differently. A finding's fingerprint depends on which
results it covers, so a regrouped finding looks new; the results themselves (rule, nets, parts) do not change, and those match across versions.
"""
from __future__ import annotations

import re
from typing import Any

from . import evidence as ev
from . import partial
from .fingerprint import TargetIndex, fingerprint
from .number_check import Quantity, allowed_from_text, extract, make_quantity

HOLD_DB = 1.0          # a result is consistent with a prediction (or the prediction held) when the worst in-band S11 is within this many dB of it
SAME_DB = 0.5          # the solver repeats to about this much, so a smaller change is no change (contracts: S11 depth repeats to +-0.5 dB)
OVERLAP = 0.5          # share of a finding's results that must reappear for the finding to count as the same issue
S11_KEYS = ('as_built_band_worst_s11', 's11_band_worst')
MEAN_KEYS = ('as_built_band_mean_s11', 's11_band_mean')
POWER_WORST_KEYS = ('as_built_band_worst_accepted_power_pct', 'band_worst_accepted_power_pct')
POWER_MEAN_KEYS = ('as_built_band_mean_accepted_power_pct', 'band_mean_accepted_power_pct')
SEVERITY_RANK = {'critical': 0, 'high': 1, 'medium': 2, 'low': 3, 'info': 4}


# ---- candidate fingerprints ----------------------------------------------------------------------------------------------

_POINT = re.compile(r'^point:(-?[\d.]+),(-?[\d.]+)$')


def candidate_print(candidate: dict, index: TargetIndex | None = None) -> str:
    """The identity of one analyzer or KiCad result: rule, nets and parts, so it survives a new numbering and small moves.

    With the board's index the targets resolve to nets and parts. Without one (older stored reviews) the target ids themselves are used.
    """
    rule, targets = candidate.get('rule') or '', list(candidate.get('targets') or [])
    if index is not None:
        return fingerprint(rule, targets, index)
    nets, others = set(), set()
    for target in targets:
        match = _POINT.match(target)
        if match:
            others.add(f'cell:{round(float(match.group(1)) / 2) * 2},{round(float(match.group(2)) / 2) * 2}')
        elif target.startswith('net:'):
            nets.add(target)
        else:
            others.add(target)
    return f'{rule}|{",".join(sorted(nets))}|{",".join(sorted(others))}'


def review_prints(store, review_id: str, *, stored: bool) -> dict[str, dict]:
    """{candidate_id: {print, state, rule}} for a review. `stored` uses the fingerprint saved when the candidate was ingested."""
    out: dict[str, dict] = {}
    for row in store.list_candidates(review_id):
        data = row.get('data') or {}
        out[row['candidate_id']] = {'print': (data.get('fingerprint') if stored else None) or candidate_print(data), 'state': row['review_state'], 'rule': row.get('rule')}
    return out


def _all_stored(store, review_id: str) -> bool:
    rows = store.list_candidates(review_id)
    return bool(rows) and all((r.get('data') or {}).get('fingerprint') for r in rows)


def prints_pair(store, old_review_id: str | None, new_review_id: str | None) -> tuple[dict, dict]:
    """The candidate prints of two reviews, both computed the same way (stored fingerprints only when both reviews have them)."""
    if not old_review_id or not new_review_id:
        return {}, {}
    stored = _all_stored(store, old_review_id) and _all_stored(store, new_review_id)
    return review_prints(store, old_review_id, stored=stored), review_prints(store, new_review_id, stored=stored)


def finding_prints(store, finding: dict, by_candidate: dict[str, dict]) -> set[str]:
    """The prints of the results a finding covers (its candidate evidence rows)."""
    out: set[str] = set()
    for evidence_id in finding.get('evidence_ids') or []:
        row = store.get_evidence(evidence_id)
        if row and row['type'] == ev.CANDIDATE:
            item = by_candidate.get((row['values'] or {}).get('candidate_id'))
            if item:
                out.add(item['print'])
    return out


# ---- the as-built result of a review -------------------------------------------------------------------------------------

def _num(measurements: dict, names: tuple[str, ...]) -> float | None:
    for name in names:
        item = measurements.get(name)
        if isinstance(item, dict) and isinstance(item.get('value'), (int, float)):
            return float(item['value'])
    return None


def as_built(rows: list[dict]) -> dict | None:
    """The confirmed base of a review, as built: worst and mean S11 in the band, accepted power worst and mean, and the evidence code."""
    row = next((r for r in rows if r['type'] == ev.CONFIRM and (r['values'] or {}).get('label') == 'confirm base' and ((r['values'] or {}).get('trust') or {}).get('status') == 'ok'), None)
    if row is None:
        return None
    values = row['values'] or {}
    m = values.get('measurements') or {}
    worst = _num(m, S11_KEYS)
    if worst is None:
        return None
    return {'band_worst_s11_db': worst, 'band_mean_s11_db': _num(m, MEAN_KEYS), 'accepted_power_worst_pct': _num(m, POWER_WORST_KEYS),
            'accepted_power_mean_pct': _num(m, POWER_MEAN_KEYS), 'evidence': row['code'], 'basis': (values.get('metrics') or {}).get('basis') or 'as_built'}


def solver_run(store, rows: list[dict]) -> str | None:
    """The solver output the confirmed base of a review was finalised from: the geometry key of its simulation job.

    A job's hash is the geometry key, then (when its matching parts were finalised again) a dash and a signature of those parts. Two versions with
    the same antenna copper therefore share the part before the dash: one solver output, post-processed with each version's own parts.
    None when it cannot be told (no job recorded).
    """
    row = next((r for r in rows if r['type'] == ev.CONFIRM and (r['values'] or {}).get('label') == 'confirm base' and ((r['values'] or {}).get('trust') or {}).get('status') == 'ok'), None)
    job_id = ((row or {}).get('values') or {}).get('job_id')
    job = store.get_sim_job(job_id) if job_id else None
    return str(job['scenario_hash']).split('-')[0] if job and job.get('scenario_hash') else None


def _same_value(a: Any, b: Any) -> bool:
    """Two part values written as text ("0.3 pF", "0.3pF (fitted)") are the same part value."""
    clean = lambda x: re.sub(r'\s*\(fitted\)', '', str(x or '')).strip()
    qa, qb = extract(clean(a)), extract(clean(b))
    if qa and qb:
        return qa[0].dimension == qb[0].dimension and abs(qa[0].base - qb[0].base) <= 0.01 * max(abs(qa[0].base), abs(qb[0].base), 1e-30)
    return clean(a).lower().replace(' ', '') == clean(b).lower().replace(' ', '')


def prediction_of(store, review_id: str) -> dict | None:
    """What the review said a fix would do, confirmed at confirm quality: the partial win's best parts, else the confirmed change."""
    from . import report
    review = store.get_review(review_id) or {}
    investigations = report._investigations(store, review_id)
    target = ev.target_db(review.get('goal') or {}, (review.get('summary') or {}).get('rf') or {})
    win = partial.headline(investigations, store.list_findings(review_id), target)
    if win:
        best = win['best']
        return {'source': 'partial_win', 'finding': win.get('finding'), 'evidence': best.get('evidence'), 'parts': best.get('parts') or [], 'ops': best.get('ops') or [],
                'parts_text': win['parts_text'], 'band_worst_s11_db': best['band_worst_s11_db'], 'band_mean_s11_db': best.get('band_mean_s11_db'),
                'accepted_power_worst_pct': best.get('accepted_power_worst_pct'), 'accepted_power_mean_pct': best.get('accepted_power_mean_pct')}
    for inv in investigations:
        c = inv.get('confirmation') or {}
        winner = c.get('winner') or {}
        confirm = ((c.get('rows') or {}).get('winner') or {}).get('confirm') or {}
        if c.get('outcome') in ('held', 'held_smaller', 'held_after_retune') and confirm.get('band_worst_s11_db') is not None and (winner.get('parts') or winner.get('ops')):
            best = {'ops': winner.get('ops') or [], 'parts': winner.get('parts') or []}
            return {'source': 'confirmed_change', 'finding': (inv.get('outcome') or {}).get('finding'), 'evidence': c.get('comparison_evidence'), 'parts': best['parts'], 'ops': best['ops'],
                    'parts_text': partial.parts_text(best), 'band_worst_s11_db': round(confirm['band_worst_s11_db'], 2),
                    'band_mean_s11_db': None if confirm.get('band_mean_s11_db') is None else round(confirm['band_mean_s11_db'], 2),
                    'accepted_power_worst_pct': confirm.get('band_worst_accepted_power_pct'), 'accepted_power_mean_pct': confirm.get('band_mean_accepted_power_pct')}
    return None


def _applied(prediction: dict | None, rows: list[dict]) -> bool | None:
    """Are the previous review's recommended part values the ones this version has? None when it cannot be read from the evidence."""
    if not prediction or not prediction.get('parts') or prediction.get('ops'):
        return None
    current: dict[str, Any] = {}
    for row in rows:
        for part in (row['values'] or {}).get('parts') or []:
            if part.get('ref') and part.get('was') is not None:
                current.setdefault(part['ref'], part['was'])
    checks = [_same_value(current[p['ref']], p['now']) for p in prediction['parts'] if p.get('ref') in current]
    return all(checks) if checks else None


def version_change(store, newer_review_id: str, older_review_id: str) -> dict | None:
    """The as-built result of the newer review against the older one, with the older review's prediction. None when either has no confirmed base."""
    newer, older = store.get_review(newer_review_id), store.get_review(older_review_id)
    if not newer or not older:
        return None
    rows_new, rows_old = store.list_evidence(newer_review_id), store.list_evidence(older_review_id)
    to, frm = as_built(rows_new), as_built(rows_old)
    if not to or not frm or to['basis'] != frm['basis']:
        return None
    number = lambda review: (store.get_version(review['version_id']) or {}).get('number')
    goal, rf = newer.get('goal') or {}, (newer.get('summary') or {}).get('rf') or {}
    target = ev.target_db(goal, rf)
    gain = round(frm['band_worst_s11_db'] - to['band_worst_s11_db'], 2)
    direction = 'improved' if gain >= SAME_DB else 'worse' if gain <= -SAME_DB else 'unchanged'
    prediction = prediction_of(store, older_review_id)
    applied = _applied(prediction, rows_new)
    difference = consistent = held = None
    run_new, run_old = solver_run(store, rows_new), solver_run(store, rows_old)
    # same_solver_run: True when both bases come from one solver output (the copper is unchanged), False when two separate runs, None when it cannot be told.
    same_run = None if not (run_new and run_old) else run_new == run_old
    if prediction:
        difference = round(to['band_worst_s11_db'] - prediction['band_worst_s11_db'], 2)
        consistent = None if applied is False else abs(difference) <= HOLD_DB
        held = consistent if (same_run is False and consistent is not None) else None       # only an independent confirm run can say the prediction held
    base_ids = {r['id'] for r in rows_new if r['code'] == to['evidence']}
    antenna = next((f['code'] for f in store.list_findings(newer_review_id) if f['status'] != 'superseded' and base_ids & set(f.get('evidence_ids') or [])), None)
    out = {
        'kind': 'version_change', 'finding': antenna, 'metric': 'as_built_band_worst_s11', 'unit': 'dB', 'basis': to['basis'],
        'from_version': number(older), 'to_version': number(newer), 'from_review_id': older_review_id, 'to_review_id': newer_review_id,
        'from': frm, 'to': to, 'gain_db': gain, 'direction': direction,
        'accepted_power_gain_worst_pct': None if None in (frm['accepted_power_worst_pct'], to['accepted_power_worst_pct']) else round(to['accepted_power_worst_pct'] - frm['accepted_power_worst_pct'], 1),
        'accepted_power_gain_mean_pct': None if None in (frm['accepted_power_mean_pct'], to['accepted_power_mean_pct']) else round(to['accepted_power_mean_pct'] - frm['accepted_power_mean_pct'], 1),
        'prediction_from_previous': prediction, 'applied': applied, 'same_solver_run': same_run, 'independent_check': same_run is False, 'consistent': consistent, 'held': held,
        'difference_db': difference, 'hold_tolerance_db': HOLD_DB,
        'target_db': target, 'target_met': to['band_worst_s11_db'] <= target, 'band_hz': rf.get('band_hz') or goal.get('band_hz'),
    }
    out['text'] = lead_text(out)
    return out


# ---- the words -----------------------------------------------------------------------------------------------------------

def lead_text(vc: dict) -> str:
    """The lead of a recheck's summary and report, from the stored numbers."""
    a, b, frm, to = vc['from_version'], vc['to_version'], vc['from'], vc['to']
    moved = {'improved': 'improved', 'worse': 'got worse', 'unchanged': 'did not change'}[vc['direction']]
    text = f'Compared with version {a}, the as-built worst S11 across the band {moved}'
    if vc['direction'] == 'unchanged':
        text += f' ({to["band_worst_s11_db"]:g} dB in version {b}, {frm["band_worst_s11_db"]:g} dB in version {a}).'
    else:
        text += f' from {frm["band_worst_s11_db"]:g} dB to {to["band_worst_s11_db"]:g} dB, a {"gain" if vc["direction"] == "improved" else "loss"} of {abs(vc["gain_db"]):g} dB.'
    if frm.get('accepted_power_worst_pct') is not None and to.get('accepted_power_worst_pct') is not None:
        text += (f' Accepted power (a mismatch figure only, not radiated power or efficiency) at the worst point of the band went from {frm["accepted_power_worst_pct"]:g} % to '
                 f'{to["accepted_power_worst_pct"]:g} %')
        if frm.get('accepted_power_mean_pct') is not None and to.get('accepted_power_mean_pct') is not None:
            text += f', and its mean across the band from {frm["accepted_power_mean_pct"]:g} % to {to["accepted_power_mean_pct"]:g} %'
        text += '.'
    p = vc.get('prediction_from_previous')
    if p:
        text += f' Version {a} predicted {p["band_worst_s11_db"]:g} dB for these parts: {p["parts_text"]}.'
        if vc.get('applied') is False:
            text += f' Those parts are not what version {b} has, so the prediction was not tested.'
        elif vc.get('independent_check'):
            if vc.get('held'):
                text += f' A separate confirm run of version {b} gives {to["band_worst_s11_db"]:g} dB, within {vc["hold_tolerance_db"]:g} dB of it, so the prediction held.'
            else:
                text += f' A separate confirm run of version {b} gives {to["band_worst_s11_db"]:g} dB, not within {vc["hold_tolerance_db"]:g} dB of it, so the prediction did not hold.'
        elif vc.get('consistent'):
            text += (f' Consistent with the prediction: same solver run, recalculated with version {b}\'s parts ({to["band_worst_s11_db"]:g} dB against {p["band_worst_s11_db"]:g} dB predicted,'
                     f' within {vc["hold_tolerance_db"]:g} dB). It is not an independent result, and it is simulated, not a lab measurement.')
        else:
            text += (f' Not consistent with the prediction ({to["band_worst_s11_db"]:g} dB against {p["band_worst_s11_db"]:g} dB predicted, more than {vc["hold_tolerance_db"]:g} dB apart),'
                     f' although it is the same solver run recalculated with version {b}\'s parts. Check that the fitted parts are the recommended ones. It is simulated, not a lab measurement.')
    text += (f' The tuning target of {vc["target_db"]:g} dB is met.' if vc['target_met'] else f' The tuning target of {vc["target_db"]:g} dB is not met.')
    return text


def quantities(vc: dict | None) -> list[Quantity]:
    """The numbers of a version change, which a summary or finding of the recheck may quote."""
    if not vc:
        return []
    out: list[Quantity] = []
    add = lambda value, unit: out.extend(q for q in [make_quantity(value, unit)] if q) if isinstance(value, (int, float)) else None
    for side in (vc['from'], vc['to'], vc.get('prediction_from_previous') or {}):
        add(side.get('band_worst_s11_db'), 'dB')
        add(side.get('band_mean_s11_db'), 'dB')
        add(side.get('accepted_power_worst_pct'), '%')
        add(side.get('accepted_power_mean_pct'), '%')
    for key, unit in (('gain_db', 'dB'), ('difference_db', 'dB'), ('hold_tolerance_db', 'dB'), ('target_db', 'dB'), ('accepted_power_gain_worst_pct', '%'), ('accepted_power_gain_mean_pct', '%')):
        add(vc.get(key), unit)
        if isinstance(vc.get(key), (int, float)):
            add(abs(vc[key]), unit)
    out.extend(allowed_from_text((vc.get('prediction_from_previous') or {}).get('parts_text') or ''))
    for part in (vc.get('prediction_from_previous') or {}).get('parts') or []:
        out.extend(allowed_from_text(str(part.get('was') or '')) + allowed_from_text(str(part.get('now') or '')))
    return out


def previous_quantities(store, older_review_id: str | None) -> list[Quantity]:
    """Numbers of the previous review's as-built result and prediction, which the recheck may quote before its own base exists."""
    if not older_review_id:
        return []
    frm = as_built(store.list_evidence(older_review_id))
    if not frm:
        return []
    shell = {'from': frm, 'to': {}, 'prediction_from_previous': prediction_of(store, older_review_id)}
    return quantities(shell)


_SENTENCE = re.compile(r'(?<=[.!?])\s+')
_LEAD_SENTENCES = 6
_LEAD_CHARS = 1100
_WORDS = {'improved': re.compile(r'improv|gain|better|rose|up from|recover', re.I), 'worse': re.compile(r'worse|worsen|lost|loss|degrad|regress|fell|dropped|down from', re.I),
          'unchanged': re.compile(r'unchanged|did not change|no change|same', re.I)}
_HELD = re.compile(r'\b(?:held|hold|holds|consistent|predict\w*)\b', re.I)
_SAYS_HELD = re.compile(r'\b(?:prediction|predicted)\b[^.]{0,60}\b(?:held|confirmed|verified)\b|\b(?:held|confirmed|verified)\b[^.]{0,40}\bprediction\b', re.I)
_TARGET_MET = re.compile(r'target\b[^.]{0,40}\bis\s+met\b|\bmeets?\s+the\s+(?:tuning\s+)?target|\btarget\s+(?:was\s+)?met\b', re.I)
_DENIES_GAIN = re.compile(r'not a partial win|no improvement|did not improve|not improved', re.I)


def lead_problem(summary: str, vc: dict | None) -> str | None:
    """A recheck summary opens with what changed since the previous version. None when it does; otherwise what is missing and the sentence to use."""
    if not vc:
        return None
    sentences = _SENTENCE.split((summary or '').strip())
    lead = ' '.join(sentences[:_LEAD_SENTENCES])[:_LEAD_CHARS]
    first = ' '.join(sentences[:2])[:400]                  # the change itself comes first
    a, frm, to = vc['from_version'], vc['from'], vc['to']
    missing = []
    if not re.search(rf'\bversion\s+{a}\b', first, re.I):
        missing.append(f'name the previous version in the first sentences ("compared with version {a}")')
    written = [q for q in extract(first) if q.dimension == 'dB']
    close = lambda value: any(abs(q.base - value) <= max(q.tolerance, 0.005) or abs(abs(q.base) - abs(value)) <= max(q.tolerance, 0.005) for q in written)
    absent = [f'{v:g} dB' for v in (frm['band_worst_s11_db'], to['band_worst_s11_db']) if not close(v)]
    if absent:
        missing.append('give the as-built worst in-band S11 before and after (' + ' and '.join(absent) + ' missing)')
    if not _WORDS[vc['direction']].search(first):
        missing.append(f'say that it {vc["direction"] if vc["direction"] != "worse" else "got worse"}')
    if vc['direction'] == 'improved' and _DENIES_GAIN.search(lead):
        missing.append('not deny the improvement; it is a gain against the previous version even though the target is missed')
    if vc.get('prediction_from_previous') and vc.get('applied') is not False and not _HELD.search(lead):
        missing.append(f'say whether the result is consistent with the prediction from version {a}' + ('' if vc.get('independent_check') else
                       ' (it is the same solver run recalculated with the new parts, not an independent check)'))
    if re.search(r'\bmeasured\b(?:[^.]|\.\d){0,80}\bdB\b|\bdB\b(?:[^.]|\.\d){0,80}\bmeasured\b', lead, re.I):
        missing.append('not call a simulated S11 "measured" (say "simulated" or "calculated"; only KiCad checks and analyzers measure)')
    if vc.get('prediction_from_previous') and vc.get('applied') is not False and not vc.get('independent_check') and _SAYS_HELD.search(lead):
        missing.append('not say the prediction "held" or was confirmed (this is the same solver run with the new parts: say it is consistent with the prediction)')
    if vc['target_met'] and not _TARGET_MET.search(lead):
        missing.append('say the target is met')
    if not vc['target_met'] and not partial.NOT_MET.search(lead):
        missing.append('say plainly that the target is not met')
    if not missing:
        return None
    return ('A recheck leads with what changed since the previous version, then the findings. Your opening sentences must ' + '; '.join(missing) + '. Use this lead: ' + vc['text'])


# ---- findings against the previous review ---------------------------------------------------------------------------------

def metric_direction(name: str, old: float, new: float) -> str | None:
    """Better or worse for the measurements whose direction is known (S11 lower is better, accepted power higher). None for the others."""
    low = name.lower()
    if 's11' in low and ('worst' in low or 'mean' in low or 'min' in low or 'band' in low):
        better = new < old
    elif 'accepted_power' in low:
        better = new > old
    else:
        return None
    return 'improved' if better else 'worse'
