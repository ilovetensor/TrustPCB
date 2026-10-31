"""A board-simulation result as the web app reads it.

The simulators write one `result.json`: metrics, trust, overlays, assumptions and a free-form `detail`. The app wants rows grouped by what they are about (a
load, a part, a net), curves as `series`, trust checks as `{check, ok, detail}`, and assumption names that begin with the part they belong to. This module
maps one to the other. Nothing is added that the result does not say: a row exists only when the simulator's own detail has it.
"""
from __future__ import annotations

import re
from typing import Any

from . import simmetrics as sm

MAX_ROWS = 60
_CURRENT_ON = re.compile(r'^current (\S+?)(?: (?:on|@) (\S+))?$')
_POWER_OF = re.compile(r'^power (\S+)$')
_UNIT_VIEW = {'ohm': 'Ω', 'C': '°C', 'mOhm': 'mΩ', 'percent': '%'}


def _unit(unit: Any) -> str:
    return _UNIT_VIEW.get(str(unit or ''), str(unit or ''))


def trust_view(trust: dict | None, status: str | None = None) -> dict:
    checks = []
    for c in (trust or {}).get('checks') or []:
        if isinstance(c, dict):
            name = c.get('check') or c.get('name') or c.get('id') or 'check'
            ok = c.get('ok') if isinstance(c.get('ok'), bool) else c.get('status') != 'failed'
            label = str(name).replace('_', ' ')
            if c.get('net'):                          # a power run checks each net on its own: say which, so the rows differ
                label += f' ({c["net"]}{", ground return" if c.get("return_path") else ""})'
            checks.append({'check': label, 'ok': ok, **({'detail': str(c['detail'])[:300]} if c.get('detail') else {})})
    return {'status': (trust or {}).get('status') if (trust or {}).get('status') in ('ok', 'failed') else None, 'checks': checks}


def assumption_view(assumptions: list[dict]) -> list[dict]:
    """Names begin with the part they are about ("F1 current on VBUS", "U1 power"), so the app can show the source beside a part."""
    out = []
    for a in assumptions or []:
        name = str(a.get('name') or '')
        if m := _CURRENT_ON.match(name):
            name = f'{m.group(1)} current' + (f' on {m.group(2)}' if m.group(2) else '')
        elif m := _POWER_OF.match(name):
            name = f'{m.group(1)} power'
        out.append({'name': name, 'value': a.get('value'), 'unit': _unit(a.get('unit')), 'source': a.get('source') if a.get('source') in ('datasheet', 'user', 'default', 'board') else 'default', 'cite': a.get('cite')})
    return out


def _rows_power(detail: dict) -> dict:
    rows: dict[str, dict] = {}
    for net, info in (detail.get('nets') or {}).items():
        vnom = info.get('nominal_v')
        for load in info.get('loads') or []:
            if not sm.is_number(load.get('total_drop_mv')) or not (load.get('amps') or 0) > 0:
                continue
            key = f'load.{load["ref"]}'
            key = key if key not in rows else f'{key}@{net}'
            limit = load.get('allowed_drop_pct')
            rows[key] = {'value': load['total_drop_mv'], 'unit': 'mV', 'label': f'{load["ref"]} on {net}', 'group': 'load', 'ref': load['ref'], 'net': net, 'current': load['amps'], 'current_unit': 'A',
                         **({'limit': round(limit / 100.0 * vnom * 1000.0, 4)} if sm.is_number(limit) and sm.is_number(vnom) else {})}
    return rows


def _rows_thermal(detail: dict) -> dict:
    rows = {}
    for part in detail.get('parts') or []:
        if not sm.is_number(part.get('t_board_max_c')):
            continue
        limit = part.get('t_max_c') or part.get('tj_max_c')
        rows[f'part.{part["ref"]}'] = {'value': part['t_board_max_c'], 'unit': '°C', 'label': f'{part["ref"]} {part.get("value") or ""}'.strip(), 'group': 'part', 'ref': part['ref'],
                                       **({'power': part['power_w'], 'power_unit': 'W'} if sm.is_number(part.get('power_w')) else {}),
                                       **({'board': part['t_board_mean_c']} if sm.is_number(part.get('t_board_mean_c')) else {}), **({'limit': limit} if sm.is_number(limit) else {})}
    return rows


def _rows_signal(result: dict) -> dict:
    rows = {}
    metrics = result.get('metrics') or {}
    for net, info in ((result.get('detail') or {}).get('nets') or {}).items():
        median = (metrics.get(f'{net}.z0_median') or {}).get('value')
        dev = (metrics.get(f'{net}.z0_worst_deviation') or {}).get('value')
        if not sm.is_number(median):
            continue
        rows[f'net.{net}'] = {'value': median, 'unit': 'Ω', 'label': f'{net}: median impedance' + ('' if info.get('within_quasi_static_limit', True) else ' (above the quasi-static limit of the model)'), 'group': 'net', 'net': net,
                              **({'deviation': dev} if sm.is_number(dev) else {}), **({'limit': 10.0} if info.get('target_ohm') else {})}
    return rows


def series_view(result: dict) -> list[dict]:
    out = []
    for s in ((result.get('detail') or {}).get('series') or []):
        if isinstance(s, dict) and s.get('x') and s.get('y'):
            out.append({'id': s['id'], 'label': s.get('label') or s['id'], 'x_label': s.get('x_label'), 'x_unit': _unit(s.get('x_unit')), 'y_unit': _unit(s.get('y_unit')), 'x': s['x'], 'y': s['y'],
                        **({'target': s['target']} if sm.is_number(s.get('target')) else {}), **({'tolerance_pct': s['tolerance_pct']} if sm.is_number(s.get('tolerance_pct')) else {})})
    return out


def web_result(result: dict) -> dict:
    """The result for the app: numeric metrics (as the simulator wrote them) plus grouped rows from its detail, series, trust checks and renamed assumptions."""
    kind = result.get('kind')
    metrics = {}
    for name, m in (result.get('metrics') or {}).items():
        if isinstance(m, dict) and sm.is_number(m.get('value')) and sm.unit_of(m) not in ('count',):
            metrics[name] = {'value': m['value'], 'unit': _unit(m.get('unit')), 'label': m.get('label') or name.replace('_', ' ')}
    detail = result.get('detail') or {}
    extra = {'power': lambda: _rows_power(detail), 'thermal': lambda: _rows_thermal(detail), 'signal': lambda: _rows_signal(result)}.get(kind, dict)()
    metrics.update(dict(list(extra.items())[:MAX_ROWS]))
    if kind == 'signal':
        primary = sm.primary_of(result)
        if primary:
            metrics['worst_deviation'] = {'value': abs(float(primary[1]['value'])), 'unit': '%', 'label': 'Worst impedance deviation from the target'}
    if kind == 'signal':        # the per-net numbers of the model are grouped by their net rows; the long list stays in the report and the evidence
        metrics = {k: v for k, v in metrics.items() if '.' not in k or k.startswith('net.')}
    out = {'kind': kind, 'status': result.get('status'), 'reason': result.get('reason'), 'metrics': metrics, 'trust': trust_view(result.get('trust')),
           'overlays': result.get('overlays') or [], 'assumptions': assumption_view(result.get('assumptions') or []), 'notes': list(result.get('notes') or [])[:10], 'solve_s': result.get('solve_s') or 0}
    series = series_view(result)
    if series:
        out['series'] = series
    if kind == 'signal':
        out['cross_check'] = detail.get('cross_check')
    return out
