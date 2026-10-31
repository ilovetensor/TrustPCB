"""What the schematic file says, read for the checks that need it: pin names, do-not-populate flags and no-connect markers.

The board file names pads by number only, so "pin 3" of a regulator could be its input or its enable. The schematic keeps the symbol's pin names
and electrical types, whether a part is marked do-not-populate, and where the no-connect markers sit. ``schematic_info(board, ctx)`` reads the
``.kicad_sch`` beside the board (or the first one under ``ctx.version_dir``), once per context. With no schematic every answer is empty and
callers say nothing about it. Read only; nothing here changes a file.

* ``pins[(ref, number)]`` gives ``{name, type, x, y}``: the symbol's pin name, its electrical type and its position on the sheet;
* ``dnp[ref]`` is the schematic's do-not-populate flag, ``value[ref]`` the symbol value;
* ``no_connect`` holds the (x, y) of every no-connect marker, and ``nc_pins`` the (ref, number) pins that carry one.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..pcb import sexpr

NC_TOLERANCE_MM = 0.05          # a marker sits on the pin end; this much slack covers rounding in the file
_SUPPLY_NAME = re.compile(r'^(?:[AD]?V(?:DD|CC|IN|OUT|BUS|BAT|SYS|REG|S)[A-Z0-9_]*|DEC[A-Z0-9_]*|DCC[A-Z0-9_]*|\+V[A-Z0-9_]*|V\+)$', re.I)
_SUPPLY_TYPES = ('power_in', 'power_out')


@dataclass
class SchematicInfo:
    path: str | None = None
    pins: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    dnp: dict[str, bool] = field(default_factory=dict)
    value: dict[str, str] = field(default_factory=dict)
    no_connect: list[tuple[float, float]] = field(default_factory=list)
    nc_pins: set[tuple[str, str]] = field(default_factory=set)

    @property
    def found(self) -> bool:
        return self.path is not None

    def pin_name(self, ref: str, number: str) -> str | None:
        pin = self.pins.get((ref, str(number)))
        return pin['name'] if pin else None

    def is_supply_pin(self, ref: str, number: str) -> bool | None:
        """True for a power pin (by electrical type or a supply-like name), False for another named pin, None when the schematic does not know the pin."""
        pin = self.pins.get((ref, str(number)))
        if pin is None:
            return None
        return pin['type'] in _SUPPLY_TYPES or bool(_SUPPLY_NAME.match(pin['name'] or ''))

    def nc_numbers(self, ref: str) -> list[str]:
        return sorted((n for r, n in self.nc_pins if r == ref), key=lambda s: (len(s), s))


def _child(node: list, tag: str) -> list | None:
    return next((c for c in node if isinstance(c, list) and c and c[0] == tag), None)


def _children(node: list, tag: str) -> list[list]:
    return [c for c in node if isinstance(c, list) and c and c[0] == tag]


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _lib_pins(lib_symbol: list) -> list[dict[str, Any]]:
    """Every pin of a library symbol: {number, name, type, x, y, unit}. `unit` 0 pins belong to every unit."""
    out: list[dict[str, Any]] = []
    for sub in _children(lib_symbol, 'symbol'):
        m = re.search(r'_(\d+)_(\d+)$', str(sub[1]) if len(sub) > 1 else '')
        unit = int(m.group(1)) if m else 0
        for pin in _children(sub, 'pin'):
            at, name, number = _child(pin, 'at'), _child(pin, 'name'), _child(pin, 'number')
            if at is None or number is None or len(at) < 3:
                continue
            out.append({'number': str(number[1]), 'name': str(name[1]) if name and len(name) > 1 else '', 'type': str(pin[1]) if len(pin) > 1 else '',
                        'x': _num(at[1]), 'y': _num(at[2]), 'unit': unit})
    return out


def _place(px: float, py: float, at: tuple[float, float, float], mirror: str | None) -> tuple[float, float]:
    """A library pin position on the sheet: mirror, rotate counter-clockwise (the library is y up, the sheet y down), then move."""
    if mirror == 'x':
        py = -py
    elif mirror == 'y':
        px = -px
    angle = math.radians(at[2])
    rx = px * math.cos(angle) - py * math.sin(angle)
    ry = px * math.sin(angle) + py * math.cos(angle)
    return round(at[0] + rx, 4), round(at[1] - ry, 4)


def parse_schematic(text: str, path: str | None = 'schematic') -> SchematicInfo:
    info = SchematicInfo(path=path)
    root = sexpr.parse(text)
    libs: dict[str, list[dict[str, Any]]] = {}
    for lib in _children(_child(root, 'lib_symbols') or [], 'symbol'):
        libs[str(lib[1])] = _lib_pins(lib)
    for node in root:
        if not isinstance(node, list) or not node:
            continue
        if node[0] == 'no_connect':
            at = _child(node, 'at')
            if at and len(at) >= 3:
                info.no_connect.append((_num(at[1]), _num(at[2])))
            continue
        if node[0] != 'symbol' or _child(node, 'lib_id') is None:
            continue
        ref = next((str(p[2]) for p in _children(node, 'property') if len(p) > 2 and p[1] == 'Reference'), None)
        if not ref or ref.startswith('#'):
            continue
        value = next((str(p[2]) for p in _children(node, 'property') if len(p) > 2 and p[1] == 'Value'), '')
        dnp = _child(node, 'dnp')
        info.dnp[ref] = bool(dnp and len(dnp) > 1 and dnp[1] == 'yes')
        info.value[ref] = value
        at_node, unit_node, mirror_node = _child(node, 'at'), _child(node, 'unit'), _child(node, 'mirror')
        at = (_num(at_node[1]), _num(at_node[2]), _num(at_node[3]) if len(at_node) > 3 else 0.0) if at_node and len(at_node) >= 3 else (0.0, 0.0, 0.0)
        unit = int(_num(unit_node[1], 1)) if unit_node and len(unit_node) > 1 else 1
        mirror = str(mirror_node[1]) if mirror_node and len(mirror_node) > 1 else None
        for pin in libs.get(str(_child(node, 'lib_id')[1]), []):
            if pin['unit'] not in (0, unit):
                continue
            x, y = _place(pin['x'], pin['y'], at, mirror)
            info.pins[(ref, pin['number'])] = {'name': pin['name'], 'type': pin['type'], 'x': x, 'y': y}
    for (ref, number), pin in info.pins.items():
        if any(abs(pin['x'] - x) <= NC_TOLERANCE_MM and abs(pin['y'] - y) <= NC_TOLERANCE_MM for x, y in info.no_connect):
            info.nc_pins.add((ref, number))
    return info


def schematic_file(board: Any, ctx: Any = None) -> Path | None:
    """The schematic beside the board file, else the first ``.kicad_sch`` under the version directory."""
    source = getattr(board, 'source', None)
    if source:
        beside = Path(source).with_suffix('.kicad_sch')
        if beside.is_file():
            return beside
        found = sorted(Path(source).parent.glob('*.kicad_sch'))
        if found:
            return found[0]
    version_dir = getattr(ctx, 'version_dir', None)
    if version_dir:
        root = Path(version_dir)
        root = root / 'project' if (root / 'project').is_dir() else root
        try:
            pcbs = sorted(root.rglob('*.kicad_pcb'), key=lambda p: (len(p.parts), str(p)))
            if pcbs and pcbs[0].with_suffix('.kicad_sch').is_file():
                return pcbs[0].with_suffix('.kicad_sch')
            found = sorted(root.rglob('*.kicad_sch'), key=lambda p: (len(p.parts), str(p)))
            if found:
                return found[0]
        except OSError:
            return None
    return None


def schematic_info(board: Any, ctx: Any = None) -> SchematicInfo:
    """The parsed schematic of this board, cached on the context. Empty (``found`` is False) when none can be read."""
    cache = getattr(ctx, 'cache', None)
    if cache is not None and cache.get('schematic_board') is board:
        return cache['schematic']
    info = SchematicInfo()
    path = schematic_file(board, ctx)
    if path is not None:
        try:
            info = parse_schematic(path.read_text(encoding='utf-8'), str(path))
        except (OSError, ValueError, sexpr.SexprError, IndexError):
            info = SchematicInfo()
    if cache is not None:
        cache['schematic'], cache['schematic_board'] = info, board
    return info


# ---------------------------------------------------------------------------------------------- facts for the review

def pin_state(info: SchematicInfo, ref: str, number: str) -> dict[str, Any]:
    """What the schematic says about one pin: its name and type, and whether a no-connect marker sits on it or the symbol declares it no-connect."""
    pin = info.pins.get((ref, str(number)))
    marker = (ref, str(number)) in info.nc_pins
    return {'pin': str(number), 'name': pin['name'] if pin else None, 'type': pin['type'] if pin else None, 'no_connect_marker': marker,
            'no_connect_pin_type': bool(pin and pin['type'] == 'no_connect')}


def part_state(info: SchematicInfo, ref: str) -> dict[str, Any] | None:
    """The schematic's view of one part: value, do-not-populate flag, pins with a no-connect marker. None when the schematic has no such part."""
    if ref not in info.value:
        return None
    pins = [pin_state(info, r, n) for (r, n) in sorted(k for k in info.pins if k[0] == ref)]
    return {'ref': ref, 'value': info.value[ref], 'dnp_in_schematic': info.dnp.get(ref, False),
            'pins_with_no_connect_marker': [p['pin'] for p in pins if p['no_connect_marker']],
            'pins_declared_no_connect_by_symbol': [p['pin'] for p in pins if p['no_connect_pin_type']], 'pins': pins}


def annotate_parity(candidates: list[dict[str, Any]], board: Any, ctx: Any = None) -> int:
    """Add ``schematic_state`` (and a matching line in ``details``) to schematic-parity candidates when the schematic already has what a fix would add.

    A pad without a net that the schematic already marks no-connect (a marker on the pin, or a pin the symbol declares no-connect) says so, so no
    review recommends marking it again; a part the schematic marks do-not-populate lists every part with that flag, so a text quotes all of them.
    Idempotent; returns the number of candidates changed. Nothing here edits a file.
    """
    info = schematic_info(board, ctx)
    if not info.found:
        return 0
    dnp_refs = sorted((r for r, v in info.dnp.items() if v), key=lambda r: (re.sub(r'\d+', '', r), int(re.sub(r'\D', '', r) or 0)))
    changed = 0
    for c in candidates:
        if not str(c.get('rule', '')).startswith('parity_') or c.get('schematic_state'):
            continue
        state: dict[str, Any] = {}
        lines: list[str] = []
        for target in c.get('targets') or []:
            if target.startswith('pad:') and target.count(':') >= 2:
                _, ref, number = target.split(':', 2)
                p = pin_state(info, ref, number)
                label = f'{ref} pin {number}' + (f' ({p["name"]})' if p['name'] else '')
                if p['no_connect_marker']:
                    state.setdefault('no_connect', []).append({'ref': ref, 'pin': number, 'by': 'marker'})
                    lines.append(f'The schematic already has a no_connect marker on {label}.')
                elif p['no_connect_pin_type']:
                    state.setdefault('no_connect', []).append({'ref': ref, 'pin': number, 'by': 'symbol'})
                    lines.append(f'The schematic symbol already declares {label} as no-connect.')
            elif target.startswith('ref:') and target.removeprefix('ref:') in info.dnp and info.dnp[target.removeprefix('ref:')] and 'dnp' not in state:
                state['dnp'] = {'refs': dnp_refs}
                lines.append(f'The schematic marks {_join(dnp_refs)} do-not-populate.')
        if state:
            c['schematic_state'] = {**state, 'text': ' '.join(dict.fromkeys(lines))}
            details = list(c.get('details') or [])
            c['details'] = [*details, c['schematic_state']['text']]
            changed += 1
    return changed


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ', '.join(items[:-1]) + ' and ' + items[-1]
