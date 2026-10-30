"""Part and net classification by name, used when no board brief supplies part roles.

Everything here is a heuristic over reference prefixes, values, library ids and net names. A
brief's ``part_roles`` always wins. Roles are short lowercase words:

``rf`` ``switching`` ``power`` ``analog`` ``digital`` ``clock`` ``usb`` ``connector``
``protection`` ``mechanical`` (mounting holes, fiducials and test points are never zoned).
"""

from __future__ import annotations

import math
import re
from typing import TYPE_CHECKING

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


def classify_parts(board: Board, ctx: AnalyzerContext) -> dict[str, str]:
    """Role per footprint reference: the brief's roles first, then heuristics. Deterministic."""
    given = {k: str(v).strip().lower() for k, v in (ctx.part_roles or {}).items()}
    roles: dict[str, str] = {}
    pads_by_ref = {f.ref: board.pads_of(f.ref) for f in board.footprints}
    nets_of = {ref: sorted({p.net for p in pads if p.net and not is_ground(p.net)}) for ref, pads in pads_by_ref.items()}
    has_switcher = any(_module_role(f) == "switching" for f in board.footprints) or any(_SW_NET_RE.search(n.name.upper()) for n in board.nets)
    anchors: list[tuple[Footprint, str]] = []

    for f in board.footprints:
        if f.ref in given:
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
