"""Evidence: what the findings stand on, and the class that follows from it.

The server, never the model, decides a finding's class (spec section 13):
KiCad or analyzer measurements are `measured`, a trusted simulation is `simulated`, nothing is `opinion`.
Values are stored as `{"measurements": {name: {value, unit}}, "kind": ..., ...}` so the number check
and the report read every kind of evidence the same way.
"""
from __future__ import annotations

from typing import Any

from .budgets import SCREEN_CONFIRM_DB, SCREEN_CONFIRM_HZ
from .number_check import Quantity, allowed_from_text, allowed_from_values, make_quantity

# evidence types
CANDIDATE, MEASUREMENT, SIMULATION, CONFIRM, COMPARISON, CALCULATION, PLACEMENT, REFERENCE = (
    'candidate', 'measurement', 'simulation', 'confirm', 'comparison', 'calculation', 'placement', 'reference')
SIMULATED_TYPES = (SIMULATION, CONFIRM, COMPARISON, CALCULATION)
MEASURED_TYPES = (CANDIDATE, MEASUREMENT)
NOT_EVIDENCE_FOR_FINDINGS = (PLACEMENT,)   # a what-if on a ghost part is not a fact about the board


def candidate_values(candidate: dict) -> dict:
    return {
        'kind': 'measured', 'candidate_id': candidate['id'], 'rule': candidate.get('rule'), 'title': candidate.get('title'),
        'source': candidate.get('source'), 'targets': list(candidate.get('targets', [])), 'severity_hint': candidate.get('severity_hint'),
        'measurements': candidate.get('measurements') or {}, 'details': (candidate.get('details') or [])[:5],
        'downgraded': bool(candidate.get('downgraded')),
    }


def metrics_from_result(run: dict, goal: dict | None = None) -> dict:
    """Plain numbers from a Lab run, in one stable shape.

    Accepts the worker's result.json under `result`, or the RF summary (`sim`, `trust` and `s11` at the top
    level). The band-worst S11 comes from the as-built result when the board's matching network is known,
    else from the antenna alone.
    """
    result = {**(run.get('sim') or {}), **(run.get('result') or {})}
    trust = run.get('trust') or result.get('trust') or {}
    bandwidth = result.get('bandwidth') or {}
    zin = result.get('zin_at_res') or {}
    s11 = run.get('s11') or {}
    stats = s11.get('as_built') or s11.get('antenna_only') or {}
    worst = result.get('band_worst_s11_db', stats.get('band_worst_db'))
    out: dict[str, Any] = {
        'f_res_hz': result.get('f_res'), 's11_min_db': result.get('s11_min_db'), 'bandwidth_hz': bandwidth.get('width_hz'),
        'dmax_dbi': result.get('dmax_dbi'), 'efficiency': result.get('efficiency_est'),
        'zin_re': zin.get('re'), 'zin_im': zin.get('im'), 'band_worst_s11_db': worst,
        'trust_status': trust.get('status'), 'trust_reasons': list(trust.get('reasons') or []), 'solve_s': run.get('solve_s') or result.get('solve_s'),
    }
    return {k: v for k, v in out.items() if v is not None or k.startswith('trust')}


def trusted(metrics: dict, run: dict | None = None) -> bool:
    return (run is None or run.get('status') == 'done') and metrics.get('trust_status') == 'ok'


def meets_target(metrics: dict, goal: dict, rf: dict | None = None) -> bool:
    """Does this result meet the tuning target? Good-enough rule: yes ends optimisation."""
    target = _target_db(goal, rf)
    if metrics.get('trust_status') != 'ok':
        return False
    worst = metrics.get('band_worst_s11_db')
    if worst is not None:
        return worst <= target
    f, s11 = metrics.get('f_res_hz'), metrics.get('s11_min_db')
    band = _band(goal, rf)
    if f is None or s11 is None:
        return False
    return s11 <= target and (band is None or band[0] <= f <= band[1])


def _target_db(goal: dict, rf: dict | None) -> float:
    for source in (goal, rf or {}):
        value = source.get('target_s11_db') if isinstance(source, dict) else None
        if isinstance(value, (int, float)):
            return float(value)
    return -10.0


def _band(goal: dict, rf: dict | None) -> tuple[float, float] | None:
    for source in (rf or {}, goal):
        band = source.get('band_hz') if isinstance(source, dict) else None
        if isinstance(band, (list, tuple)) and len(band) == 2 and all(isinstance(v, (int, float)) for v in band):
            return float(band[0]), float(band[1])
    return None


def objective(metrics: dict) -> float | None:
    """Smaller is better: the band-worst S11, else the deepest S11."""
    value = metrics.get('band_worst_s11_db')
    return value if value is not None else metrics.get('s11_min_db')


def delta(metrics: dict, base: dict) -> dict:
    """What a what-if changed against the base run: {s11_db, f_hz, bandwidth_hz, dmax_db}."""
    out: dict[str, float] = {}
    a, b = objective(metrics), objective(base)
    if a is not None and b is not None:
        out['s11_db'] = round(a - b, 3)
    if metrics.get('f_res_hz') is not None and base.get('f_res_hz') is not None:
        out['f_hz'] = round(metrics['f_res_hz'] - base['f_res_hz'], 1)
    if metrics.get('bandwidth_hz') is not None and base.get('bandwidth_hz') is not None:
        out['bandwidth_hz'] = round(metrics['bandwidth_hz'] - base['bandwidth_hz'], 1)
    if metrics.get('dmax_dbi') is not None and base.get('dmax_dbi') is not None:
        out['dmax_db'] = round(metrics['dmax_dbi'] - base['dmax_dbi'], 3)
    return out


def measurements_of(metrics: dict) -> dict:
    """Metrics as evidence measurements with units people read."""
    out = {}
    if metrics.get('f_res_hz') is not None:
        out['resonance'] = {'value': round(metrics['f_res_hz'] / 1e6, 3), 'unit': 'MHz'}
    if metrics.get('s11_min_db') is not None:
        out['s11_min'] = {'value': round(metrics['s11_min_db'], 2), 'unit': 'dB'}
    if metrics.get('band_worst_s11_db') is not None:
        out['s11_band_worst'] = {'value': round(metrics['band_worst_s11_db'], 2), 'unit': 'dB'}
    if metrics.get('bandwidth_hz') is not None:
        out['bandwidth'] = {'value': round(metrics['bandwidth_hz'] / 1e6, 2), 'unit': 'MHz'}
    if metrics.get('dmax_dbi') is not None:
        out['peak_directivity'] = {'value': round(metrics['dmax_dbi'], 2), 'unit': 'dBi'}
    if metrics.get('efficiency') is not None:
        out['radiation_efficiency'] = {'value': round(metrics['efficiency'] * 100, 1), 'unit': '%'}
    if metrics.get('zin_re') is not None:
        out['input_resistance'] = {'value': round(metrics['zin_re'], 1), 'unit': 'ohm'}
    if metrics.get('zin_im') is not None:
        out['input_reactance'] = {'value': round(metrics['zin_im'], 1), 'unit': 'ohm'}
    return out


def delta_measurements(d: dict) -> dict:
    out = {}
    if 's11_db' in d:
        out['delta_s11'] = {'value': d['s11_db'], 'unit': 'dB'}
    if 'f_hz' in d:
        out['delta_resonance'] = {'value': round(d['f_hz'] / 1e6, 3), 'unit': 'MHz'}
    if 'bandwidth_hz' in d:
        out['delta_bandwidth'] = {'value': round(d['bandwidth_hz'] / 1e6, 2), 'unit': 'MHz'}
    if 'dmax_db' in d:
        out['delta_peak_directivity'] = {'value': d['dmax_db'], 'unit': 'dB'}
    return out


def simulation_values(*, ref: str, label: str, quality: str, sc_hash: str, scenario: dict, run: dict, metrics: dict, suspect: str | None) -> dict:
    return {
        'kind': 'simulated', 'ref': ref, 'label': label, 'quality': quality, 'scenario_hash': sc_hash, 'suspect': suspect,
        'trust': {'status': metrics.get('trust_status'), 'reasons': metrics.get('trust_reasons', [])},
        'measurements': measurements_of(metrics), 'metrics': metrics, 'whatif_ops': scenario.get('whatif_ops', []),
        'material': scenario.get('material'), 'status': run.get('status'), 'solve_s': metrics.get('solve_s'),
        'curve': run.get('curve'), 'job_id': run.get('job_id'),
    }


def screen_confirm_agree(screen: dict, confirm: dict) -> tuple[bool, dict]:
    """Do a screen result and its confirm run agree within the limits (spec section 7)?"""
    d = delta(confirm, screen)
    agree = abs(d.get('s11_db', 0.0)) <= SCREEN_CONFIRM_DB and abs(d.get('f_hz', 0.0)) <= SCREEN_CONFIRM_HZ
    return agree, d


def quantities_of(row: dict) -> list[Quantity]:
    """Numbers a finding may quote from this evidence row, with the restrictions that apply to them."""
    values = row.get('values') or {}
    trust = (values.get('trust') or {}).get('status')
    kind = values.get('kind')
    note = ''
    if kind == 'simulated' and trust != 'ok':
        note = 'trust_failed'
    out: list[Quantity] = []
    for name, item in (values.get('measurements') or {}).items():
        extra = {}
        if note:
            extra['note'] = note
        elif kind == 'simulated' and values.get('quality') == 'screen' and not name.startswith('delta_'):
            extra['note'] = 'needs_confirm_run'   # screen runs are for comparing, not for absolute claims
        out.extend(allowed_from_values(item, **extra))
    for text_key in ('title',):
        if values.get(text_key):
            out.extend(allowed_from_text(str(values[text_key]), **({'note': note} if note else {})))
    for detail in values.get('details') or []:
        out.extend(allowed_from_text(str(detail)))
    return out


def derive_class(rows: list[dict]) -> str:
    """measured | simulated | opinion, from the evidence alone."""
    kinds = []
    for row in rows:
        values = row.get('values') or {}
        if row.get('type') in NOT_EVIDENCE_FOR_FINDINGS or row.get('type') == REFERENCE:
            continue
        if values.get('kind') == 'simulated' and (values.get('trust') or {}).get('status') == 'ok':
            kinds.append('simulated')
        elif values.get('kind') == 'measured':
            kinds.append('measured')
    if 'simulated' in kinds:
        return 'simulated'
    if 'measured' in kinds:
        return 'measured'
    return 'opinion'


def context_quantities(goal: dict | None, assumptions: list[dict], board: Any, brief: dict | None, rf: dict | None = None) -> list[Quantity]:
    """Numbers that are facts of the setup rather than measurements: the goal, assumptions, part values and board size."""
    out: list[Quantity] = []

    def add_text(value: Any) -> None:
        if isinstance(value, str):
            out.extend(allowed_from_text(value))
        elif isinstance(value, dict):
            if 'value' in value and 'unit' in value:
                out.extend(allowed_from_values(value))
            for item in value.values():
                add_text(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                add_text(item)
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            return

    add_text(goal or {})
    for item in assumptions:
        add_text(item.get('value'))
        add_text(item.get('name'))
    add_text(brief or {})
    band = (rf or {}).get('band_hz') or (goal or {}).get('band_hz')
    if isinstance(band, (list, tuple)) and len(band) == 2:
        for edge in band:
            quantity = make_quantity(float(edge) / 1e6, 'MHz')
            if quantity:
                out.append(quantity)
    target = (goal or {}).get('target_s11_db')
    if isinstance(target, (int, float)):
        out.append(make_quantity(float(target), 'dB'))
    for footprint in getattr(board, 'footprints', []) or []:
        out.extend(allowed_from_text(str(getattr(footprint, 'value', '') or '')))
    bbox = getattr(board, 'bbox', None)
    if bbox:
        for value in ((bbox[2] - bbox[0]), (bbox[3] - bbox[1])):
            quantity = make_quantity(round(value, 2), 'mm')
            if quantity:
                out.append(quantity)
    stackup = getattr(board, 'stackup', None) or {}
    total = stackup.get('total_thickness_mm')
    if isinstance(total, (int, float)):
        out.append(make_quantity(total, 'mm'))
    for layer in stackup.get('layers', []) or []:
        if isinstance(layer.get('thickness_mm'), (int, float)):
            out.append(make_quantity(layer['thickness_mm'], 'mm'))
    return [q for q in out if q]
