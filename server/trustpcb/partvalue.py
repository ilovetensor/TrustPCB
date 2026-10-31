"""One reader for passive part values, shared by the analyzers and the RF setup.

``parse_value(text, kind)`` returns the value in base units (ohm, farad, henry) or ``None``.
It follows standard EE notation:

* ``R`` ``K`` ``M`` ``G`` in the place of the decimal point: ``2R2`` is 2.2 ohm, ``4K7`` 4.7 kohm,
  ``1M5`` 1.5 Mohm, ``R22`` 0.22 ohm.
* ``p n u m`` (and the micro sign) for capacitors and inductors: ``4n7``, ``100n``, ``2.2u``.
* a unit suffix after the number: ``pF`` ``nH`` ``kOhm`` ``ohm`` ``22R``.
* a tolerance, voltage, power or frequency after the value is ignored: ``100n 16V``, ``10k 1%``.
* ``DNP`` style words are ignored.

Letter case matters only where the standard says so: ``M`` is mega, ``m`` is milli. ``Meg`` is mega.

``kind`` is ``"R"``, ``"L"`` or ``"C"``. Where a letter would mean different things the kind
decides: a capacitor or inductor has no mega, a resistor has no pico, nano or micro. A bare number
is an ohm value on a resistor, because a resistor has no other unit. On a capacitor or an inductor
it is ``None``: nothing in the text says pF from uF or nH from uH, and a guess would be wrong by
orders of magnitude. With no kind (or an unknown one) the unit in the text decides, and a bare
number is ``None``. Text that does not read as one value returns ``None``; nothing here raises.
"""

from __future__ import annotations

import re

_SCALE = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "m": 1e-3, "k": 1e3, "M": 1e6, "meg": 1e6, "G": 1e9, "": 1.0}
_SMALL = {"p", "n", "u", "m"}
_RESISTOR_PREFIX = {"", "m", "k", "M", "meg", "G"}

_DNP_WORD = re.compile(r"(?<![A-Za-z0-9.])(?:DNP|DNF|NC|NP)(?![A-Za-z0-9])", re.I)
# tolerance, rating and frequency text that follows a value: it is never the value
_NOISE = re.compile(
    r"[±+\-]?\d+(?:\.\d+)?\s*(?:%|ppm)"
    r"|\d+\s*/\s*\d+\s*W\b"
    r"|\d+(?:\.\d+)?\s*[kKmMu]?(?:V|A|W|Hz)(?![A-Za-z0-9])",
    re.I,
)
_TOKEN = re.compile(
    r"(?<![A-Za-z0-9.])(?:"
    r"(?P<di>\d*)(?P<dl>[RKMGPNU])(?P<df>\d+)(?:\s*(?P<du>ohms?|F|H|R))?"
    r"|(?P<num>\d+(?:\.\d+)?|\.\d+)\s*(?P<pre>meg|[pnumkMG])?\s*(?P<unit>ohms?|F|H|R)?"
    r")(?![A-Za-z0-9.])",
    re.I,
)


def _normalise(text: str) -> str:
    t = text.replace("\u00b5", "u").replace("\u03bc", "u")
    t = t.replace("\u03a9", "ohm").replace("\u2126", "ohm").replace("\u03c9", "ohm")
    t = re.sub(r"(?<=\d),(?=\d)", ".", t)
    t = _DNP_WORD.sub(" ", t)
    return _NOISE.sub(" ", t)


def _prefix(letter: str) -> str:
    """The prefix key of a letter. Only ``m`` and ``M`` keep their case; ``meg`` is mega in any case."""
    low = letter.lower()
    if low == "meg":
        return "meg"
    if letter in ("m", "M"):
        return letter
    return "G" if low == "g" else low


def _unit_kind(unit: str | None) -> str | None:
    if not unit:
        return None
    u = unit.lower()
    return "C" if u == "f" else "L" if u == "h" else "R"


def _resolve(number: str, key: str, unit: str | None, kind: str | None) -> float | None:
    """Apply the kind rules to one number, prefix key and unit; ``key`` is "" for no prefix."""
    unit_kind = _unit_kind(unit)
    if kind and unit_kind and unit_kind != kind:
        return None
    use = kind or unit_kind
    if key == "R":  # the letter R in the decimal place is the ohm itself
        return float(number) if use in (None, "R") else None
    if use is None:
        if key in _SMALL:
            return float(number) * _SCALE[key]  # farad or henry: the scale is known, the unit is not needed
        use = "R" if key in ("k", "M", "meg", "G") else None
        if use is None:
            return None
    if use == "R":
        ok = key in _RESISTOR_PREFIX
    else:
        ok = key in _SMALL or (key == "" and unit_kind == use)
    return float(number) * _SCALE[key] if ok else None


def parse_value(text: str | None, kind: str | None = None) -> float | None:
    """The value of a passive in base units, or ``None`` when it cannot be known. Never raises."""
    try:
        return _parse(text, kind)
    except (ValueError, TypeError, OverflowError):
        return None


def _parse(text: str | None, kind: str | None) -> float | None:
    if not text or not isinstance(text, str):
        return None
    kind = kind.upper() if isinstance(kind, str) and kind.upper() in ("R", "L", "C") else None
    decorated: list[tuple[str, str, str | None]] = []
    bare: list[str] = []
    clean = _normalise(text)
    for m in _TOKEN.finditer(clean):
        if m.group("dl"):
            letter = m.group("dl")
            if not m.group("di") and letter not in ("R", "r"):
                continue  # only R may open a value (R22); M3, K7 and the like are not values
            number = f"{m.group('di') or '0'}.{m.group('df')}"
            key = "R" if letter in ("R", "r") else _prefix(letter)
            decorated.append((number, key, m.group("du")))
        elif m.group("pre") or m.group("unit"):
            decorated.append((m.group("num"), _prefix(m.group("pre") or ""), m.group("unit")))
        else:
            bare.append(m.group("num"))
    if decorated:
        return _resolve(*decorated[0], kind)
    if kind == "R" and len(bare) == 1 and clean.strip() == bare[0]:
        return float(bare[0])  # a lone number is ohms; a number inside other text ("AP2112K-3.3") is not a value
    return None
