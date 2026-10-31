"""Evidence: what the findings stand on, and the class that follows from it.

The server, never the model, decides a finding's class:
KiCad or analyzer measurements are `measured`, a trusted simulation is `simulated`, nothing is `opinion`.
Values are stored as `{"measurements": {name: {value, unit}}, "kind": ..., ...}` so the number check
and the report read every kind of evidence the same way.
"""
from __future__ import annotations

from typing import Any

from .budgets import SCREEN_CONFIRM_DB, SCREEN_CONFIRM_RES_REL, gain_db, is_improvement, moved_little, rank_key  # noqa: F401
from .number_check import Quantity, allowed_from_text, allowed_from_values, make_quantity

MAX_DIPS = 10
FIXED_DIP_HZ = 12e6            # a dip that moves less than this between two runs has stayed where it was

# evidence types
CANDIDATE, MEASUREMENT, SIMULATION, CONFIRM, COMPARISON, CALCULATION, PLACEMENT, REFERENCE = (
    'candidate', 'measurement', 'simulation', 'confirm', 'comparison', 'calculation', 'placement', 'reference')
SIMULATED_TYPES = (SIMULATION, CONFIRM, COMPARISON, CALCULATION)
MEASURED_TYPES = (CANDIDATE, MEASUREMENT)
NOT_EVIDENCE_FOR_FINDINGS = (PLACEMENT,)   # a what-if on a ghost part is not a fact about the board


STATISTIC_KEYS = ('rule_totals', 'rule_summary', 'net_summary', 'class_summary', 'measured_capacitors', 'corridor', 'mounting_holes_in_path', 'feed_length', 'radiating_length',
                  'net_breakdown', 'stackup_assumptions', 'net_class', 'reference', 'reference_plane_missing', 'distinct_key', 'duplicate_of', 'net_pair')


def candidate_values(candidate: dict) -> dict:
    stats = {k: candidate[k] for k in STATISTIC_KEYS if candidate.get(k) is not None}
    return {**({'statistics': stats} if stats else {}), **_candidate_core(candidate)}


def _walk_numbers(node: Any, key: str = ''):
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _walk_numbers(v, str(k))
    elif isinstance(node, (list, tuple)):
        for v in node[:60]:
            yield from _walk_numbers(v, key)
    elif isinstance(node, (int, float)) and not isinstance(node, bool):
        yield key, node


def statistic_quantities(stats: Any, code: str) -> list[Quantity]:
    """Numbers of an analyzer's own statistics (class minimum and maximum, per-net totals) as quantities a text may quote, bound to the row."""
    out = []
    for key, value in _walk_numbers(stats):
        unit = 'mm' if key.endswith('_mm') else ('%' if key.endswith('_pct') else ('ohm' if key.endswith('_ohm') else None))
        if unit and (q := make_quantity(value, unit, source=code, name='stat_' + key)):
            out.append(q)
    return out


def statistic_counts(stats: Any) -> set[int]:
    return {int(v) for _, v in _walk_numbers(stats) if isinstance(v, int)} if stats else set()


def _candidate_core(candidate: dict) -> dict:
    return {
        'kind': 'measured', 'candidate_id': candidate['id'], 'rule': candidate.get('rule'), 'title': candidate.get('title'),
        'source': candidate.get('source'), 'targets': list(candidate.get('targets', [])), 'severity_hint': candidate.get('severity_hint'),
        'measurements': candidate.get('measurements') or {}, 'details': (candidate.get('details') or [])[:5],
        'downgraded': bool(candidate.get('downgraded')),
    }


def metrics_from_result(run: dict, goal: dict | None = None) -> dict:
    """Plain numbers from a Lab run, in one stable shape.

    Accepts the worker's result.json under `result`, or the RF summary (`sim`, `trust` and `s11` at the top
    level). The in-band numbers (worst, mean) come from the as-built result when the board's matching network is
    known, else from the antenna alone. The deepest dip is kept as a description only.
    """
    result = {**(run.get('sim') or {}), **(run.get('result') or {})}
    trust = run.get('trust') or result.get('trust') or {}
    bandwidth = result.get('bandwidth') or {}
    zin = result.get('zin_at_res') or {}
    s11 = run.get('s11') or {}
    stats = s11.get('as_built') or s11.get('antenna_only') or {}
    worst = result.get('band_worst_s11_db', stats.get('band_worst_db'))
    mean = result.get('band_mean_s11_db', stats.get('band_mean_db'))
    centre = run.get('zin_at_band_centre') or result.get('zin_at_band_centre') or {}
    out: dict[str, Any] = {
        'band_worst_accepted_power_pct': result.get('band_worst_accepted_power_pct', stats.get('band_worst_accepted_power_pct')),
        'band_mean_accepted_power_pct': result.get('band_mean_accepted_power_pct', stats.get('band_mean_accepted_power_pct')),
        'f_res_hz': result.get('f_res'), 's11_min_db': result.get('s11_min_db'), 'bandwidth_hz': bandwidth.get('width_hz'),
        'dmax_dbi': result.get('dmax_dbi'), 'efficiency': result.get('efficiency_est'), 'pattern_f_hz': result.get('pattern_f_hz'),
        'zin_re': zin.get('re'), 'zin_im': zin.get('im'), 'band_worst_s11_db': worst, 'band_mean_s11_db': mean,
        'centre_f_hz': centre.get('f_hz'), 'centre_re': centre.get('re'), 'centre_im': centre.get('im'),
        'trust_status': trust.get('status'), 'trust_reasons': list(trust.get('reasons') or []), 'solve_s': run.get('solve_s') or result.get('solve_s'),
    }
    # What the real RF lab adds: every dip (the deepest one alone is fragile), the first series resonance, and both band views.
    dips = [{'f_hz': d['f_hz'], 's11_db': d['s11_db']} for d in (result.get('s11_dips') or []) if isinstance(d, dict) and 'f_hz' in d and 's11_db' in d][:MAX_DIPS]
    if dips:
        out['dips'] = dips
    alone, built = s11.get('antenna_only') or {}, s11.get('as_built') or {}
    out.update(first_series_resonance_hz=result.get('first_series_resonance_hz'),
               antenna_only_band_worst_db=alone.get('band_worst_db'), antenna_only_band_best_db=alone.get('band_best_db'), antenna_only_band_mean_db=alone.get('band_mean_db'),
               as_built_band_worst_db=built.get('band_worst_db'), as_built_band_best_db=built.get('band_best_db'), as_built_band_mean_db=built.get('band_mean_db'),
               antenna_only_band_worst_accepted_power_pct=alone.get('band_worst_accepted_power_pct'), antenna_only_band_mean_accepted_power_pct=alone.get('band_mean_accepted_power_pct'),
               as_built_band_worst_accepted_power_pct=built.get('band_worst_accepted_power_pct'), as_built_band_mean_accepted_power_pct=built.get('band_mean_accepted_power_pct'),
               basis='as_built' if built else ('antenna_only' if alone else None), summary_text=run.get('summary_text'),
               plausibility=list(run.get('plausibility') or []) or None)
    return {k: v for k, v in out.items() if v is not None or k.startswith('trust')}


def reactance_character(re: float | None, im: float | None) -> str | None:
    """inductive, capacitive or resistive, from an impedance R + jX."""
    if im is None:
        return None
    magnitude = abs(complex(re or 0.0, im))
    if abs(im) < max(5.0, 0.1 * magnitude):
        return 'resistive'
    return 'inductive' if im > 0 else 'capacitive'


CHARACTER_MEANING = {
    'inductive': 'Inductive: the antenna looks electrically long at the band centre. Matching can often cancel this with a series capacitor or a smaller series inductor.',
    'capacitive': 'Capacitive: the antenna looks electrically short at the band centre. Matching can often cancel this with a series inductor.',
    'resistive': 'Nearly resistive: little reactance for matching to cancel. A poor match here comes from the resistance, not a reactance.',
}


def trusted(metrics: dict, run: dict | None = None) -> bool:
    return (run is None or run.get('status') == 'done') and metrics.get('trust_status') == 'ok'


def meets_target(metrics: dict, goal: dict, rf: dict | None = None) -> bool:
    """Does this result meet the tuning target across the whole band? Good-enough rule: yes ends optimisation.

    Judged by the worst in-band S11 only; a deep dip elsewhere does not count.
    """
    if metrics.get('trust_status') != 'ok':
        return False
    worst = metrics.get('band_worst_s11_db')
    return worst is not None and worst <= target_db(goal, rf)


def target_db(goal: dict, rf: dict | None = None) -> float:
    for source in (goal, rf or {}):
        value = source.get('target_s11_db') if isinstance(source, dict) else None
        if isinstance(value, (int, float)):
            return float(value)
    return -10.0


def objective(metrics: dict) -> float | None:
    """Smaller is better: the worst in-band S11 (as built), else the mean in band. The deepest dip is a last resort only."""
    for key in ('band_worst_s11_db', 'band_mean_s11_db'):
        if metrics.get(key) is not None:
            return metrics[key]
    return metrics.get('s11_min_db')


def delta(metrics: dict, base: dict) -> dict:
    """What a change did against the base run, judged in band: {s11_db (worst in band), band_mean_db, series_res_hz, f_hz (deepest dip, descriptive), ...}.

    Negative dB is better matching.
    """
    out: dict[str, float] = {}
    a, b = objective(metrics), objective(base)
    if a is not None and b is not None:
        out['s11_db'] = round(a - b, 3)
    if metrics.get('band_mean_s11_db') is not None and base.get('band_mean_s11_db') is not None:
        out['band_mean_db'] = round(metrics['band_mean_s11_db'] - base['band_mean_s11_db'], 3)
    if metrics.get('band_worst_accepted_power_pct') is not None and base.get('band_worst_accepted_power_pct') is not None:
        out['accepted_worst_pct'] = round(metrics['band_worst_accepted_power_pct'] - base['band_worst_accepted_power_pct'], 1)
    if metrics.get('band_mean_accepted_power_pct') is not None and base.get('band_mean_accepted_power_pct') is not None:
        out['accepted_mean_pct'] = round(metrics['band_mean_accepted_power_pct'] - base['band_mean_accepted_power_pct'], 1)
    if metrics.get('first_series_resonance_hz') is not None and base.get('first_series_resonance_hz') is not None:
        out['series_res_hz'] = round(metrics['first_series_resonance_hz'] - base['first_series_resonance_hz'], 1)
    if metrics.get('f_res_hz') is not None and base.get('f_res_hz') is not None:
        out['f_hz'] = round(metrics['f_res_hz'] - base['f_res_hz'], 1)
    if metrics.get('bandwidth_hz') is not None and base.get('bandwidth_hz') is not None:
        out['bandwidth_hz'] = round(metrics['bandwidth_hz'] - base['bandwidth_hz'], 1)
    if metrics.get('dmax_dbi') is not None and base.get('dmax_dbi') is not None:
        out['dmax_db'] = round(metrics['dmax_dbi'] - base['dmax_dbi'], 3)
    return out


def fixed_dips(dips_a: list[dict], dips_b: list[dict], tol_hz: float = FIXED_DIP_HZ) -> list[dict]:
    """Dips present at the same frequency in both runs. A dip that stays put while the antenna changes is probably a board mode."""
    out = []
    for a in dips_a or []:
        near = [b for b in dips_b or [] if abs(b['f_hz'] - a['f_hz']) <= tol_hz]
        if near:
            b = min(near, key=lambda d: abs(d['f_hz'] - a['f_hz']))
            out.append({'f_hz': (a['f_hz'] + b['f_hz']) / 2, 'moved_hz': round(b['f_hz'] - a['f_hz'], 1)})
    return out


ACCEPTED_POWER_NOTE = ('Accepted power is 100 x (1 - |S11|^2) at each frequency: the share of the source power that gets past the mismatch. '
                       'It is a mismatch figure only. It is not radiated power and not efficiency; loss in the board and the parts is not in it.')
FIXED_DIP_NOTE = ('A fixed dip stays at the same frequency when the antenna geometry changes, so it is not the antenna\'s own resonance. '
                  'Call it a dip of unknown origin. Rely on the in-band numbers and the impedance at the band centre, and use the first series resonance as the length-related indicator.')


def fixed_dip_measurements(fixed: list[dict]) -> dict:
    """The `fixed_dip_N` measurements of a comparison between two runs of different geometry."""
    return {f'fixed_dip_{index}': {'value': round(d['f_hz'] / 1e6, 1), 'unit': 'MHz'} for index, d in enumerate(fixed or [], start=1)}


def fixed_dips_between(metrics: dict, other: dict, ops_a: list | None, ops_b: list | None) -> list[dict]:
    """Dips that stayed where they were between two runs of the same quality and different geometry; empty when the geometry is the same."""
    if (ops_a or []) == (ops_b or []):
        return []
    return fixed_dips(metrics.get('dips') or [], other.get('dips') or [])


def measurements_of(metrics: dict) -> dict:
    """Metrics as evidence measurements with units people read."""
    out = {}
    if metrics.get('f_res_hz') is not None:
        out['resonance'] = {'value': round(metrics['f_res_hz'] / 1e6, 3), 'unit': 'MHz'}
    if metrics.get('s11_min_db') is not None:
        out['s11_min'] = {'value': round(metrics['s11_min_db'], 2), 'unit': 'dB'}
    if metrics.get('band_worst_s11_db') is not None:
        out['s11_band_worst'] = {'value': round(metrics['band_worst_s11_db'], 2), 'unit': 'dB'}
    if metrics.get('band_mean_s11_db') is not None:
        out['s11_band_mean'] = {'value': round(metrics['band_mean_s11_db'], 2), 'unit': 'dB'}
    for key in ('band_worst_accepted_power_pct', 'band_mean_accepted_power_pct',
                'antenna_only_band_worst_accepted_power_pct', 'antenna_only_band_mean_accepted_power_pct',
                'as_built_band_worst_accepted_power_pct', 'as_built_band_mean_accepted_power_pct'):
        if metrics.get(key) is not None:
            out[key] = {'value': round(metrics[key], 1), 'unit': '%'}
    if metrics.get('centre_re') is not None and metrics.get('centre_im') is not None:
        out['band_centre_resistance'] = {'value': round(metrics['centre_re'], 1), 'unit': 'ohm'}
        out['band_centre_reactance'] = {'value': round(metrics['centre_im'], 1), 'unit': 'ohm'}
        if metrics.get('centre_f_hz') is not None:
            out['band_centre_frequency'] = {'value': round(metrics['centre_f_hz'] / 1e6, 2), 'unit': 'MHz'}
    if metrics.get('bandwidth_hz') is not None:
        out['bandwidth'] = {'value': round(metrics['bandwidth_hz'] / 1e6, 2), 'unit': 'MHz'}
    if metrics.get('dmax_dbi') is not None:
        out['peak_directivity'] = {'value': round(metrics['dmax_dbi'], 2), 'unit': 'dBi'}
    if metrics.get('pattern_f_hz') is not None and (metrics.get('dmax_dbi') is not None or metrics.get('efficiency') is not None):
        out['pattern_frequency'] = {'value': round(metrics['pattern_f_hz'] / 1e6, 2), 'unit': 'MHz'}  # peak directivity and efficiency are both at this frequency
    if metrics.get('efficiency') is not None:
        out['radiation_efficiency'] = {'value': round(metrics['efficiency'] * 100, 1), 'unit': '%'}
    if metrics.get('zin_re') is not None:
        out['input_resistance'] = {'value': round(metrics['zin_re'], 1), 'unit': 'ohm'}
    if metrics.get('zin_im') is not None:
        out['input_reactance'] = {'value': round(metrics['zin_im'], 1), 'unit': 'ohm'}
    if metrics.get('antenna_only_band_worst_db') is not None:
        out['antenna_only_band_worst_s11'] = {'value': round(metrics['antenna_only_band_worst_db'], 2), 'unit': 'dB'}
    if metrics.get('as_built_band_worst_db') is not None:
        out['as_built_band_worst_s11'] = {'value': round(metrics['as_built_band_worst_db'], 2), 'unit': 'dB'}
    if metrics.get('as_built_band_mean_db') is not None:
        out['as_built_band_mean_s11'] = {'value': round(metrics['as_built_band_mean_db'], 2), 'unit': 'dB'}
    if metrics.get('antenna_only_band_mean_db') is not None:
        out['antenna_only_band_mean_s11'] = {'value': round(metrics['antenna_only_band_mean_db'], 2), 'unit': 'dB'}
    if metrics.get('first_series_resonance_hz') is not None:
        out['first_series_resonance'] = {'value': round(metrics['first_series_resonance_hz'] / 1e6, 1), 'unit': 'MHz'}
    for index, dip in enumerate(metrics.get('dips') or [], start=1):
        out[f's11_dip_{index}'] = {'value': round(dip['f_hz'] / 1e6, 1), 'unit': 'MHz'}
        out[f's11_dip_{index}_depth'] = {'value': round(dip['s11_db'], 2), 'unit': 'dB'}
    return out


def delta_measurements(d: dict) -> dict:
    out = {}
    if 's11_db' in d:
        out['delta_s11'] = {'value': d['s11_db'], 'unit': 'dB'}
    if 'band_mean_db' in d:
        out['delta_band_mean'] = {'value': d['band_mean_db'], 'unit': 'dB'}
    if 'accepted_worst_pct' in d:
        out['delta_band_worst_accepted_power'] = {'value': d['accepted_worst_pct'], 'unit': '%'}
    if 'accepted_mean_pct' in d:
        out['delta_band_mean_accepted_power'] = {'value': d['accepted_mean_pct'], 'unit': '%'}
    if 'series_res_hz' in d:
        out['delta_first_series_resonance'] = {'value': round(d['series_res_hz'] / 1e6, 3), 'unit': 'MHz'}
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
    """Do a screen result and its confirm run agree within the limits? Judged in band, with the first series resonance as a second check."""
    d = delta(confirm, screen)
    agree = abs(d.get('s11_db', 0.0)) <= SCREEN_CONFIRM_DB
    a, b = screen.get('first_series_resonance_hz'), confirm.get('first_series_resonance_hz')
    if a and b and abs(a - b) / b > SCREEN_CONFIRM_RES_REL:
        agree = False
    return agree, d


def quantities_of(row: dict) -> list[Quantity]:
    """Numbers a finding may quote from this evidence row, with the restrictions that apply to them."""
    values = row.get('values') or {}
    trust = (values.get('trust') or {}).get('status')
    kind = values.get('kind')
    note = ''
    if kind == 'simulated' and trust != 'ok':
        note = 'trust_failed'
    code = row.get('code') or ''
    out: list[Quantity] = []
    for name, item in (values.get('measurements') or {}).items():
        extra = {'source': code, 'name': name}
        if note:
            extra['note'] = note
        elif kind == 'simulated' and values.get('quality') == 'screen' and not name.startswith('delta_'):
            extra['note'] = 'needs_confirm_run'   # screen runs are for comparing, not for absolute claims
        out.extend(allowed_from_values(item, **extra))
    for op in values.get('whatif_ops') or []:      # the geometry change that was simulated is an input the finding may state ("shorten by 7 mm")
        numbers = [op['delta_mm'], abs(op['delta_mm'])] if isinstance(op.get('delta_mm'), (int, float)) else []
        numbers += [v for v in op.get('bbox') or [] if isinstance(v, (int, float))]
        for number in numbers:
            out.extend(allowed_from_values({'value': number, 'unit': 'mm'}, name='whatif_op'))    # an input of the test, not a result: no code binding
    for text_key in ('title',):
        if values.get(text_key):
            out.extend(allowed_from_text(str(values[text_key]), source=code, name='title', **({'note': note} if note else {})))
    for detail in values.get('details') or []:
        out.extend(allowed_from_text(str(detail), source=code, name='detail'))
    out.extend(statistic_quantities(values.get('statistics'), code))
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
