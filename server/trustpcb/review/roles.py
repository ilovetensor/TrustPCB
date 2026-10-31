"""Part roles in the board brief must not contradict the part's own symbol value.

The brief is the model's reading of the board, and the analyzers let a brief's `part_roles` win over their own classification by value. A model
that guesses ("U1 is the regulator, U2 the sensor") then feeds the wrong role to the zone and decoupling checks and into the report overview. This check compares
each role the brief gives to what the footprint's value and library id say the part is, for the part families it can tell apart, and reports the ones
that disagree with the role the symbol suggests. A part whose value says nothing is never contradicted.
"""
from __future__ import annotations

import re
from typing import Any

MAX_ROLE_REFUSALS = 2        # the brief is refused this many times for a role that contradicts a symbol; then the server uses the symbol's role and records a limit

# family -> (value / library id pattern, canonical role, words that name that family as a role, what the part is)
FAMILIES: dict[str, tuple[re.Pattern, str, frozenset, str]] = {
    'protection': (re.compile(r'USBLC6|PRTR5V|TPD\d|\bESD|PESD|SRV05|ECMF|IP4\d{3}|SMAJ|SMF\d|\bTVS|NUP\d|CDSOT|SP\d{4}|PTC|polyfuse', re.I), 'protection',
                   frozenset({'protection', 'esd', 'tvs', 'clamp', 'surge', 'usb_protection', 'esd_protection', 'protect'}), 'ESD or surge protection'),
    'regulator': (re.compile(r'AP2112|AP2210|AP7\d{3}|AMS1117|LM1117|LM1085|LM317|LM2596|LM2576|LM2940|MCP170\d|MCP18\d|XC62\d\d|TLV7\d|TLV\d{3}\b|TPS7\d|TPS6\d|TPS5\d|LD1117|LDO|'
                             r'REG1\d|NCP1\d{3}|RT9\d{3}|SPX\d|LP29\d|LP5907|MIC5\d|\bLDL|MAX1\d{3}', re.I), 'regulator',
                  frozenset({'regulator', 'ldo', 'power', 'supply', 'buck', 'boost', 'converter', 'pmic', 'reg', 'power_supply', 'switching'}), 'a voltage regulator'),
    'sensor': (re.compile(r'LM75|TMP\d{2,3}|SHT\d|HDC\d|BME\d|BMP\d|LIS\d|MPU\d|ICM\d|DS18B20|LSM\d|ADXL|BMI\d|SI7\d|TCN75|MAX30|MAX31|LPS\d|VL53|APDS|OPT\d|AS7\d', re.I), 'sensor',
               frozenset({'sensor', 'temperature', 'accelerometer', 'imu', 'humidity', 'temp', 'thermometer'}), 'a sensor'),
    'radio': (re.compile(r'NRF5\d|NRF9\d|ESP32|ESP8266|CC\d{4}|EFR32|SX12|\bRFM|CMWX', re.I), 'radio',
              frozenset({'radio', 'rf', 'mcu', 'soc', 'wireless', 'ble', 'bluetooth', 'transceiver', 'processor', 'digital', 'module'}), 'a radio or radio MCU'),
    'indicator': (re.compile(r'\bLED|_LED|LED_', re.I), 'indicator', frozenset({'indicator', 'led', 'status', 'light', 'display'}), 'an LED'),
}
# roles that name a family of their own and never fit a part of another family
_ALL_ROLE_WORDS = {w: family for family, (_, _, words, _) in FAMILIES.items() for w in words}


def _norm(role: str) -> str:
    return re.sub(r'[^a-z0-9]+', '_', str(role).lower()).strip('_')


def family_of(value: str, lib_id: str = '') -> str | None:
    """The part family the symbol value (or, failing that, the library id) says, or None when it does not say."""
    for text in (value or '', lib_id.split(':')[-1] if lib_id else ''):
        hits = [name for name, (pattern, *_rest) in FAMILIES.items() if pattern.search(text)]
        if len(hits) == 1:
            return hits[0]
    return None


def conflicts(board: Any, part_roles: dict[str, str]) -> list[dict]:
    """[{ref, role, value, expected, suggested, message}] for each role that contradicts the symbol. Unknown references are skipped (checked elsewhere).

    The analyzers' own `check_role` decides when it is there (ok False means the symbol says the part is something else, None means the symbol does not
    decide); the table above is the fallback for a tree without it."""
    try:
        from ..analyzers import check_role
    except ImportError:
        check_role = None
    out = []
    for ref, role in (part_roles or {}).items():
        if check_role is not None:
            found = check_role(ref, str(role), board)
            suggested = found.get('symbol_role') or found.get('symbol_family')
            if found.get('ok') is False and suggested not in (None, 'digital'):       # "digital" is the generic fallback: the table below knows LEDs better
                out.append({'ref': ref, 'role': role, 'value': found.get('value') or '', 'expected': found.get('symbol_family'), 'suggested': suggested,
                            'message': f'{found["message"]} Use "{suggested}", or leave {ref} out of part_roles if you are not sure.'})
                continue
            if found.get('ok') is not False:
                continue
        footprint = board.footprint(ref) if hasattr(board, 'footprint') else None
        if footprint is None:
            continue
        expected = family_of(getattr(footprint, 'value', '') or '', getattr(footprint, 'lib_id', '') or '')
        given = _ALL_ROLE_WORDS.get(_norm(role))
        if expected is None or given is None or given == expected:
            continue
        # a radio MCU is also "digital"; an indicator LED is not a protection part: only different families conflict
        _, suggested, _, what = FAMILIES[expected]
        value = getattr(footprint, 'value', '') or getattr(footprint, 'lib_id', '')
        out.append({'ref': ref, 'role': role, 'value': value, 'expected': expected, 'suggested': suggested,
                    'message': f'{ref} is called "{role}", but its symbol value is {value}, which is {what}. Use "{suggested}", or leave {ref} out of part_roles if you are not sure.'})
    return out
