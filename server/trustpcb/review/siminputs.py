"""The inputs of a board simulation, as the review sets them.

Each simulator describes what it needs in `inputs_needed()`. The shapes differ a little: a scalar with an `id` and a dotted `path` (power), a scalar with a
`name` (thermal, signal), or a container whose entries are keyed by a part or a net (`part_power`, `net_currents`, `rise_time_ns`, `ldo.U1`). This module turns
all of them into one flat list of inputs a review can set by id, and builds the nested `inputs` dict the simulator reads.

An input is a fact (a current, a power, a voltage: it needs a source) or a choice (which nets to analyse: it needs none). The mesh settings are the server's
(the quality of a run), so they are not offered.
"""
from __future__ import annotations

from typing import Any

HIDDEN = frozenset({'quality', 'max_cells', 'resolution_mm', 'pitch_mm', 'convergence_tol', 'time_budget_s', 'use_defaults', 'mesh', 'sources', 'parts', 'track_watts', 'extra_sources'})
CHOICES = frozenset({'nets', 'max_nets', 'driver_ref', 'include_return'})
RICH = {'currents': 'amps', 'part_power': 'watts'}          # containers whose entries carry their own source: {amps|watts, source, cite}
MAX_LISTED = 40


def split_path(path: str) -> tuple[str, ...]:
    """'currents.U3@+3.3V' -> ('currents', 'U3@+3.3V'): the first dot only, because a net name may hold dots."""
    head, _, rest = str(path).partition('.')
    return (head, rest) if rest else (head,)


def normalize(spec: list[dict]) -> list[dict]:
    """The settable inputs of a simulator, flat. Each: id, label, unit, default, source, cite, needed, choice, path, container (when it takes entries by key)."""
    out: list[dict] = []
    for item in spec:
        ident = item.get('id') or item.get('name')
        if not ident or ident in HIDDEN or str(ident).split('.')[0] in HIDDEN:
            continue
        default = item['default'] if 'default' in item else item.get('value')
        base = {'label': item.get('label') or str(ident).replace('_', ' '), 'unit': item.get('unit') or '', 'source': item.get('source') or 'default', 'cite': item.get('cite') or item.get('help'),
                'needed': bool(item.get('needed') or item.get('required')), 'choice': ident in CHOICES}
        if isinstance(default, dict):
            if str(ident).startswith('ldo.') and default:
                for field in default:
                    out.append({**base, 'id': f'{ident}.{field}', 'path': (str(ident).split('.')[0], str(ident).split('.', 1)[1], field), 'default': default[field],
                                'unit': {'vin_v': 'V', 'vout_v': 'V', 'load_a': 'A', 'iq_a': 'A'}.get(field, ''), 'label': f'{ident} {field}'})
            else:
                out.append({**base, 'id': str(ident), 'path': (str(ident),), 'default': None, 'container': True,
                            'label': f'{base["label"]} (set one entry: {ident}.<key>)'})
            continue
        path = split_path(item['path']) if item.get('path') else (str(ident),)
        out.append({**base, 'id': str(ident), 'path': path, 'default': default})
    return out


def resolve(items: list[dict], ident: str) -> dict | None:
    """The input an id names. An entry of a container (`part_power.U1`) is resolved from the container."""
    for item in items:
        if item['id'] == ident and not item.get('container'):
            return item
    for item in items:
        if item.get('container') and ident.startswith(item['id'] + '.') and len(ident) > len(item['id']) + 1:
            key = ident[len(item['id']) + 1:]
            unit = {'part_power': 'W', 'net_currents': 'A', 'currents': 'A'}.get(item['id'], item['unit'])
            return {**item, 'id': ident, 'path': (item['id'], key), 'container': False, 'unit': unit, 'label': f'{item["id"].replace("_", " ")} of {key}'}
    return None


def set_path(root: dict, path: tuple[str, ...], value: Any) -> None:
    node = root
    for key in path[:-1]:
        node = node.setdefault(key, {})
    node[path[-1]] = value


def build_inputs(stored: dict[str, dict]) -> dict:
    """The nested inputs dict for a simulator from the inputs a review set (id -> {value|names|text, source, cite, path})."""
    out: dict = {}
    for ident, item in sorted(stored.items()):
        path = tuple(item['path'])
        value = item['value'] if 'value' in item else (item.get('names') if item.get('names') is not None else item.get('text'))
        key = RICH.get(path[0])
        if key and item.get('source') in ('user', 'datasheet') and len(path) == 2:
            value = {key: value, 'source': item['source'], 'cite': item.get('cite') or ('datasheet' if item['source'] == 'datasheet' else 'stated by the engineer')}
        set_path(out, path, value)
    return out


def listed(items: list[dict]) -> tuple[list[dict], int]:
    """At most MAX_LISTED inputs for the agent, the ones that are needed first."""
    ordered = sorted(items, key=lambda i: (not i['needed'], i.get('container', False), i['id']))
    return ordered[:MAX_LISTED], max(0, len(ordered) - MAX_LISTED)
