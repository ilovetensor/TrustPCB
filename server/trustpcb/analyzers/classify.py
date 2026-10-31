"""Part and net classification by name, used when no board brief supplies part roles.

Everything here is a heuristic over reference prefixes, values, library ids and net names. A
brief's ``part_roles`` is taken as given unless the part's own symbol value or library id says
otherwise (an ESD array called a regulator): then the symbol wins and the conflict is kept in
``ctx.cache["role_conflicts"]`` and reported by ``part_roles_report``. ``symbol_role`` and
``check_role`` let a caller test any proposed role against the symbol value. Roles are short
lowercase words:

``rf`` ``switching`` ``power`` ``analog`` ``digital`` ``clock`` ``usb`` ``connector``
``protection`` ``mechanical`` (mounting holes, fiducials and test points are never zoned).
"""

from __future__ import annotations

import math
import re
from typing import TYPE_CHECKING, Any

from ..pcb import geom
from ..pcb.model import Board, Footprint

if TYPE_CHECKING:  # pragma: no cover
    from .base import AnalyzerContext

# ----------------------------------------------------------------------------- nets

_GROUND_RE = re.compile(r"(^|[^A-Z])(GND|GNDA|GNDD|AGND|DGND|PGND|SGND|VSS|VSSA|0V|EARTH)([^A-Z]|$)|^GND|GND$|^VSS")
_POWER_RE = re.compile(
    r"^[+-]?\d+V\d*(\.\d+)?(_.*)?$"
    r"|^[+]?V(CC|DD|BUS|BAT|SYS|IN|OUT|REG|IO|DDA|DDH|DDIO|CCA|PP|M)\w*$"
    r"|^(A|D|P)?V(DD|CC)\w*$"
    r"|^DEC\w*$|^PWR\w*$|^VIN$|^VOUT$|^V\+$|^\+\w*V\w*$"
)
_RF_RE = re.compile(r"(^|[^A-Z])(ANT|ANTENNA|RF|RFIN|RFOUT|RF_IN|RF_OUT|LNA|2G4)([^A-Z]|$)")
_FAST_RE = re.compile(r"USB|(^|[^A-Z])(DP|DM)([^A-Z]|$)|D\+|D-|CLK|SCLK|SCK|SPI|QSPI|MOSI|MISO|SWD|XTAL|HDMI|LVDS|ETH|MDIO|RMII|SDIO|(^|[^A-Z])OSC")
_DIFF_RE = re.compile(r"D\+|D-|(^|[^A-Z])(DP|DM)([^A-Z]|$)|_P$|_N$|[+-]$|(^|[^A-Z])(TX|RX)[PN]([^A-Z]|$)")
_CLOCK_RE = re.compile(r"XTAL|OSC|CLK|(^|[^A-Z])XI|(^|[^A-Z])XO|HSE|LSE|32K")
_USB_RE = re.compile(r"USB|VBUS|D\+|D-|(^|[^A-Z])(CC1|CC2|DP|DM)([^A-Z]|$)")


def is_ground(name: str | None) -> bool:
    return bool(name) and bool(_GROUND_RE.search(name.upper()))


def is_power(name: str | None, ctx: AnalyzerContext | None = None) -> bool:
    if not name or is_ground(name):
        return False
    if ctx is not None and name in ctx.power_nets:
        return True
    return bool(_POWER_RE.match(name.upper()))


def is_rf_net(name: str | None, ctx: AnalyzerContext | None = None) -> bool:
    if not name:
        return False
    if ctx is not None and name in ctx.rf_nets:
        return True
    return bool(_RF_RE.search(name.upper()))


def is_diff_net(name: str | None) -> bool:
    return bool(name) and bool(_DIFF_RE.search(name.upper())) and not is_rf_net(name)


def is_fast_net(name: str | None, ctx: AnalyzerContext | None = None) -> bool:
    if not name or is_ground(name) or is_power(name):
        return False
    if ctx is not None and name in ctx.fast_nets:
        return True
    return is_rf_net(name, ctx) or bool(_FAST_RE.search(name.upper()))


def is_clock_net(name: str | None) -> bool:
    return bool(name) and bool(_CLOCK_RE.search(name.upper()))


def fast_nets(board: Board, ctx: AnalyzerContext) -> list[str]:
    """Nets that carry tracks and are fast by the brief or by name, in board net order."""
    routed = {t.net for t in board.tracks if t.net}
    return [n for n in board.net_names() if n in routed and is_fast_net(n, ctx)]


def rf_nets(board: Board, ctx: AnalyzerContext) -> list[str]:
    return [n for n in board.net_names() if is_rf_net(n, ctx)]


# ---------------------------------------------------------------------------- values

_UNIT = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3, "k": 1e3, "meg": 1e6, "g": 1e9, "": 1.0}
_VALUE_RE = re.compile(r"(?<![A-Za-z0-9.])(\d+(?:[.,]\d+)?)\s*(p|n|u|µ|m|k|meg|g|r)?\s*(f|h|ohm|Ω|hz)?(?![A-Za-z])", re.I)
_RKM_RE = re.compile(r"^(\d+)([pnuµmkrgf])(\d+)\s*(f|h)?$", re.I)


def parse_value(value: str) -> float | None:
    """Numeric value in base units (farad, henry, ohm) from strings like ``100nF``, ``4u7`` or ``2.2nH``."""
    text = (value or "").strip().replace(",", ".")
    if not text:
        return None
    m = _RKM_RE.match(text)
    if m:
        mult = _UNIT.get(m.group(2).lower(), 1.0 if m.group(2).lower() == "r" else None)
        if mult is not None:
            return float(f"{m.group(1)}.{m.group(3)}") * mult
    m = _VALUE_RE.search(text)
    if not m:
        return None
    mult = _UNIT.get((m.group(2) or "").lower().replace("r", ""), 1.0)
    return float(m.group(1)) * mult


# ---------------------------------------------------------------------------- parts

_PREFIX_RE = re.compile(r"^([A-Za-z]+)")

_RF_PART_RE = re.compile(
    r"nrf5\d|nrf9|nrf24|esp32|esp8266|esp-?wroom|cc2[0-9]{3}|cc1[0-9]{3}|sx12[0-9]{2}|lora|rfm9|efr32|bgm\d|mgm\d|stm32w[bl]|"
    r"ble|bluetooth|wifi|wi-?fi|zigbee|lna|saw|balun|diplexer|rf|antenna|ant\d|chip_?ant|2450|gnss|max-?m\d|neo-?m|sim7|sara|nina|mdbt",
    re.I,
)
_SWITCH_RE = re.compile(r"buck|boost|tps6[0-9]{3}|tps5[0-9]{4}|lm25[0-9]{2}|lm26[0-9]{2}|mp[0-9]{4}|rt[0-9]{4}|sy8[0-9]{3}|xl[0-9]{4}|lt3[0-9]{3}|ltc3[0-9]{3}|st1s|dc-?dc|smps|switching|inverter|charge[\s_-]?pump|mcp1640|ap34[0-9]{2}|aoz1", re.I)
_LDO_RE = re.compile(r"ldo|ap2112|ap7[0-9]{3}|lm1117|ams1117|mcp17[0-9]{2}|tlv7[0-9]{2}|xc6[0-9]{3}|lp5[0-9]{3}|ncp1[0-9]{3}|regulator|reg_|spx3819|ld1117|bq2[0-9]{4}|charger|pmic", re.I)
_ANALOG_RE = re.compile(
    r"lm75|tmp[0-9]{3}|ds18|sensor|bm[ep][0-9]{3}|sht[0-9]{2}|adc|ads[0-9]{4}|opa[0-9]|lm358|lm324|mcp6[0-9]{3}|ina[0-9]{3}|ref[0-9]{4}|max31|adxl|lis[0-9]|mpu-?[0-9]|icm-?[0-9]|"
    r"imu|magnet|hall|photo|ldr|thermistor|ntc|opamp|op-?amp|amplifier|dac|codec|microphone|mic",
    re.I,
)
_PROT_RE = re.compile(r"usblc|tpd[0-9]|esd|prtr|srv05|pesd|tvs|varistor|ptc|polyfuse|fuse|smbj|smaj|ip4", re.I)
_USB_PART_RE = re.compile(r"usb", re.I)
_XTAL_RE = re.compile(r"crystal|xtal|resonator|oscillator|tcxo|mhz|khz", re.I)
_MECH_RE = re.compile(r"mountinghole|mounting_hole|fiducial|testpoint|test_point|tp_|logo|heatsink|standoff", re.I)
_MCU_RE = re.compile(r"stm32|atmega|attiny|rp2040|samd|esp32|pic[0-9]|msp430|mcu|fpga|cpld|ice40|lpc[0-9]|nrf5", re.I)
_SW_NET_RE = re.compile(r"(^|[^A-Z])(SW|LX|PHASE|SWITCH|SW_NODE|VSW)([^A-Z0-9]|$)")


def ref_base(ref: str) -> str:
    return ref.split("#", 1)[0]


def ref_prefix(ref: str) -> str:
    m = _PREFIX_RE.match(ref_base(ref))
    return m.group(1).upper() if m else ""


def part_kind(fp: Footprint) -> str:
    """Coarse part kind: cap res ind ferrite diode led xtal ic connector switch fuse testpoint fiducial mounting antenna other."""
    prefix = ref_prefix(fp.ref)
    lib = fp.lib_id.split(":")[-1]
    hay = f"{fp.value} {lib}"
    if prefix in ("TP",) or re.search(r"testpoint", lib, re.I):
        return "testpoint"
    if prefix.startswith("FID") or re.search(r"fiducial", lib, re.I):
        return "fiducial"
    if prefix in ("H", "MH") or re.search(r"mountinghole", lib, re.I):
        return "mounting"
    if prefix in ("AE", "ANT", "E") or (re.search(r"antenna|chip_?ant|2450ab|2450at", hay, re.I) and prefix != "C"):
        return "antenna"
    if prefix in ("Y", "XTAL") or prefix == "X" and _XTAL_RE.search(hay) or re.search(r"crystal", lib, re.I):
        return "xtal"
    if prefix in ("J", "P", "CN", "CON", "JP", "K") or re.search(r"connector|header|usb_c|receptacle|socket", lib, re.I):
        return "connector"
    if prefix == "C" or prefix.startswith("C") and re.match(r"^CP?$", prefix):
        return "cap"
    if prefix in ("R", "RN", "RV", "RT", "NTC"):
        return "res"
    if prefix == "L":
        return "ind"
    if prefix in ("FB", "FL"):
        return "ferrite"
    if prefix == "D":
        return "led" if re.search(r"led", hay, re.I) else "diode"
    if prefix in ("LED",):
        return "led"
    if prefix in ("SW", "S", "BTN"):
        return "switch"
    if prefix in ("F", "PTC"):
        return "fuse"
    if prefix in ("U", "IC", "Q", "M", "A", "MOD", "T") or len(fp.pad_ids) > 3:
        return "ic"
    return "other"


def _module_role(fp: Footprint) -> str | None:
    """Role for an active part from its value and library id."""
    lib = fp.lib_id.split(":")[-1]
    hay = f"{fp.value} {lib}"
    if _MECH_RE.search(lib) or ref_prefix(fp.ref) in ("H", "MH", "TP") or ref_prefix(fp.ref).startswith("FID"):
        return "mechanical"
    if _SWITCH_RE.search(hay):
        return "switching"
    if _PROT_RE.search(hay):
        return "protection"
    if _RF_PART_RE.search(hay):
        return "rf"
    if _LDO_RE.search(hay):
        return "power"
    if _ANALOG_RE.search(hay):
        return "analog"
    if _MCU_RE.search(hay):
        return "digital"
    return None


_DNP_RE = re.compile(r"(^|[^a-z])(dnp|dnf|dni|dnm|do[\s_-]?not[\s_-]?(populate|place|fit|install|mount|stuff)|not[\s_-]?(fitted|populated|placed|installed|mounted)|unpopulated|nostuff|no[\s_-]?stuff)([^a-z]|$)", re.I)


def is_dnp(fp: Footprint) -> bool:
    """True when the part is not fitted: the footprint says so, or its value does ("DNP 0.5pF").

    The board file often lacks the attribute although the schematic marks the part do-not-populate,
    so the value text is read too.
    """
    return bool(fp.dnp) or bool(_DNP_RE.search(fp.value or ""))


# ------------------------------------------------------------------- roles from the symbol
#
# (pattern, family, analyzer role, exact proposed roles that fit, proposed-role words that fit)
# Patterns are matched on the symbol value and the library id (the footprint name). They are strict
# on purpose: a match here overrules a guess, so each pattern names a real part family.
_P = frozenset
_SYMBOL_FAMILIES: tuple[tuple[re.Pattern[str], str, str, frozenset[str], frozenset[str]], ...] = (
    (
        re.compile(r"usblc6|usbuf|tpd[0-9]+e|tpd[0-9]+s|ip42[0-9]+|srv05|pesd[0-9]|prtr5v|esda[0-9]|esd[0-9]{3,}|tvs|ecmf|ecl[0-9]{4}|nup[0-9]+|rclamp|sp0[0-9]{3}|smbj|smaj|p6ke|varistor", re.I),
        "esd_protection",
        "protection",
        _P({"protection", "esd", "esd_protection", "usb_protection", "usb_esd", "tvs", "surge", "clamp", "usb", "input_protection", "signal_protection"}),
        _P({"protection", "esd", "tvs", "surge", "clamp", "protect"}),
    ),
    (
        re.compile(r"ap2112|ap2127|ap2210|ap7[0-9]{3}|lm1117|ams1117|ld1117|ld3985|mcp17[0-9]{2}|mcp1700|tlv7[0-9]{2}|tps7[0-9]{4}|tps78[0-9]|xc6[0-9]{3}|lp59[0-9]{2}|lp298[0-9]|lp38[0-9]{2}|lt1117|ncp11[0-9]{2}|ncp1[0-9]{3}|spx3819|mic5[0-9]{3}|ldo|rt9013|rt9193|ht73[0-9]{2}|xcl|662k|ams1117|78l[0-9]{2}|lm317|lm78[0-9]{2}", re.I),
        "ldo_regulator",
        "power",
        _P({"regulator", "ldo", "power", "power_supply", "voltage_regulator", "linear_regulator", "supply", "power_regulator", "power_input", "power_conversion"}),
        _P({"regulator", "ldo", "power", "supply", "regulation"}),
    ),
    (
        re.compile(r"tps6[0-9]{4}|tps5[0-9]{4}|lm25[0-9]{2}|lm26[0-9]{2}|mp[0-9]{4}|rt[0-9]{4}[a-z]*buck|sy8[0-9]{3}|xl[0-9]{4}|lt3[0-9]{3}|ltc3[0-9]{3}|st1s[0-9]+|mcp1640|ap34[0-9]{2}|aoz1[0-9]{3}|buck|boost|dc-?dc|smps|charge[\s_-]?pump", re.I),
        "switching_regulator",
        "switching",
        _P({"regulator", "switching", "buck", "boost", "dcdc", "dc_dc", "smps", "power", "power_supply", "converter", "switching_regulator", "power_conversion"}),
        _P({"regulator", "switching", "buck", "boost", "dcdc", "smps", "power", "converter", "supply"}),
    ),
    (
        re.compile(r"nrf5\d|nrf9\d|nrf24|esp32|esp8266|esp-?wroom|cc2[0-9]{3}|cc1[0-9]{3}|sx12[0-9]{2}|rfm9|efr32|bgm\d|mgm\d|stm32w[bl]|mdbt|nina-|ble[\s_-]?module|nrf-?soc", re.I),
        "radio_soc",
        "rf",
        _P({"radio", "rf", "mcu", "soc", "wireless", "ble", "bluetooth", "radio_mcu", "mcu_radio", "processor", "controller", "microcontroller", "main", "main_mcu", "core", "digital", "transceiver", "wifi", "lora", "zigbee"}),
        _P({"radio", "rf", "mcu", "soc", "wireless", "ble", "bluetooth", "processor", "controller", "microcontroller", "transceiver"}),
    ),
    (
        re.compile(r"stm32|atmega|attiny|rp2040|samd|pic[0-9]{2}|msp430|lpc[0-9]|ice40|cpld", re.I),
        "mcu",
        "digital",
        _P({"mcu", "microcontroller", "processor", "controller", "main", "main_mcu", "core", "digital", "soc", "fpga"}),
        _P({"mcu", "microcontroller", "processor", "controller", "digital", "core"}),
    ),
    (
        re.compile(r"lm75|tmp[0-9]{3}|ds18|bm[ep][0-9]{3}|sht[0-9]{2}|adxl|lis[0-9]|mpu-?[0-9]|icm-?[0-9]|bmi[0-9]|max31|ina[0-9]{3}|ads[0-9]{4}|hdc[0-9]{4}|si70[0-9]{2}|aht[0-9]|veml|opt[0-9]{4}|sensor", re.I),
        "sensor",
        "analog",
        _P({"sensor", "analog", "temperature_sensor", "temp_sensor", "imu", "accelerometer", "adc", "measurement", "monitor", "thermal"}),
        _P({"sensor", "analog", "temperature", "temp", "imu", "accelerometer", "adc", "measurement", "thermal"}),
    ),
)
_LED_RE = re.compile(r"led", re.I)
_LED_ROLES = _P({"led", "indicator", "status", "status_led", "digital", "display", "user_led"})
_XTAL_FAMILY_ROLES = _P({"crystal", "clock", "oscillator", "xtal", "timing", "high_speed", "hf_crystal", "32mhz_crystal"})
_KIND_ROLES: dict[str, tuple[str, frozenset[str]]] = {
    "led": ("led", _LED_ROLES),
    "xtal": ("crystal", _XTAL_FAMILY_ROLES),
    "fuse": ("fuse", _P({"fuse", "protection", "ptc", "input_protection", "power_protection", "polyfuse", "power_input", "power"})),
    "antenna": ("antenna", _P({"antenna", "ant", "rf", "radio", "radiator"})),
    "mounting": ("mounting_hole", _P({"mounting", "mechanical", "hole", "mounting_hole"})),
    "fiducial": ("fiducial", _P({"fiducial", "mechanical", "alignment"})),
    "testpoint": ("test_point", _P({"test", "testpoint", "test_point", "mechanical", "debug", "probe"})),
}
#: roles that only an active part can have: a passive or connector given one of these is a mismatch
_ACTIVE_ONLY_WORDS = _P({"regulator", "ldo", "mcu", "microcontroller", "radio", "sensor", "protection", "esd", "tvs"})


def symbol_role(fp: Footprint) -> dict[str, Any] | None:
    """What the part's own value and library id say it is, or None when they do not say.

    Returns ``{ref, value, lib_id, family, role, evidence, source}`` where ``family`` is a fine class
    (``esd_protection``, ``ldo_regulator``, ``switching_regulator``, ``radio_soc``, ``mcu``, ``sensor``,
    ``led``, ``crystal``, ``fuse``, ``antenna`` ...), ``role`` is the analyzers' coarse role and
    ``evidence`` is the text that matched. Reference prefixes decide only the kinds that need no
    value (LED, crystal, fuse, antenna, holes, test points).
    """
    kind = part_kind(fp)
    lib = fp.lib_id.split(":")[-1]
    value = fp.value or ""
    if kind in ("ic", "other"):
        for rx, family, role, _exact, _words in _SYMBOL_FAMILIES:
            m = rx.search(value) or rx.search(lib)
            if m:
                src = "value" if rx.search(value) else "lib_id"
                return {"ref": fp.ref, "value": value, "lib_id": fp.lib_id, "family": family, "role": role, "evidence": m.group(0), "source": src}
        return None
    if kind in _KIND_ROLES:
        family, _roles = _KIND_ROLES[kind]
        role = {"led": "digital", "xtal": "clock", "fuse": "protection", "antenna": "rf"}.get(kind, "mechanical")
        return {"ref": fp.ref, "value": value, "lib_id": fp.lib_id, "family": family, "role": role, "evidence": fp.lib_id or fp.ref, "source": "lib_id"}
    if kind == "diode":
        m = None
        for rx, family, role, _e, _w in _SYMBOL_FAMILIES[:1]:
            m = rx.search(value) or rx.search(lib)
            if m:
                return {"ref": fp.ref, "value": value, "lib_id": fp.lib_id, "family": family, "role": role, "evidence": m.group(0), "source": "value"}
        return {"ref": fp.ref, "value": value, "lib_id": fp.lib_id, "family": "diode", "role": "digital", "evidence": fp.lib_id or fp.ref, "source": "lib_id"}
    return None


def _norm_role(role: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(role or "").strip().lower()).strip("_")


def check_role(fp: Footprint | str, proposed: str, board: Board | None = None) -> dict[str, Any]:
    """Check a proposed role for a part against its symbol value and library id.

    ``fp`` is a footprint, or a reference when ``board`` is given. Returns::

        {ref, proposed, ok: True | False | None, symbol_family, symbol_role, evidence, message}

    ``ok`` is False when the symbol says the part is something else (``USBLC6-2SC6`` is ESD
    protection, so "regulator" is False), True when the proposed role fits what the symbol says, and
    None when the symbol does not decide (a passive, or a part number the rules do not know): the
    role stays unchecked, not approved. Matching is on the whole role or its words, so
    "usb_protection" fits an ESD part and "power_regulator" fits an LDO.
    """
    if isinstance(fp, str):
        found = board.footprint(fp) if board is not None and hasattr(board, "footprint") else None
        if found is None:
            return {"ref": fp, "proposed": proposed, "ok": None, "symbol_family": None, "symbol_role": None, "evidence": None, "message": f"{fp} is not on the board, so its role cannot be checked."}
        fp = found
    norm = _norm_role(proposed)
    sym = symbol_role(fp)
    base = {"ref": fp.ref, "proposed": proposed, "symbol_family": sym["family"] if sym else None, "symbol_role": sym["role"] if sym else None, "evidence": sym["evidence"] if sym else None, "value": fp.value}
    if sym is None:
        # a passive or connector given a role only an active part can have
        kind = part_kind(fp)
        words = set(norm.split("_"))
        if kind in ("cap", "res", "ind", "ferrite", "connector", "switch", "testpoint", "fiducial", "mounting") and words & _ACTIVE_ONLY_WORDS and not words & {"decoupling", "bypass", "filter", "matching"}:
            return {**base, "ok": False, "message": f"{fp.ref} ({fp.value or fp.lib_id}) is a {kind}; '{proposed}' is a role for an active part."}
        return {**base, "ok": None, "message": f"The symbol value '{fp.value}' does not decide the role of {fp.ref}; the proposed role '{proposed}' is unchecked."}
    exact, words_ok = _family_roles(sym["family"])
    words = set(norm.split("_"))
    reading = _reading(sym)
    if norm in exact or (words & words_ok):
        return {**base, "ok": True, "message": f"'{proposed}' fits {fp.ref}: {reading}."}
    return {**base, "ok": False, "message": f"'{proposed}' does not fit {fp.ref}: {reading}, so its role is {sym['role']}."}


def _reading(sym: dict[str, Any]) -> str:
    family = sym["family"].replace("_", " ")
    if sym["source"] == "value":
        return f"the symbol value '{sym['value']}' reads as {family} (matched '{sym['evidence']}')"
    return f"the library id '{sym['lib_id']}' reads as {family} (symbol value '{sym['value']}')"


def _family_roles(family: str) -> tuple[frozenset[str], frozenset[str]]:
    for _rx, fam, _role, exact, words in _SYMBOL_FAMILIES:
        if fam == family:
            return exact, words
    for kind, (fam, roles) in _KIND_ROLES.items():
        if fam == family:
            return roles, roles
    if family == "diode":
        return _P({"diode", "rectifier", "protection", "digital"}), _P({"diode", "rectifier", "protection"})
    return _P(), _P()


def is_protection_part(fp: Footprint, ctx: AnalyzerContext | None = None) -> bool:
    """ESD, TVS or other protection part: by the symbol first, by the brief's role only when the symbol is silent."""
    sym = symbol_role(fp)
    if sym is not None:
        return sym["family"] in ("esd_protection",)
    given = str((ctx.part_roles or {}).get(fp.ref, "")).strip().lower() if ctx is not None else ""
    return _norm_role(given) in ("protection", "esd", "esd_protection", "tvs", "usb_protection")


def part_roles_report(board: Board, ctx: AnalyzerContext) -> dict[str, dict[str, Any]]:
    """Per reference: the role used, where it came from, the symbol's reading and any conflict with the brief."""
    roles = classify_parts(board, ctx)
    given = {k: str(v).strip().lower() for k, v in (ctx.part_roles or {}).items()}
    out: dict[str, dict[str, Any]] = {}
    for f in sorted(board.footprints, key=lambda f: f.ref):
        sym = symbol_role(f)
        row: dict[str, Any] = {"role": roles.get(f.ref), "value": f.value, "symbol_family": sym["family"] if sym else None, "symbol_evidence": sym["evidence"] if sym else None}
        if f.ref in given:
            chk = check_role(f, given[f.ref])
            conflict = chk["ok"] is False
            row.update(brief_role=given[f.ref], source="symbol" if conflict else "brief", conflict=conflict)
            if conflict:
                row["message"] = chk["message"]
        else:
            row.update(source="symbol" if sym else "heuristic", conflict=False)
        out[f.ref] = row
    return out


def classify_parts(board: Board, ctx: AnalyzerContext) -> dict[str, str]:
    """Role per footprint reference: the brief's roles first, then heuristics. Deterministic."""
    given = {k: str(v).strip().lower() for k, v in (ctx.part_roles or {}).items()}
    roles: dict[str, str] = {}
    pads_by_ref = {f.ref: board.pads_of(f.ref) for f in board.footprints}
    nets_of = {ref: sorted({p.net for p in pads if p.net and not is_ground(p.net)}) for ref, pads in pads_by_ref.items()}
    has_switcher = any(_module_role(f) == "switching" for f in board.footprints) or any(_SW_NET_RE.search(n.name.upper()) for n in board.nets)
    anchors: list[tuple[Footprint, str]] = []

    conflicts: dict[str, dict[str, Any]] = ctx.cache.setdefault("role_conflicts", {}) if ctx is not None else {}
    for f in board.footprints:
        if f.ref in given:
            chk = check_role(f, given[f.ref])
            if chk["ok"] is False:
                # the symbol value overrules a brief role that cannot be right for the part (an ESD array called a regulator)
                sym = symbol_role(f)
                roles[f.ref] = sym["role"] if sym else given[f.ref]
                conflicts[f.ref] = {"brief_role": given[f.ref], "role_used": roles[f.ref], "message": chk["message"]}
                continue
            roles[f.ref] = given[f.ref]
            continue
        kind = part_kind(f)
        if kind in ("testpoint", "fiducial", "mounting"):
            roles[f.ref] = "mechanical"
            continue
        if kind == "antenna":
            roles[f.ref] = "rf"
            continue
        if kind == "xtal":
            roles[f.ref] = "clock"
            continue
        if kind == "connector":
            roles[f.ref] = "usb" if _USB_PART_RE.search(f"{f.value} {f.lib_id}") else "connector"
            continue
        if kind in ("ic",):
            role = _module_role(f) or "digital"
            roles[f.ref] = role
            anchors.append((f, role))
            continue
        if kind == "ind":
            nets = nets_of.get(f.ref, [])
            value = parse_value(f.value)
            if any(is_rf_net(n, ctx) for n in nets) or (value is not None and value < 100e-9):
                roles[f.ref] = "rf"
            elif any(_SW_NET_RE.search(n.upper()) for n in nets) or (has_switcher and value is not None and value >= 1e-6):
                roles[f.ref] = "switching"
            else:
                roles[f.ref] = "power"
            continue
        if kind in ("diode", "led", "switch", "fuse", "other"):
            role = _module_role(f)
            if role:
                roles[f.ref] = role
            elif kind == "fuse":
                roles[f.ref] = "protection"
            elif kind in ("led", "switch"):
                roles[f.ref] = "digital"
            else:
                roles[f.ref] = "digital"
            continue
        # passives: cap res ferrite — by net names, then by the nearest active part sharing a signal net
        nets = nets_of.get(f.ref, [])
        if any(is_rf_net(n, ctx) for n in nets):
            roles[f.ref] = "rf"
        elif any(is_clock_net(n) and not _SWITCH_RE.search(n) for n in nets) and not any(is_power(n, ctx) for n in nets):
            roles[f.ref] = "clock"
        elif any(_USB_RE.search(n.upper()) for n in nets):
            roles[f.ref] = "usb"

    # passives left over follow the nearest active part that shares one of their nets
    for f in board.footprints:
        if f.ref in roles:
            continue
        nets = set(nets_of.get(f.ref, []))
        signal = {n for n in nets if not is_power(n, ctx)}
        best: tuple[float, str] | None = None
        # a shared signal net says more than a shared supply: try those first
        for pool in (signal, nets):
            if not pool:
                continue
            for a, role in anchors:
                if role == "mechanical" or not (pool & set(nets_of.get(a.ref, []))):
                    continue
                d = math.dist((f.x, f.y), (a.x, a.y))
                if best is None or d < best[0]:
                    best = (d, role)
            if best:
                break
        roles[f.ref] = best[1] if best else ("power" if any(is_power(n, ctx) for n in nets) else "digital")
    return roles


def role_of(roles: dict[str, str], ref: str) -> str:
    return roles.get(ref, "digital")


def footprint_ring(fp: Footprint) -> list[list[float]]:
    return fp.courtyard or geom.bbox_ring(fp.bbox)


# canonical groups the proximity rule looks at; a brief can use any of the listed synonyms
NOISE_ROLES = frozenset({"switching", "rf", "radio", "pa", "power_switching", "smps", "transmitter"})
SENSITIVE_ROLES = frozenset({"analog", "sensitive", "sensor", "adc", "reference", "lna", "receiver", "precision"})
