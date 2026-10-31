"""Board simulations as evidence: metrics, assumptions, deltas and the trust verdict, as plain functions over a stored result.

A simulator (see `sims/base.py`) returns a `SimResult` as a dict. Every metric becomes a measurement of a cited evidence row, every
assumption becomes a line of a second evidence row, and a result that fails its trust checks stays untrusted: the finding engine refuses to
build a finding on it. Nothing here runs a simulation; the Lab does that.

A metric may carry optional hints from its simulator: `primary` (the number an experiment is judged by), `better` ('lower' or 'higher',
lower is the default) and `limit` (a number the board should stay within, with `limit_label` saying where it comes from). Without a
`primary` flag the first numeric metric is the primary.
"""
from __future__ import annotations

from typing import Any

from .number_check import make_quantity

IMPROVEMENT_PCT = 10.0          # a what-if counts as an improvement when the primary metric gets at least this much better
NO_PROGRESS_PCT = 2.0           # two experiments in a row that move the primary metric by less than this close the suspect
CONFIRM_AGREE_PCT = 15.0        # screen and confirm agree when the primary metric differs by less than this share
SIM_BASE_QUALITY = 'screen'
CONFIRM_MESH = 'fine'

BENIGN_UNITS = ('count', '-', '')
#: how a simulator writes a unit -> how the number check reads it
UNIT_NAMES = {'C': '°C', 'mOhm': 'mΩ', 'percent': '%', 'pct': '%', 'ohm': 'ohm'}
#: the number an experiment is judged by, when the simulator did not flag one: metric names in order of preference; `*suffix` picks the metric with the largest absolute value
PRIMARY_RULES = {'power': ('worst_drop_pct', 'worst_drop_mv'), 'thermal': ('peak_rise', 'peak_temperature'), 'signal': ('*.z0_worst_deviation', '*.reference_gap_length')}
#: a primary metric whose limit is an assumption of the same run (the percent drop is held to the allowed drop)
LIMIT_FROM_ASSUMPTION = {'worst_drop_pct': 'allowed voltage drop'}
ASSUME_WORDS = ('assum', 'default', 'estimat', 'if the', 'if it', 'provided', 'given a', 'given an', 'would', 'unless', 'conditional', 'not from your design', 'placeholder')


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == value


def _reason_of(check: dict) -> str:
    """Why a check failed, with what it measured against its limit, so the failure text shows the numbers and not only the rule."""
    name = str(check.get('name') or check.get('check') or '').replace('_', ' ')
    detail = str(check.get('detail') or check.get('message') or name or 'a check failed')
    value, limit = check.get('value'), check.get('limit')
    if value is None or value == '':
        return detail[:300]
    shown = f'{name or "check"}: measured {value}' + (f", limit {limit}" if limit not in (None, '') else '') + '. '
    return (shown + detail)[:420]


def trust_of(result: dict) -> dict:
    """The evidence trust object: `ok` only when the simulator said the result is usable and every check passed."""
    trust = result.get('trust') or {}
    status = trust.get('status')
    reasons = []
    for check in trust.get('checks') or []:
        if isinstance(check, dict):
            if check.get('ok') is False or check.get('passed') is False or check.get('status') in ('failed', 'fail'):
                reasons.append(_reason_of(check))
        elif isinstance(check, str) and check.startswith(('fail', 'FAIL')):
            reasons.append(check[:200])
    if result.get('status') != 'ok':
        return {'status': 'fail', 'reasons': [str(result.get('reason') or 'The simulation could not run.')[:300]]}
    if status != 'ok':
        return {'status': 'fail', 'reasons': reasons or ['The simulation failed its own checks.']}
    return {'status': 'ok', 'reasons': []}


def metric_items(result: dict) -> list[tuple[str, dict]]:
    return [(name, m) for name, m in (result.get('metrics') or {}).items() if isinstance(m, dict) and is_number(m.get('value'))]


def unit_of(m: dict) -> str:
    unit = str(m.get('unit') or '')
    return UNIT_NAMES.get(unit, unit)


def primary_of(result: dict) -> tuple[str, dict] | None:
    items = metric_items(result)
    for name, m in items:
        if m.get('primary'):
            return name, m
    by_name = dict(items)
    for rule in PRIMARY_RULES.get(result.get('kind') or '', ()):
        if rule.startswith('*'):
            hits = [(n, m) for n, m in items if n.endswith(rule[1:])]
            if hits:
                return max(hits, key=lambda hit: abs(float(hit[1]['value'])))
        elif rule in by_name:
            return rule, by_name[rule]
    for name, m in items:
        if unit_of(m) not in BENIGN_UNITS:
            return name, m
    return None


def measurements_of(result: dict) -> dict:
    """The metrics as evidence measurements, `{name: {value, unit}}`, so the number checks read them like any other measurement."""
    out = {}
    for name, m in metric_items(result):
        value = float(m['value'])
        out[name] = {'value': round(value, 6 if abs(value) < 1 else 4), 'unit': unit_of(m)}
    return out


def metrics_flat(result: dict) -> dict:
    """What an experiment remembers: the primary, its direction, the limit and every value."""
    primary = primary_of(result)
    out: dict[str, Any] = {'sim_kind': result.get('kind'), 'trust_status': trust_of(result)['status'], 'values': {n: float(m['value']) for n, m in metric_items(result)}}
    if primary:
        name, m = primary
        absolute = name.endswith('deviation')            # a deviation from a target is judged by its size, not its sign
        value = abs(float(m['value'])) if absolute else float(m['value'])
        out.update(primary=name, primary_value=value, unit=unit_of(m), better=m.get('better') or 'lower', label=m.get('label') or name.replace('_', ' '), **({'absolute': True} if absolute else {}))
        limit, label = m.get('limit'), m.get('limit_label')
        assumed = next((a for a in result.get('assumptions') or [] if isinstance(a, dict) and a.get('name') == LIMIT_FROM_ASSUMPTION.get(name)), None)
        if not is_number(limit) and assumed and is_number(assumed.get('value')):
            limit, label = assumed['value'], f'{assumed.get("name")} ({assumed.get("source")})'
        if is_number(limit):
            out.update(limit=float(limit), limit_label=label)
    return out


def gain_pct(new: dict, base: dict) -> float | None:
    """How much better the primary metric got, in percent of the base (positive is better). None when either has no primary."""
    a, b = new.get('primary_value'), base.get('primary_value')
    if a is None or b is None or new.get('primary') != base.get('primary'):
        return None
    if abs(b) < 1e-12:
        return 0.0
    change = (b - a) / abs(b) * 100.0
    return round(change if (new.get('better') or 'lower') == 'lower' else -change, 2)


def delta_of(new: dict, base: dict) -> dict | None:
    """The change against the base run: `gain_pct` (the key the ledger's loop rules read), the primary change and a delta for every shared metric."""
    gain = gain_pct(new, base)
    if gain is None:
        return None
    out: dict[str, Any] = {'gain_pct': gain, 'primary': new['primary'], 'primary_change': round(new['primary_value'] - base['primary_value'], 6), 'unit': new.get('unit', '')}
    changes = {}
    for name, value in (new.get('values') or {}).items():
        other = (base.get('values') or {}).get(name)
        if other is not None:
            changes[name] = round(value - other, 6)
    out['changes'] = changes
    return out


def delta_measurements(delta: dict, result: dict) -> dict:
    """`delta_<metric>` measurements in each metric's own unit, and the primary's gain in percent."""
    out = {}
    units = {n: unit_of(m) for n, m in metric_items(result)}
    for name, change in (delta.get('changes') or {}).items():
        if units.get(name) not in BENIGN_UNITS:
            out[f'delta_{name}'] = {'value': change, 'unit': units[name]}
    if delta.get('gain_pct') is not None:
        out['delta_primary_gain'] = {'value': delta['gain_pct'], 'unit': '%'}
    return out


def meets_limit(flat: dict) -> bool | None:
    """Is the primary metric within its limit? None when the simulator gave no limit."""
    if flat.get('limit') is None or flat.get('primary_value') is None or flat.get('trust_status') != 'ok':
        return None
    if (flat.get('better') or 'lower') == 'lower':
        return flat['primary_value'] <= flat['limit']
    return flat['primary_value'] >= flat['limit']


def screen_confirm_agree(screen: dict, confirm: dict) -> tuple[bool, float | None]:
    a, b = screen.get('primary_value'), confirm.get('primary_value')
    if a is None or b is None:
        return False, None
    if abs(b) < 1e-12:
        return abs(a) < 1e-12, 0.0
    diff = abs(a - b) / abs(b) * 100.0
    return diff <= CONFIRM_AGREE_PCT, round(diff, 2)


# ---- assumptions ---------------------------------------------------------------------------------------------------------------

def assumption_dicts(result: dict, items: dict | None = None, labels: dict | None = None) -> list[dict]:
    """The assumptions of a result as plain dicts. `items` are the inputs this review set (id -> {value, unit, source, cite, label}): when the simulator's own
    assumption has the same value and unit, it carries the source the review recorded (a datasheet value stays a datasheet value); an input the simulator
    did not echo back is added, so every value that went in is listed."""
    out = []
    used: set = set()
    for a in result.get('assumptions') or []:
        item = dict(a) if isinstance(a, dict) else {}
        if not item.get('name'):
            continue
        item.setdefault('source', 'default')
        for ident, given in (items or {}).items():
            if ident in used or not is_number(item.get('value')) or not is_number(given.get('value')):
                continue
            if abs(float(item['value']) - float(given['value'])) <= 1e-9 + 1e-6 * abs(float(given['value'])) and unit_name(item.get('unit')) == unit_name(given.get('unit')):
                item.update(source=given['source'], cite=given.get('cite') or item.get('cite'), label=given.get('label') or item.get('label'))
                used.add(ident)
                break
        out.append({**{k: item.get(k) for k in ('name', 'value', 'source', 'cite')}, 'unit': unit_name(item.get('unit')), 'label': item.get('label') or (labels or {}).get(item['name'])})
    for ident, given in (items or {}).items():
        if ident not in used and given.get('source') in ('user', 'datasheet'):
            out.append({'name': ident, 'value': given['value'], 'unit': unit_name(given.get('unit')), 'source': given['source'], 'cite': given.get('cite'), 'label': given.get('label') or (labels or {}).get(ident)})
    return out


def unit_name(unit: Any) -> str:
    unit = str(unit or '')
    return UNIT_NAMES.get(unit, unit)


def assumption_label(a: dict) -> str:
    return str(a.get('label') or a['name']).replace('_', ' ')


def assumption_text(a: dict) -> str:
    value = a.get('value')
    shown = f'{value:g}' if is_number(value) else str(value)
    return f'{assumption_label(a)} {shown}{(" " + a["unit"]) if a.get("unit") else ""}'


SOURCE_TEXT = {'default': 'default, not from your design', 'user': 'given by the engineer', 'datasheet': 'from a datasheet', 'board': 'read from the board'}


def assumption_measurements(assumptions: list[dict]) -> dict:
    out = {}
    for a in assumptions:
        if is_number(a.get('value')) and a.get('unit') and make_quantity(float(a['value']), str(a['unit'])):
            out['assume_' + a['name']] = {'value': float(a['value']), 'unit': str(a['unit'])}
    return out


DECISIVE_UNITS = ('A', 'mA', 'W', 'mW', 'V', 'mV')
DECISIVE_WORDS = ('current', 'power', 'load', 'ambient', 'rise time', 'swing', 'voltage of')


def is_decisive(a: dict) -> bool:
    """Does this input decide the number a run gives (a load current, a part's power, a voltage, the ambient, an edge time)? A default for a limit or a physical
    constant (the allowed drop, the copper temperature, the plating) is listed and labelled but does not make the result a statement about the default."""
    name = f'{a.get("name") or ""} {a.get("label") or ""}'.lower()
    return 'allowed' not in name and 'limit' not in name and (unit_name(a.get('unit')) in DECISIVE_UNITS or any(word in name for word in DECISIVE_WORDS))


def defaults_of(assumptions: list[dict]) -> list[dict]:
    return [a for a in assumptions if a.get('source') == 'default']


def decisive_defaults_of(assumptions: list[dict]) -> list[dict]:
    return [a for a in defaults_of(assumptions) if is_decisive(a)]


def assumption_line(a: dict) -> str:
    """The sentence a finding lists under assumptions for one input."""
    if a.get('source') == 'default':
        return f'Assumed default, not from your design: {assumption_text(a)}. Replace it with the real value and run the simulation again.'
    if a.get('source') == 'datasheet':
        return f'From a datasheet: {assumption_text(a)}' + (f' ({a["cite"]}).' if a.get('cite') else '.')
    if a.get('source') == 'user':
        return f'Given by the engineer: {assumption_text(a)}.'
    return f'Read from the board: {assumption_text(a)}.'


def run_values(*, kind: str, ref: str, label: str, quality: str, run_hash: str, result: dict, assumptions: list[dict], whatif: list[dict], inputs: dict, suspect: str | None,
               cached: bool, assumption_code: str | None = None, inputs_set: list | None = None) -> dict:
    """The stored values of a board-simulation evidence row."""
    primary = primary_of(result)
    trust = trust_of(result)
    measurements = measurements_of(result)
    detail = result.get('detail') or {}
    cross = [{k: c.get(k) for k in ('net', 'layer', 'width_mm', 'analyzer_closed_form_ohm', 'field_solver_on_the_real_board_median_ohm', 'real_board_difference_pct', 'why_they_differ')}
             for c in (detail.get('cross_check') or []) if isinstance(c, dict) and c.get('net')] if result.get('kind') == 'signal' else []
    for c in cross:         # the closed-form figure the impedance analyzer gave, beside the field-solved one, so a finding can quote either by its code
        if is_number(c.get('analyzer_closed_form_ohm')):
            measurements[f'{c["net"]}.analyzer_closed_form_z0'] = {'value': round(float(c['analyzer_closed_form_ohm']), 4), 'unit': 'ohm'}
    beyond = {n: info.get('quasi_static_limit_ghz') for n, info in (detail.get('nets') or {}).items() if isinstance(info, dict) and info.get('within_quasi_static_limit') is False} if result.get('kind') == 'signal' else {}
    return {
        'kind': 'simulated', 'sim_kind': kind, 'ref': ref, 'label': label, 'quality': quality, 'run_hash': run_hash, 'run_id': run_hash, 'suspect': suspect, 'trust': trust,
        'measurements': measurements, 'cross_check': cross, 'beyond_quasi_static': beyond, 'metrics': {n: {**{k: m.get(k) for k in ('value', 'label', 'better', 'limit', 'limit_label', 'primary') if m.get(k) is not None}, 'unit': unit_of(m)}
                                                              for n, m in metric_items(result)}, 'inputs_set': list(inputs_set or []),
        'primary': primary[0] if primary else None, 'whatif_ops': list(whatif), 'inputs': dict(inputs), 'assumptions': assumptions,
        'defaults_used': [a['name'] for a in defaults_of(assumptions)], 'decisive_defaults': [a['name'] for a in decisive_defaults_of(assumptions)], 'assumption_evidence': assumption_code, 'status': result.get('status'),
        'overlays': [{k: o.get(k) for k in ('id', 'label', 'unit', 'layer', 'vmin', 'vmax', 'scale')} for o in result.get('overlays') or [] if isinstance(o, dict)],
        'notes': [str(n)[:300] for n in (result.get('notes') or [])[:8]], 'solve_s': result.get('solve_s'), 'cached': cached,
        'reason': result.get('reason'),
    }


def assumption_values(*, kind: str, ref: str, run_hash: str, assumptions: list[dict]) -> dict:
    return {'kind': 'assumed', 'sim_kind': kind, 'for_ref': ref, 'run_hash': run_hash, 'label': f'inputs and assumptions of {ref}', 'assumptions': assumptions,
            'measurements': assumption_measurements(assumptions), 'trust': {'status': 'ok', 'reasons': []}}
