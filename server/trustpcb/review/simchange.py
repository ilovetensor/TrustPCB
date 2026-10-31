"""Board-simulation results compared across versions, where both reviews had the same simulation in their extent.

A recheck compares what the previous version's review simulated with what this one simulated, kind by kind (power, thermal, signal). The comparison is
honest about what it can say: the numbers are comparable only when the runs have the same quality (screen or confirm) and the same inputs; with
different inputs the difference mixes the board with the inputs, and the report says so instead of calling it better or worse.
"""
from __future__ import annotations

from . import evidence as ev
from . import extent as ext
from . import simmetrics as sm


def results_of(store, review_id: str) -> dict[str, dict]:
    """Per kind, the unmodified base run to compare: the confirm-quality one when it exists, else the screen one. Trusted runs only."""
    out: dict[str, dict] = {}
    for row in store.list_evidence(review_id):
        v = row['values'] or {}
        if row['type'] != ev.BOARD_SIM or not v.get('sim_kind') or v.get('comparison') or v.get('whatif_ops') or (v.get('trust') or {}).get('status') != 'ok':
            continue
        metric = (v.get('metrics') or {}).get(v.get('primary')) or {}
        if not v.get('primary') or not sm.is_number(metric.get('value')):
            continue
        item = {'kind': v['sim_kind'], 'code': row['code'], 'quality': v.get('quality'), 'primary': v['primary'], 'value': float(metric['value']), 'unit': metric.get('unit') or '',
                'better': metric.get('better') or 'lower', 'label': metric.get('label') or v['primary'].replace('_', ' '), 'inputs': v.get('inputs') or {},
                'defaults': v.get('defaults_used') or [], 'assumptions': v.get('assumptions') or []}
        have = out.get(v['sim_kind'])
        if have is None or (item['quality'] == 'confirm' and have['quality'] != 'confirm'):
            out[v['sim_kind']] = item
    return out


def compare(store, review_id: str, previous_review_id: str) -> list[dict]:
    """The change of each simulation both reviews chose and ran, previous version to this one."""
    now_review, before_review = store.get_review(review_id) or {}, store.get_review(previous_review_id) or {}
    shared = [k for k in ext.sim_kinds_of(now_review.get('extent')) if k in ext.sim_kinds_of(before_review.get('extent'))]
    before, now = results_of(store, previous_review_id), results_of(store, review_id)
    out = []
    for kind in shared:
        a, b = before.get(kind), now.get(kind)
        if a is None or b is None:
            out.append({'kind': kind, 'label': ext.LABELS[kind], 'direction': 'not_compared', 'text': f'{ext.LABELS[kind]} was chosen in both reviews, but ' + (
                'the previous review has no trusted base run to compare.' if a is None else 'this review has no trusted base run to compare.')})
            continue
        same_inputs, same_quality = a['inputs'] == b['inputs'] and a['primary'] == b['primary'], a['quality'] == b['quality']
        gain = sm.gain_pct({'primary': b['primary'], 'primary_value': b['value'], 'better': b['better']}, {'primary': a['primary'], 'primary_value': a['value'], 'better': a['better']}) if a['primary'] == b['primary'] else None
        if gain is None or not (same_inputs and same_quality):
            direction = 'not_comparable'
        else:
            direction = 'better' if gain >= sm.NO_PROGRESS_PCT else ('worse' if gain <= -sm.NO_PROGRESS_PCT else 'same')
        shown = lambda x: f'{round(x["value"], 4):g} {x["unit"]}'.strip()
        text = f'{b["label"]}: {shown(a)} ({a["code"]}, {a["quality"]}) in the previous version, {shown(b)} ({b["code"]}, {b["quality"]}) now'
        if direction == 'not_comparable':
            reasons = [r for r, bad in (('the inputs differ', not same_inputs), ('the runs have different quality', not same_quality)) if bad]
            text += f'. Not comparable: {" and ".join(reasons) or "the primary metric differs"}, so the difference mixes the board with the setup.'
        else:
            text += f': {direction}' + (f' by {abs(gain):g} %' if direction != 'same' else '') + '. Both simulated, same inputs and quality.'
        out.append({'kind': kind, 'label': ext.LABELS[kind], 'direction': direction, 'gain_pct': gain, 'previous': a, 'now': b, 'same_inputs': same_inputs, 'same_quality': same_quality, 'text': text})
    return out


def previous_text(store, previous: dict, extent: list[str] | None) -> str:
    """For the recheck briefing: what the previous version's review simulated, for the simulations this review chose too."""
    kinds = [k for k in ext.sim_kinds_of(extent) if k in ext.sim_kinds_of(previous['review'].get('extent'))]
    results = results_of(store, previous['review']['id'])
    lines = []
    for kind in kinds:
        r = results.get(kind)
        if r is None:
            continue
        inputs = ', '.join(f'{a["name"]} {a["value"]:g} {a.get("unit") or ""} ({a["source"]})'.strip() if sm.is_number(a['value']) else f'{a["name"]} {a["value"]} ({a["source"]})' for a in r['assumptions'][:8])
        lines.append(f'- {ext.LABELS[kind]}: {r["label"]} {round(r["value"], 4):g} {r["unit"]} ({r["code"]}, {r["quality"]}, simulated). Inputs: {inputs or "none recorded"}.')
    if not lines:
        return ''
    return ('Version ' + str(previous['version_number']) + ' simulated these (the same extent is chosen now):\n' + '\n'.join(lines)
            + '\nRun the unmodified base of each again with the same inputs (set_sim_input with the same sources) so the two versions can be compared; a difference with other inputs is not a change in the board.')
