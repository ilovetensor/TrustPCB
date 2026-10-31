"""Every number with a unit in agent-written text must come from evidence.

`check_text(text, allowed)` returns the numbers that no allowed value explains. Counts of things
("3 vias", "two layers") carry no unit and are exempt here; `claims.py` checks them against the distinct entities of the cited evidence. Units convert inside a dimension, so
"2400 MHz" matches 2.4 GHz and "10 mil" matches 0.254 mm. A written number matches an allowed
value when the two differ by no more than half a unit in the last digit that was written, which is
what rounding means.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

# unit text -> (dimension, factor to the dimension's base unit)
_UNIT_TABLE: dict[str, tuple[str, float]] = {}


def _add(dimension: str, factor_by_unit: dict[str, float]) -> None:
    for unit, factor in factor_by_unit.items():
        _UNIT_TABLE[unit] = (dimension, factor)


_add('length', {'mm': 1.0, 'cm': 10.0, 'um': 0.001, 'µm': 0.001, 'μm': 0.001, 'mil': 0.0254, 'mils': 0.0254, 'inch': 25.4, 'inches': 25.4})
_add('area', {'mm2': 1.0, 'mm²': 1.0, 'cm2': 100.0, 'cm²': 100.0})
_add('frequency', {'Hz': 1.0, 'kHz': 1e3, 'MHz': 1e6, 'GHz': 1e9, 'khz': 1e3, 'mhz': 1e6, 'ghz': 1e9, 'Mhz': 1e6, 'Ghz': 1e9, 'MHZ': 1e6, 'GHZ': 1e9})
_add('dB', {'dB': 1.0, 'db': 1.0})
_add('dBi', {'dBi': 1.0, 'dbi': 1.0})
_add('dBm', {'dBm': 1.0})
_add('dBc', {'dBc': 1.0})
_add('resistance', {'ohm': 1.0, 'ohms': 1.0, 'Ohm': 1.0, 'Ohms': 1.0, 'Ω': 1.0, 'kΩ': 1e3, 'kohm': 1e3, 'kOhm': 1e3, 'MΩ': 1e6, 'Mohm': 1e6, 'mΩ': 1e-3, 'mOhm': 1e-3, 'mohm': 1e-3})
_add('percent', {'%': 1.0})
_add('capacitance', {'pF': 1e-12, 'nF': 1e-9, 'uF': 1e-6, 'µF': 1e-6, 'μF': 1e-6, 'mF': 1e-3})
_add('inductance', {'pH': 1e-12, 'nH': 1e-9, 'uH': 1e-6, 'µH': 1e-6, 'μH': 1e-6, 'mH': 1e-3})
_add('voltage', {'V': 1.0, 'mV': 1e-3, 'kV': 1e3})
_add('current', {'A': 1.0, 'mA': 1e-3, 'uA': 1e-6, 'µA': 1e-6})
_add('power', {'W': 1.0, 'mW': 1e-3, 'uW': 1e-6, 'µW': 1e-6})
_add('time', {'ps': 1e-12, 'ns': 1e-9, 'us': 1e-6, 'µs': 1e-6, 'μs': 1e-6, 'ms': 1e-3})
_add('angle', {'°': 1.0, 'deg': 1.0, 'degrees': 1.0, 'degree': 1.0})
_add('temperature', {'°C': 1.0, 'degC': 1.0, '℃': 1.0, 'K': 1.0})
_add('thermal_resistance', {'°C/W': 1.0, 'K/W': 1.0, 'degC/W': 1.0})
_add('current_density', {'A/mm2': 1.0, 'A/mm²': 1.0})
_add('sheet_current_density', {'A/mm': 1.0})

_UNIT_RE = '|'.join(re.escape(u) for u in sorted(_UNIT_TABLE, key=len, reverse=True))
_NUMBER = re.compile(r'(?<![\w.])(?P<n>[-−+]?(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?|\.\d+))')
_UNIT_AFTER = re.compile(rf'\s{{0,1}}(?P<u>{_UNIT_RE})(?![A-Za-z0-9_])')
_SEPARATOR = re.compile(r'\s*(?:to|–|—|-|/|×|x|by)\s*|\s+to\s+|\s+by\s+', re.I)
_PERCENT_ALIASES = {'percent': '%'}


@dataclass(frozen=True)
class Quantity:
    value: float
    unit: str
    dimension: str
    base: float          # value converted to the dimension's base unit
    decimals: int = 0    # digits written after the decimal point
    raw: str = ''
    start: int = 0
    absolute: bool = True  # False for comparisons ("improved by 2 dB") that a screen run can still support
    note: str = ''         # why a value may not be quoted (screen run absolute, failed trust)
    source: str = ''       # evidence code the value comes from ('' for setup facts such as the goal or a part value)
    name: str = ''         # measurement name inside that evidence row ('distance', 's11_band_worst', ...)
    end: int = 0           # where the written number ends in the text (the unit, if any, follows)

    @property
    def tolerance(self) -> float:
        """Half a unit in the last written digit, in base units."""
        return 0.5 * 10 ** (-self.decimals) * _UNIT_TABLE[self.unit][1] + 1e-12


def make_quantity(value: float, unit: str, decimals: int | None = None, **extra: Any) -> Quantity | None:
    """A quantity from an evidence value; None when the unit is not one we compare."""
    unit = _normalise_unit(unit)
    if unit not in _UNIT_TABLE:
        return None
    dimension, factor = _UNIT_TABLE[unit]
    if decimals is None:
        text = repr(float(value))
        decimals = len(text.split('.')[1].rstrip('0')) if '.' in text and 'e' not in text else 6
    return Quantity(float(value), unit, dimension, float(value) * factor, decimals, **extra)


def _normalise_unit(unit: str) -> str:
    unit = (unit or '').strip()
    return {'ohm': 'ohm', 'Ohm': 'ohm', 'ohms': 'ohm', 'Ohms': 'ohm', 'percent': '%', 'pct': '%', 'deg': 'deg', 'mm^2': 'mm2'}.get(unit, unit)


def extract(text: str) -> list[Quantity]:
    """All numbers in `text` that carry a known unit, ranges and shared units included."""
    if not text:
        return []
    found = [(m, _UNIT_AFTER.match(text, m.end())) for m in _NUMBER.finditer(text)]
    units: list[str | None] = [None] * len(found)
    # Walk backwards so "2.4 to 2.4835 GHz" and "50 x 60 mm" give the first number the shared unit.
    for index in range(len(found) - 1, -1, -1):
        number, unit_match = found[index]
        if unit_match:
            units[index] = unit_match.group('u')
        elif index + 1 < len(found) and units[index + 1]:
            gap = text[number.end():found[index + 1][0].start()]
            if _SEPARATOR.fullmatch(gap):
                units[index] = units[index + 1]
    out = []
    for (number, _), unit in zip(found, units):
        if not unit:
            continue
        raw = number.group('n')
        cleaned = raw.replace('−', '-').replace(',', '')
        decimals = len(cleaned.split('.')[1]) if '.' in cleaned else 0
        quantity = make_quantity(float(cleaned), unit, decimals, raw=text[number.start():number.end()] + ' ' + unit, start=number.start(), end=number.end())
        if quantity:
            out.append(quantity)
    return out


def _explains(written: Quantity, allowed: Quantity) -> bool:
    if written.dimension != allowed.dimension:
        return False
    if abs(written.base - allowed.base) <= written.tolerance:
        return True
    # "10 dB of return loss" for an S11 of -10.3 dB: a gain or loss written without its sign.
    if written.dimension.startswith('dB') and written.value > 0 > allowed.value:
        return abs(written.base + allowed.base) <= written.tolerance
    return False


@dataclass
class Mismatch:
    raw: str
    value: float
    unit: str
    reason: str   # 'not_in_evidence' | 'needs_confirm_run' | 'trust_failed'
    start: int = 0   # where the number starts in the text

    def message(self) -> str:
        if self.reason == 'needs_confirm_run':
            return f'"{self.raw}" comes from a screen run, which is only for comparing. Quote it only after a confirm run, or state the comparison instead.'
        if self.reason == 'trust_failed':
            return f'"{self.raw}" comes from a result that failed its trust checks and cannot support a statement.'
        return f'"{self.raw}" does not match any value in the evidence. Use a number a tool produced, or remove it.'


def check_text(text: str, allowed: Iterable[Quantity]) -> list[Mismatch]:
    """Numbers in `text` that no allowed quantity explains. A match against a restricted value reports why."""
    pool = list(allowed)
    problems: list[Mismatch] = []
    for written in extract(text):
        matches = [a for a in pool if _explains(written, a)]
        if not matches:
            problems.append(Mismatch(written.raw, written.value, written.unit, 'not_in_evidence', written.start))
        elif all(a.note for a in matches):
            problems.append(Mismatch(written.raw, written.value, written.unit, matches[0].note, written.start))
    return problems


def allowed_from_values(values: Any, **extra: Any) -> list[Quantity]:
    """Quantities from `{name: {value, unit}}`, lists of those, or a nested mix."""
    out: list[Quantity] = []
    if isinstance(values, dict):
        if 'value' in values and 'unit' in values and isinstance(values['value'], (int, float)) and not isinstance(values['value'], bool):
            quantity = make_quantity(values['value'], str(values['unit']), **extra)
            if quantity:
                out.append(quantity)
                if quantity.unit == '%':
                    pass
            if values.get('unit') in ('ratio', 'fraction'):
                out.append(make_quantity(values['value'] * 100, '%', **extra))
        else:
            for item in values.values():
                out.extend(allowed_from_values(item, **extra))
    elif isinstance(values, (list, tuple)):
        for item in values:
            out.extend(allowed_from_values(item, **extra))
    return [q for q in out if q]


def allowed_from_text(text: str, **extra: Any) -> list[Quantity]:
    """Numbers a tool wrote into its own output text (a candidate title, a part value)."""
    return [Quantity(q.value, q.unit, q.dimension, q.base, q.decimals, q.raw, q.start, **{'end': q.end, **extra}) for q in extract(text or '')]


# ---- sentences and problems shared by the claim checks ------------------------------------------------------------------

_SPLIT = re.compile(r'(?<=[.!?])(?<!e\.g\.)(?<!i\.e\.)\s+|\n+')


def sentence_spans(text: str) -> list[tuple[int, int]]:
    """(start, end) of each sentence of `text`. A full stop inside a number ("5.74 mm") does not end one."""
    spans: list[tuple[int, int]] = []
    position = 0
    for match in _SPLIT.finditer(text or ''):
        if text[position:match.start()].strip():
            spans.append((position, match.start()))
        position = match.end()
    if (text or '')[position:].strip():
        spans.append((position, len(text)))
    return spans


@dataclass
class Problem:
    """One claim the evidence does not support, with the message the agent gets and the sentence it is in."""
    code: str
    field: str
    sentence: str
    message: str
    data: dict | None = None
