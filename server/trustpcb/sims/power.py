"""DC power delivery: IR drop, current density and via current on the real copper of each supply net.

For every supply net with a source (a regulator output or a connector power pin) the module rasterises the
copper (tracks, pads, zone fills, via pads) on every layer, joins the layers through vias and plated holes as
resistors, and solves the conduction problem with a sparse direct solver (``power_grid.py``). The return path
through ground is solved the same way, so a load sees the drop of its supply copper and the rise of its ground
copper relative to its source.

Currents are inputs. A load whose current nobody supplied gets a default by part kind, labelled as a default in
every result, and each load also carries its path resistance (drop per amp) so the answer survives a wrong
guess. Nothing here edits a design: ``widen_track``, ``add_via`` and ``set_current`` change a copy in memory.

Resistivity is copper at 20 C scaled by 0.393 %/K to ``temperature_c`` (default 25 C); sheet resistance is
resistivity over the copper thickness the stackup gives (35 um outer and 17.5 um inner when the board file
carries no stackup, and that is listed as an assumption).
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from ..analyzers import schematic as sch
from ..analyzers.base import AnalyzerContext
from ..analyzers.classify import classify_parts, is_dnp, is_ground, is_power, part_kind, parse_value
from . import power_grid as pg
from .base import Assumption, Overlay, SimResult, Simulator, run_cached, run_hash

#: what-if operations this simulator accepts
WHATIF_OPS = ("widen_track", "add_via", "set_current")

DEFAULT_ALLOWED_DROP_PCT = 5.0
DEFAULT_TEMPERATURE_C = 25.0
DEFAULT_PLATING_UM = 25.0
DEFAULT_TEMP_RISE_C = 10.0
DEFAULT_LED_VF = 2.0
DEFAULT_REGULATOR_EFFICIENCY = 0.85
DEFAULT_REGULATOR_IQ_A = 0.0001
DEFAULT_MAX_CELLS = 300_000
CHECK_RATIO = 2.0                   # when a finer grid is too big, the convergence check solves again with cells this many times wider
REFINE_RATIO = 1.5                  # otherwise the net is solved again with cells this many times narrower, and that finer result is the one reported
REFINE_CELL_CAP = 500_000           # most cells a finer check may have
DROP_FLOOR_V = 0.00005              # a difference under 0.05 mV at the assumed current cannot matter to a decision
DEFAULT_CONVERGENCE_TOL = 0.05
RESISTANCE_FLOOR_OHM = 0.001        # differences below one milliohm (two squares of 1 oz copper) are not a convergence failure
CONTINUITY_TOL = 1e-6
RESIDUAL_TOL = 1e-8

#: placeholder currents by the role the part classifier gives an active part. These are not datasheet values.
DEFAULT_CURRENT_BY_ROLE = {"rf": 0.030, "digital": 0.020, "analog": 0.001, "usb": 0.005, "protection": 0.0}
DEFAULT_CURRENT_ACTIVE = 0.010
NO_DC_KINDS = {"cap", "testpoint", "fiducial", "mounting", "switch", "xtal", "antenna", "diode", "led", "ind", "ferrite", "fuse"}

_NOMINAL_NAME = re.compile(r"^[+]?(\d+)V(\d+)$|^[+]?(\d+(?:\.\d+)?)V$", re.I)
_VBUS_NAME = re.compile(r"VBUS", re.I)
_IN_PIN = re.compile(r"^(VIN|IN|VI|VBUS|PVIN|VCC|VDD|VS\+?|\+VS|VBAT|VSUP|IN\+)\d*$", re.I)
_SUPPLY_PIN = re.compile(r"^(?:[AD]?V(?:DD|CC|IN|OUT|BUS|BAT|SYS|REG|S|SUP)[A-Z0-9_]*|DEC[A-Z0-9_]*|DCC[A-Z0-9_]*|\+V[A-Z0-9_]*|V\+|VO|VI|VOUT|IN|OUT)$", re.I)
_CONN_POWER_PIN = re.compile(r"VBUS|VIN|VCC|VDD|VBAT|VSYS|5V|3V3|12V|\+", re.I)
_CONN_POWER_PART = re.compile(r"usb|dc.?jack|barrel|power|batt|vin|supply", re.I)
_CONN_POWER_NET = re.compile(r"VBUS|VIN|VBAT|^\+?5V|^\+?12V|^\+?24V", re.I)
_SHIELD_PIN = re.compile(r"shield|^sh$|^s\d$|^mh", re.I)
_SWITCHER = re.compile(r"buck|boost|tps6[0-9]{3}|tps5[0-9]{4}|lm25[0-9]{2}|lm26[0-9]{2}|mp[0-9]{4}|sy8[0-9]{3}|xl[0-9]{4}|lt3[0-9]{3}|ltc3[0-9]{3}|st1s|dc-?dc|smps|mcp1640|ap34[0-9]{2}|aoz1", re.I)


# ============================================================================== stackup

def _copper_order(board) -> list[str]:
    names = board.copper_layers()
    return sorted(names, key=lambda n: (0, 0) if n == "F.Cu" else (2, 0) if n == "B.Cu" else (1, int(re.sub(r"\D", "", n) or 0)))


@dataclass
class Stack:
    names: list[str]
    thickness: list[float]
    z: list[float]
    assumed: bool
    note: str

    def index(self, name: str) -> int | None:
        return self.names.index(name) if name in self.names else None


def build_stack(board) -> Stack:
    names = _copper_order(board)
    stack = board.stackup or {}
    layers = stack.get("layers") or []
    cu = {x["name"]: x.get("thickness_mm") for x in layers if x.get("kind") == "copper"}
    n = len(names)
    assumed = bool(stack.get("assumed", True))
    thick: list[float] = []
    for i, nm in enumerate(names):
        t = cu.get(nm)
        if not t or t <= 0:
            t = 0.035 if i in (0, n - 1) else 0.0175
            assumed = True
        thick.append(float(t))
    # centre height of every copper layer, from the stackup order when it is complete
    z: list[float] = []
    seq = [x for x in layers if x.get("kind") in ("copper", "dielectric")]
    ok = bool(seq) and all(x.get("thickness_mm") for x in seq) and all(nm in cu for nm in names)
    if ok:
        pos = 0.0
        centre: dict[str, float] = {}
        for x in seq:
            t = float(x["thickness_mm"])
            if x.get("kind") == "copper":
                centre[x["name"]] = pos + t / 2
            pos += t
        z = [centre[nm] for nm in names]
    else:
        total = float(getattr(board, "thickness", 1.6) or 1.6)
        span = max(total - thick[0] / 2 - thick[-1] / 2, 0.1)
        z = [thick[0] / 2 + span * i / max(n - 1, 1) for i in range(n)]
        assumed = True
    note = "copper thickness and layer spacing read from the board stackup" if not assumed else "copper thickness and layer spacing are assumed (35 um outer, 17.5 um inner, board thickness spread evenly)"
    return Stack(names, thick, z, assumed, note)


# ============================================================================== discovery

@dataclass
class Load:
    ref: str
    net: str
    amps: float
    source: str                   # 'datasheet' | 'user' | 'default'
    cite: str
    pads: list[Any]
    kind: str                     # part kind
    role: str = ""
    through: str | None = None    # 'regulator' or 'series' when this load is a converter input
    known: bool = False
    note: str = ""

    @property
    def per_pad(self) -> float:
        return self.amps / max(len(self.pads), 1)


@dataclass
class Feeder:
    part: str
    from_net: str
    type: str                     # 'regulator' | 'series'
    ratio: float = 1.0
    iq: float = 0.0
    note: str = ""


@dataclass
class NetPlan:
    net: str
    vnom: float | None = None
    vnom_source: str = ""
    sources: list[Any] = field(default_factory=list)
    source_ref: str | None = None
    source_why: str = ""
    loads: list[Load] = field(default_factory=list)
    feeder: Feeder | None = None
    skipped: str | None = None
    demand: float = 0.0


@dataclass
class Plan:
    nets: dict[str, NetPlan]
    order: list[str]
    assumptions: list[Assumption]
    notes: list[str]
    dnp: set[str]
    roles: dict[str, str]
    schematic: bool
    series_ohm: dict[str, float]
    si: Any = None


def _vnom_from_name(name: str) -> float | None:
    m = _NOMINAL_NAME.match(name)
    if not m:
        return None
    if m.group(1) is not None:
        return float(f"{m.group(1)}.{m.group(2)}")
    return float(m.group(3))


def _vnom_from_value(value: str) -> float | None:
    m = re.search(r"(?<![\d.])(\d{1,2})V(\d)(?!\d)", value) or None
    if m:
        return float(f"{m.group(1)}.{m.group(2)}")
    m = re.search(r"[-_ ](\d{1,2}\.\d)(?![\d.])", value)
    return float(m.group(1)) if m else None


def _norm_current(v: Any) -> tuple[float, str, str] | None:
    """(amps, source, cite) from an input value: a number, or {amps, source, cite}."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v), "user", "entered by the user"
    if isinstance(v, dict) and isinstance(v.get("amps"), (int, float)):
        src = v.get("source") if v.get("source") in ("datasheet", "user", "default") else "user"
        return float(v["amps"]), src, str(v.get("cite") or ("datasheet" if src == "datasheet" else "entered by the user"))
    return None


def _pad_function(si, pad) -> tuple[str | None, str | None]:
    pin = si.pins.get((pad.ref, str(pad.number))) if si and si.found else None
    return (pin["name"], pin["type"]) if pin else (None, None)


def plan_power(board, ctx, inputs: dict, whatif: list[dict]) -> Plan:
    ctx = ctx or AnalyzerContext()
    si = sch.schematic_info(board, ctx)
    try:
        roles = classify_parts(board, ctx)
    except Exception:  # noqa: BLE001  (the roles only pick default currents)
        roles = {}
    fps = {f.ref: f for f in board.footprints}
    dnp = {f.ref for f in board.footprints if is_dnp(f) or (si.found and si.dnp.get(f.ref))}
    notes: list[str] = []
    assumptions: list[Assumption] = []
    pads_on: dict[str, list[Any]] = {}
    for p in board.pads:
        if p.net and p.ref not in dnp:
            pads_on.setdefault(p.net, []).append(p)

    wanted = inputs.get("nets")
    if wanted:
        cand = [n for n in wanted if n in pads_on or n in board.net_names()]
    else:
        cand = [n for n in board.net_names() if is_power(n, ctx) and not is_ground(n) and len(pads_on.get(n, [])) >= 2]
    cand = [n for n in cand if not is_ground(n)]
    plans = {n: NetPlan(n) for n in cand}

    # nominal voltages
    given_v = inputs.get("nominal_v") or {}
    for n, pl in plans.items():
        if isinstance(given_v.get(n), (int, float)):
            pl.vnom, pl.vnom_source = float(given_v[n]), "user"
        elif _vnom_from_name(n) is not None:
            pl.vnom, pl.vnom_source = _vnom_from_name(n), "board"
        elif _VBUS_NAME.search(n):
            pl.vnom, pl.vnom_source = 5.0, "default"

    # ---- sources, pass 1: regulator outputs (schematic power_out pins) or the user's choice
    user_src = inputs.get("sources") or {}
    for n, pl in plans.items():
        if n in user_src:
            wantedp = {str(x) for x in (user_src[n] if isinstance(user_src[n], list) else [user_src[n]])}
            pl.sources = [p for p in pads_on.get(n, []) if p.id in wantedp or p.ref in wantedp]
            if pl.sources:
                pl.source_ref, pl.source_why = pl.sources[0].ref, "chosen by the user"
            continue
        outs = [p for p in pads_on.get(n, []) if _pad_function(si, p)[1] == "power_out"]
        if outs:
            ref = outs[0].ref
            pl.sources = [p for p in outs if p.ref == ref]
            pl.source_ref, pl.source_why = ref, "schematic pin type power_out"
    # ---- pass 2: connector power pins
    for n, pl in plans.items():
        if pl.sources or n in user_src:
            continue
        conn = []
        for p in pads_on.get(n, []):
            fp = fps.get(p.ref)
            if fp is None or part_kind(fp) != "connector":
                continue
            name, _ = _pad_function(si, p)
            if si.found:
                if name and _CONN_POWER_PIN.search(name) and not _SHIELD_PIN.search(name):
                    conn.append(p)
            elif _CONN_POWER_PART.search(f"{fp.value} {fp.lib_id}") or _CONN_POWER_NET.search(n):
                conn.append(p)
        if conn:
            ref = conn[0].ref
            pl.sources = [p for p in conn if p.ref == ref]
            pl.source_ref, pl.source_why = ref, "connector power pin" if si.found else "connector on a supply net (no schematic to name the pin)"

    # ---- converters: a source part whose input pin sits on another supply net
    for n, pl in plans.items():
        if not pl.sources or pl.source_ref is None or pl.source_why.startswith("connector"):
            continue
        fp = fps.get(pl.source_ref)
        if fp is None:
            continue
        in_pads = []
        for p in board.pads_of(pl.source_ref):
            name, typ = _pad_function(si, p)
            if p.net and p.net != n and p.net in plans and not is_ground(p.net) and typ == "power_in" and (name is None or _IN_PIN.match(name) or True):
                in_pads.append(p)
        in_pads = [p for p in in_pads if _IN_PIN.match(_pad_function(si, p)[0] or "") or not si.found] or in_pads
        if in_pads and si.found:
            from_net = in_pads[0].net
            hay = f"{fp.value} {fp.lib_id}"
            switching = bool(_SWITCHER.search(hay)) or roles.get(fp.ref) == "switching"
            vin = plans[from_net].vnom
            vout = pl.vnom or _vnom_from_value(fp.value or "")
            if pl.vnom is None and vout:
                pl.vnom, pl.vnom_source = vout, "board"
            eta = float(inputs.get("regulator_efficiency", DEFAULT_REGULATOR_EFFICIENCY))
            iq = float(inputs.get("regulator_iq_a", DEFAULT_REGULATOR_IQ_A))
            if switching and vin and vout:
                pl.feeder = Feeder(fp.ref, from_net, "regulator", ratio=vout / (eta * vin), iq=iq, note=f"switching regulator, efficiency {eta:.0%} assumed")
                assumptions.append(Assumption(f"{fp.ref} efficiency", eta, "", "default", "switching regulator: input current = Vout x Iout / (efficiency x Vin)"))
            else:
                pl.feeder = Feeder(fp.ref, from_net, "regulator", ratio=1.0, iq=iq, note="linear regulator: input current = output current + quiescent current")
                if not switching:
                    assumptions.append(Assumption(f"{fp.ref} quiescent current", iq, "A", "default", "placeholder; replace with the datasheet value"))
    # ---- series elements pass the current on from an upstream net
    series_ok = {"fuse", "ferrite"}
    changed = True
    while changed:
        changed = False
        for n, pl in plans.items():
            if pl.sources or pl.skipped or n in user_src:
                continue
            for p in pads_on.get(n, []):
                fp = fps.get(p.ref)
                if fp is None:
                    continue
                kind = part_kind(fp)
                if kind not in series_ok and not (kind in ("res", "ind") and _is_jumper(fp, kind)):
                    continue
                others = [q for q in board.pads_of(fp.ref) if q.net and q.net != n and q.net in plans and (plans[q.net].sources or q.net in user_src)]
                if not others:
                    continue
                pl.sources = [p]
                pl.source_ref, pl.source_why = fp.ref, f"{kind} between {others[0].net} and {n}"
                pl.feeder = Feeder(fp.ref, others[0].net, "series", ratio=1.0, note=f"{kind} in series: its resistance is not part of the copper result")
                if pl.vnom is None:
                    pl.vnom, pl.vnom_source = plans[others[0].net].vnom, plans[others[0].net].vnom_source
                changed = True
                break
    for n, pl in plans.items():
        if not pl.sources:
            pl.skipped = "no source identified (no regulator output, connector power pin or series part feeds this net); name one in inputs.sources"

    # ---- loads
    cur_in = inputs.get("currents") or {}
    for w in whatif:
        if w.get("op") == "set_current":
            cur_in = {**cur_in, (f"{w['ref']}@{w['net']}" if w.get("net") else w["ref"]): float(w["amps"])}
    solved = [n for n, pl in plans.items() if not pl.skipped]

    # primary supply net of a part: where it has the most supply pads
    def supply_pads(ref: str, net: str) -> list[Any]:
        ps = [p for p in pads_on.get(net, []) if p.ref == ref]
        if si.found:
            sup = [p for p in ps if si.is_supply_pin(p.ref, p.number)]
            if sup:
                return sup
        return ps

    primary: dict[str, str] = {}
    for ref in {p.ref for n in solved for p in pads_on.get(n, [])}:
        best = max(solved, key=lambda n: (len(supply_pads(ref, n)) if any(q.ref == ref for q in pads_on.get(n, [])) else -1))
        primary[ref] = best

    leaf: dict[str, list[Load]] = {n: [] for n in solved}
    for n in solved:
        pl = plans[n]
        src_pads = {p.id for p in pl.sources}
        by_ref: dict[str, list[Any]] = {}
        for p in pads_on.get(n, []):
            if p.id in src_pads:
                continue
            by_ref.setdefault(p.ref, []).append(p)
        for ref, pads in by_ref.items():
            fp = fps.get(ref)
            if fp is None:
                continue
            kind = part_kind(fp)
            sp = [p for p in supply_pads(ref, n) if p in pads] or pads
            feeder_here = [m for m in solved if plans[m].feeder and plans[m].feeder.part == ref and plans[m].feeder.from_net == n]
            if feeder_here:
                continue                                   # the converter input is added below
            if ref == pl.source_ref:
                continue
            amps, source, cite, note, known = _load_current(board, plans, fps, si, roles, primary, cur_in, inputs, ref, n, kind, sp, assumptions)
            leaf[n].append(Load(ref, n, amps, source, cite, sp, kind, roles.get(ref, ""), None, known, note))
            # pads of the same part that are not supply pins stay probes

    # converter and series inputs: demand of the child net passes up
    demand: dict[str, float] = {}

    def need(n: str, seen: tuple[str, ...] = ()) -> float:
        if n in demand:
            return demand[n]
        if n in seen:
            return 0.0
        total = sum(l.amps for l in leaf.get(n, []))
        for m in solved:
            f = plans[m].feeder
            if f and f.from_net == n:
                total += _feeder_amps(f, need(m, seen + (n,)))
        demand[n] = total
        return total

    for n in solved:
        need(n)
    for m in solved:
        f = plans[m].feeder
        if not f or f.from_net not in leaf:
            continue
        up = f.from_net
        amps = _feeder_amps(f, demand[m])
        fp = fps.get(f.part)
        pads = [p for p in pads_on.get(up, []) if p.ref == f.part]
        if si.found and f.type == "regulator":
            sup = [p for p in pads if _pad_function(si, p)[1] == "power_in" and _IN_PIN.match(_pad_function(si, p)[0] or "")] or [p for p in pads if si.is_supply_pin(p.ref, p.number)] or pads
            pads = sup
        if not pads:
            continue
        ovr = _norm_current(cur_in.get(f"{f.part}@{up}", cur_in.get(f.part)))
        if ovr:
            amps, src, cite = ovr
            known = True
        else:
            src, cite, known = "default", f"computed: {f.note}; load on {m} is {demand[m] * 1000:.2f} mA", False
        leaf[up].append(Load(f.part, up, amps, src, cite, pads, part_kind(fp) if fp else "ic", roles.get(f.part, ""), f.type, known, f.note))
    # a converter input changes the demand of its own upstream: recompute
    demand.clear()
    for n in solved:
        need(n)
    for n in solved:
        plans[n].loads = leaf[n]
        plans[n].demand = sum(l.amps for l in leaf[n])
    order = _roots_first(plans, solved)
    series_ohm = {k: float(v) for k, v in (inputs.get("series_ohm") or {}).items() if isinstance(v, (int, float))}
    return Plan(plans, order, assumptions, notes, dnp, roles, si.found, series_ohm, si)


def _is_active(l: Load) -> bool:
    """A load that draws current or is an active part, as against a capacitor or test point that only shows the voltage of the net."""
    return l.amps > 0 or l.through is not None or l.kind in ("ic", "connector", "other")


def _entry_active(e: dict) -> bool:
    return e["amps"] > 0 or e["through"] is not None or e["kind"] in ("ic", "connector", "other")


def _is_jumper(fp, kind: str) -> bool:
    if kind == "res":
        v = parse_value(fp.value or "", "R")
        return v is not None and v <= 1.0
    return False


def _feeder_amps(f: Feeder, child_demand: float) -> float:
    return child_demand * f.ratio + (f.iq if f.type == "regulator" else 0.0)


def _roots_first(plans: dict[str, NetPlan], solved: list[str]) -> list[str]:
    out: list[str] = []
    todo = list(solved)
    while todo:
        for n in list(todo):
            f = plans[n].feeder
            if f is None or f.from_net in out or f.from_net not in solved:
                out.append(n)
                todo.remove(n)
    return out


def _load_current(board, plans, fps, si, roles, primary, cur_in, inputs, ref, net, kind, pads, assumptions) -> tuple[float, str, str, str, bool]:
    """(amps, source, cite, note, known) for one part on one supply net."""
    for key in (f"{ref}@{net}", ref):
        got = _norm_current(cur_in.get(key))
        if got:
            return got[0], got[1], got[2], "", True
    if primary.get(ref) != net and kind == "ic":
        return 0.0, "default", "not assumed: this is not the part's main supply net, current unknown", "current unknown on this net, 0 A used", False
    if kind == "res":
        fp = fps[ref]
        r = parse_value(fp.value or "", "R")
        vnom = plans[net].vnom
        if r and vnom:
            other = [p for p in board.pads_of(ref) if p.net != net]
            other_net = other[0].net if other else None
            led = other_net and any(part_kind(fps[q.ref]) in ("led", "diode") for q in board.pads if q.net == other_net and q.ref in fps)
            vf = float(inputs.get("led_vf", DEFAULT_LED_VF))
            if other_net and is_ground(other_net):
                i, why = vnom / r, f"{vnom:g} V across {r:g} ohm to ground (upper bound)"
            elif led:
                i, why = max(vnom - vf, 0.0) / r, f"({vnom:g} V - {vf:g} V LED drop) across {r:g} ohm"
                assumptions.append(Assumption("LED forward voltage", vf, "V", "default", "typical value; replace with the datasheet value"))
            else:
                i, why = vnom / r, f"{vnom:g} V across {r:g} ohm with the far end at 0 V (upper bound for a pull-up)"
            return i, "default", "estimated from the resistor value: " + why, "", False
        return 0.0, "default", "resistor value or supply voltage unknown; 0 A used", "", False
    if kind in NO_DC_KINDS:
        return 0.0, "default", f"{kind}: no DC load assumed", "", False
    if kind == "connector":
        return 0.0, "default", "connector pin that is not a source: no load assumed", "", False
    role = roles.get(ref, "")
    if role in DEFAULT_CURRENT_BY_ROLE:
        return DEFAULT_CURRENT_BY_ROLE[role], "default", f"placeholder for a part with role '{role}'; not from a datasheet", "", False
    return DEFAULT_CURRENT_ACTIVE, "default", "placeholder for an unclassified active part; not from a datasheet", "", False


# ============================================================================== problem building

@dataclass
class Mods:
    widths: dict[str, float] = field(default_factory=dict)       # track id -> width
    net_widths: dict[str, tuple[float, str | None]] = field(default_factory=dict)
    vias: list[dict[str, Any]] = field(default_factory=list)


def parse_whatif(board, whatif: list[dict]) -> tuple[Mods, str | None]:
    mods = Mods()
    nets = set(board.net_names())
    refs = {f.ref for f in board.footprints}
    for w in whatif or []:
        op = w.get("op")
        if op == "widen_track":
            net = w.get("net")
            width = w.get("width_mm")
            if net not in nets:
                return mods, f"widen_track: unknown net {net!r}"
            if not isinstance(width, (int, float)) or not (0.02 <= width <= 50):
                return mods, "widen_track: width_mm must be a number between 0.02 and 50"
            ids = w.get("tracks") or ([w["track"]] if w.get("track") else [])
            if ids:
                for i in ids:
                    t = board._track.get(i)
                    if t is None or t.net != net:
                        return mods, f"widen_track: {i!r} is not a track of net {net}"
                    mods.widths[i] = float(width)
            else:
                if not any(t.net == net for t in board.tracks):
                    return mods, f"widen_track: net {net} has no tracks"
                mods.net_widths[net] = (float(width), w.get("layer"))
        elif op == "add_via":
            at = w.get("at")
            net = w.get("net")
            if net not in nets:
                return mods, f"add_via: unknown net {net!r}"
            if not (isinstance(at, (list, tuple)) and len(at) == 2 and all(isinstance(v, (int, float)) for v in at)):
                return mods, "add_via: at must be [x, y] in millimetres"
            d = float(w.get("diameter_mm") or 0.0) or None
            dr = float(w.get("drill_mm") or 0.0) or None
            mods.vias.append({"net": net, "x": float(at[0]), "y": float(at[1]), "d": d, "drill": dr})
        elif op == "set_current":
            if w.get("ref") not in refs:
                return mods, f"set_current: unknown part {w.get('ref')!r}"
            if not isinstance(w.get("amps"), (int, float)) or w["amps"] < 0:
                return mods, "set_current: amps must be a number of at least 0"
        else:
            return mods, f"what-if {op!r} is not accepted by the power simulator (accepts: {', '.join(WHATIF_OPS)})"
    return mods, None


def _track_width(t, mods: Mods) -> float:
    if t.id in mods.widths:
        return mods.widths[t.id]
    nw = mods.net_widths.get(t.net)
    if nw and (nw[1] is None or nw[1] == t.layer):
        return nw[0]
    return t.width


def _ring_area(ring) -> float:
    a = 0.0
    for i in range(len(ring)):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % len(ring)]
        a += x1 * y2 - x2 * y1
    return abs(a) / 2


@dataclass
class NetModel:
    problem: pg.Problem
    w_min: float
    area: float
    pad_terms: dict[str, Any]          # terminal name -> pad
    flags: list[dict]
    via_info: dict[str, dict]
    track_info: list[dict]


def _barrel(stack: Stack, layers_idx: list[int], drill: float, plating: float, rho: float, bid: str, x: float, y: float, radius: float) -> pg.Barrel:
    seg = []
    for a, b in zip(layers_idx[:-1], layers_idx[1:]):
        length = abs(stack.z[b] - stack.z[a])
        seg.append(pg.barrel_resistance(max(drill, 0.05), plating, max(length, 0.01), rho))
    return pg.Barrel(bid, x, y, radius, layers_idx, seg)


def build_net_model(board, stack: Stack, net: str, mods: Mods, opts: dict, terminals: list[pg.Terminal], pad_terms: dict[str, Any]) -> NetModel:
    rho = pg.rho_at(opts["temperature_c"])
    plating = opts["plating_mm"]
    nl = len(stack.names)
    sheet = [rho / t for t in stack.thickness]
    polys: dict[int, list[pg.Ring]] = {i: [] for i in range(nl)}
    barrels: list[pg.Barrel] = []
    flags: list[dict] = []
    w_min = math.inf
    area = 0.0
    track_info: list[dict] = []
    via_info: dict[str, dict] = {}
    for t in board.tracks:
        if t.net != net:
            continue
        li = stack.index(t.layer)
        if li is None:
            continue
        w = _track_width(t, mods)
        pts = t.points()
        for a, b in zip(pts[:-1], pts[1:]):
            polys[li].append(pg.capsule_ring(a, b, w))
        w_min = min(w_min, w)
        area += t.length * w
        track_info.append({"id": t.id, "layer": t.layer, "li": li, "width": w, "length": t.length, "pts": pts})
    for p in board.pads:
        if p.net != net or not p.ring:
            continue
        lidx = [stack.index(l) for l in p.layers if stack.index(l) is not None]
        for li in lidx:
            polys[li].append([tuple(q) for q in p.ring])
        area += _ring_area(p.ring) * max(len(lidx), 1)
        w_min = min(w_min, max(min(p.w, p.h), 0.1) * 0.8)
        if p.drill and len(lidx) >= 2:
            lidx = sorted(lidx)
            barrels.append(_barrel(stack, lidx, p.drill, plating, rho, p.id, p.x, p.y, max(min(p.w, p.h) / 2, p.drill / 2)))
    vias = [dict(id=v.id, x=v.x, y=v.y, d=v.diameter, drill=v.drill, layers=v.layers) for v in board.vias if v.net == net]
    for k, v in enumerate(mods.vias):
        if v["net"] != net:
            continue
        ref_via = next((q for q in board.vias), None)
        d = v["d"] or (ref_via.diameter if ref_via else 0.6)
        dr = v["drill"] or (ref_via.drill if ref_via else 0.3)
        vias.append(dict(id=f"whatif_via:{k + 1}", x=v["x"], y=v["y"], d=d, drill=dr, layers=[stack.names[0], stack.names[-1]]))
    for v in vias:
        lidx = [stack.index(l) for l in v["layers"] if stack.index(l) is not None]
        if len(lidx) < 2:
            continue
        lo, hi = min(lidx), max(lidx)
        span = list(range(lo, hi + 1))
        for li in span:
            polys[li].append(pg.circle_ring(v["x"], v["y"], v["d"] / 2))
        barrels.append(_barrel(stack, span, v["drill"], plating, rho, v["id"], v["x"], v["y"], v["d"] / 2))
        area += math.pi * (v["d"] / 2) ** 2 * len(span)
        via_info[v["id"]] = {"x": v["x"], "y": v["y"], "diameter": v["d"], "drill": v["drill"], "layers": [stack.names[lo], stack.names[hi]]}
    for z in board.zones:
        if z.net != net or z.is_keepout:
            continue
        if not z.filled:
            flags.append({"zone": z.id, "layers": z.layers, "problem": "zone has no stored fill (fill it in KiCad and save), so its copper is missing from this model"})
            continue
        for lname, rings in z.filled.items():
            li = stack.index(lname)
            if li is None:
                continue
            for r in rings:
                polys[li].append([tuple(q) for q in r])
                area += _ring_area(r)
    if not math.isfinite(w_min):
        w_min = 0.2
    prob = pg.Problem(stack.names, sheet, {k: v for k, v in polys.items() if v}, terminals, barrels)
    return NetModel(prob, w_min, area, pad_terms, flags, via_info, track_info)


def choose_pitch(model: NetModel, opts: dict) -> float:
    if opts.get("resolution_mm"):
        return float(opts["resolution_mm"])
    h = min(max(model.w_min / 5.0, 0.02), 0.25)
    cells = model.area / (h * h)
    if cells > opts["max_cells"]:
        h = math.sqrt(model.area / opts["max_cells"])
    return min(h, 0.5)


# ============================================================================== simulator

def _ipc_limit_a(width_mm: float, thickness_mm: float, rise_c: float, external: bool) -> float:
    """IPC-2221 current for a conductor of this cross-section, amps. A guide, not a rating."""
    area_mil2 = (width_mm / 0.0254) * (thickness_mm / 0.0254)
    k = 0.048 if external else 0.024
    return k * rise_c ** 0.44 * area_mil2 ** 0.725


def _via_limit_a(drill: float, plating: float, rise_c: float) -> float:
    area_mm2 = math.pi * plating * (drill + plating)
    return 0.024 * rise_c ** 0.44 * (area_mm2 / 0.0254 ** 2) ** 0.725


class PowerSimulator(Simulator):
    kind = "power"
    label = "DC power delivery (IR drop)"
    whatif_ops = WHATIF_OPS

    def available(self, board, ctx) -> tuple[bool, str]:
        if not board.copper_layers():
            return False, "the board has no copper layers"
        ctx = ctx or AnalyzerContext()
        if not any(is_power(n, ctx) and not is_ground(n) for n in board.net_names()):
            return False, "no supply net found (name one in inputs.nets)"
        return True, ""

    def inputs_needed(self, board, ctx) -> list[dict]:
        ctx = ctx or AnalyzerContext()
        plan = plan_power(board, ctx, {}, [])
        out: list[dict] = [
            {"id": "allowed_drop_pct", "path": "allowed_drop_pct", "label": "Allowed voltage drop", "unit": "%", "default": DEFAULT_ALLOWED_DROP_PCT, "source": "default",
             "cite": "placeholder; set per net with {net: pct}", "needed": False},
            {"id": "temperature_c", "path": "temperature_c", "label": "Copper temperature", "unit": "C", "default": DEFAULT_TEMPERATURE_C, "source": "default",
             "cite": "copper resistance rises 0.393 % per kelvin", "needed": False},
        ]
        for n in plan.order:
            pl = plan.nets[n]
            out.append({"id": f"nominal_v:{n}", "path": f"nominal_v.{n}", "label": f"Nominal voltage of {n}", "unit": "V", "default": pl.vnom, "source": pl.vnom_source or "unknown",
                        "cite": "from the net name, regulator value or USB convention" if pl.vnom else "unknown: percent drop cannot be given", "needed": pl.vnom is None})
            out.append({"id": f"sources:{n}", "path": f"sources.{n}", "label": f"Source of {n}", "default": [p.id for p in pl.sources], "source": "board", "cite": pl.source_why, "needed": False})
            for l in pl.loads:
                out.append({"id": f"current:{l.ref}@{n}", "path": f"currents.{l.ref}@{n}", "label": f"Current drawn by {l.ref} from {n}", "unit": "A", "default": round(l.amps, 6), "source": l.source,
                            "cite": l.cite, "needed": not l.known and l.amps > 0 and l.through is None, "ref": l.ref, "net": n, "derived": l.through is not None})
        for n, pl in plan.nets.items():
            if pl.skipped:
                out.append({"id": f"sources:{n}", "path": f"sources.{n}", "label": f"Source of {n}", "default": [], "source": "unknown", "cite": pl.skipped, "needed": False})
        for n, pl in plan.nets.items():
            f = pl.feeder
            if f and f.type == "series":
                out.append({"id": f"series_ohm:{f.part}", "path": f"series_ohm.{f.part}", "label": f"Resistance of {f.part} in series", "unit": "ohm", "default": None, "source": "unknown",
                            "cite": "not included in the copper result unless given", "needed": False})
        return out

    # ------------------------------------------------------------------ run
    def run(self, board, ctx, inputs: dict, whatif: list[dict], out_dir: Path) -> SimResult:
        t_all = time.perf_counter()
        ctx = ctx or AnalyzerContext()
        inputs = dict(inputs or {})
        whatif = list(whatif or [])
        ok, why = self.available(board, ctx)
        if not ok:
            return SimResult(self.kind, "not_possible", why)
        mods, bad = parse_whatif(board, whatif)
        if bad:
            return SimResult(self.kind, "not_possible", bad)
        stack = build_stack(board)
        opts = {
            "temperature_c": float(inputs.get("temperature_c", DEFAULT_TEMPERATURE_C)),
            "plating_mm": float(inputs.get("plating_um", DEFAULT_PLATING_UM)) / 1000.0,
            "rise_c": float(inputs.get("temp_rise_c", DEFAULT_TEMP_RISE_C)),
            "max_cells": int(inputs.get("max_cells", DEFAULT_MAX_CELLS)),
            "conv_tol": float(inputs.get("convergence_tol", DEFAULT_CONVERGENCE_TOL)),
            "resolution_mm": inputs.get("resolution_mm"),
            "include_return": bool(inputs.get("include_return", True)),
        }
        plan = plan_power(board, ctx, inputs, whatif)
        solved = [n for n in plan.order]
        res = SimResult(self.kind, "ok")
        res.assumptions = self._assumptions(plan, stack, opts, inputs)
        if not solved:
            res.status = "not_possible"
            res.reason = "no supply net has an identified source: " + "; ".join(f"{n}: {pl.skipped}" for n, pl in plan.nets.items()) if plan.nets else "no supply nets"
            return res

        results: dict[str, NetResult] = {}
        for n in solved:
            try:
                results[n] = self._solve_rail(board, stack, plan, plan.nets[n], mods, opts)
            except (ValueError, RuntimeError, MemoryError) as exc:
                plan.nets[n].skipped = f"solver error: {exc}"
        plan.order = [n for n in plan.order if n in results]
        if not results:
            res.status = "not_possible"
            res.reason = "no supply net could be solved: " + "; ".join(f"{n}: {pl.skipped}" for n, pl in plan.nets.items() if pl.skipped)
            return res
        ground = self._solve_return(board, stack, plan, results, mods, opts) if opts["include_return"] else {}
        self._assemble(res, board, stack, plan, results, ground, mods, opts, inputs, out_dir)
        if whatif:
            self._delta(res, board, ctx, inputs, out_dir)
        for n in plan.nets:
            if plan.nets[n].skipped:
                res.notes.append(f"{n}: not solved, {plan.nets[n].skipped}")
        res.solve_s = round(time.perf_counter() - t_all, 3)
        return res

    # ------------------------------------------------------------------ pieces
    def _assumptions(self, plan: Plan, stack: Stack, opts: dict, inputs: dict) -> list[Assumption]:
        out: list[Assumption] = []
        out.append(Assumption("copper temperature", opts["temperature_c"], "C", "user" if "temperature_c" in inputs else "default", "resistivity 1.724e-8 ohm m at 20 C, +0.393 % per kelvin"))
        out.append(Assumption("copper thickness (outer, inner)", f"{stack.thickness[0] * 1000:g} um, {stack.thickness[len(stack.thickness) // 2] * 1000:g} um", "", "default" if stack.assumed else "board", stack.note))
        out.append(Assumption("via and hole plating thickness", opts["plating_mm"] * 1000, "um", "user" if "plating_um" in inputs else "default", "typical 25 um; sets the resistance of every barrel"))
        out.append(Assumption("allowed voltage drop", inputs.get("allowed_drop_pct", DEFAULT_ALLOWED_DROP_PCT) if not isinstance(inputs.get("allowed_drop_pct"), dict) else "per net", "%",
                              "user" if "allowed_drop_pct" in inputs else "default", "limit the worst drop is compared with"))
        out.append(Assumption("current per pin", "equal share", "", "default", "a part's current is split equally between its supply pins on the net"))
        out.append(Assumption("sources", "ideal, equal voltage", "", "default", "all source pins of a net sit at the same voltage"))
        out.append(Assumption("current density guide", opts["rise_c"], "C rise", "user" if "temp_rise_c" in inputs else "default", "IPC-2221 formula for a 1 mm wide track, a guide not a rating"))
        out.extend(plan.assumptions)
        for n in plan.order:
            pl = plan.nets[n]
            if pl.vnom is not None:
                out.append(Assumption(f"nominal voltage {n}", pl.vnom, "V", pl.vnom_source or "default", "net name, regulator value or USB convention"))
            out.append(Assumption(f"source of {n}", ", ".join(p.id for p in pl.sources), "", "board", pl.source_why))
            for l in pl.loads:
                if l.amps > 0 or l.through:
                    out.append(Assumption(f"current {l.ref} on {n}", round(l.amps, 6), "A", l.source, l.cite))
        for ref, ohm in plan.series_ohm.items():
            out.append(Assumption(f"series resistance {ref}", ohm, "ohm", "user", "given by the user"))
        return out

    def _terminals(self, board, plan: Plan, pl: NetPlan, stack: Stack, pads_all: list[Any], extra_probe: bool = True) -> tuple[list[pg.Terminal], dict[str, Any]]:
        terms: list[pg.Terminal] = []
        pad_terms: dict[str, Any] = {}
        used: set[str] = set()

        def rings(p) -> dict[int, list[pg.Ring]]:
            return {stack.index(l): [[tuple(q) for q in p.ring]] for l in p.layers if stack.index(l) is not None}

        for p in pl.sources:
            terms.append(pg.Terminal(p.id, "source", rings(p)))
            used.add(p.id)
            pad_terms[p.id] = p
        for l in pl.loads:
            for p in l.pads:
                if p.id in used:
                    continue
                terms.append(pg.Terminal(p.id, "load", rings(p), l.per_pad))
                used.add(p.id)
                pad_terms[p.id] = p
        if extra_probe:
            for p in pads_all:
                if p.id not in used and p.ring:
                    terms.append(pg.Terminal(p.id, "probe", rings(p)))
                    pad_terms[p.id] = p
        return terms, pad_terms

    def _solve_rail(self, board, stack: Stack, plan: Plan, pl: NetPlan, mods: Mods, opts: dict) -> NetResult:
        pads_all = [p for p in board.pads if p.net == pl.net and p.ref not in plan.dnp]
        terms, pad_terms = self._terminals(board, plan, pl, stack, pads_all)
        model = build_net_model(board, stack, pl.net, mods, opts, terms, pad_terms)
        nr = NetResult(pl.net, "rail", model)
        self._solve_pair(nr, opts)
        return nr

    def _solve_pair(self, nr: NetResult, opts: dict) -> None:
        """Solve at the chosen pitch, then once more on a finer grid when that is affordable (the finer result is the one reported) or on a
        coarser grid when it is not. The two solutions feed the mesh convergence check."""
        model = nr.model
        h = choose_pitch(model, opts)
        first = pg.solve(model.problem, h, min_feature=model.w_min)
        if not opts.get("resolution_mm") and first.ncells * REFINE_RATIO ** 2 <= REFINE_CELL_CAP:
            nr.coarse, nr.sol, nr.h, nr.check_ratio = first, pg.solve(model.problem, h / REFINE_RATIO, min_feature=model.w_min), h / REFINE_RATIO, REFINE_RATIO
        else:
            nr.sol, nr.h, nr.check_ratio = first, h, CHECK_RATIO
            nr.coarse = pg.solve(model.problem, h * CHECK_RATIO, min_feature=model.w_min)

    def _solve_return(self, board, stack: Stack, plan: Plan, rails: dict[str, NetResult], mods: Mods, opts: dict) -> dict[str, NetResult]:
        grounds = [n for n in board.net_names() if is_ground(n)]
        fps = {f.ref: f for f in board.footprints}
        # return current of each part: what it draws from the rails, and for a converter what it keeps (input minus output)
        draw: dict[str, float] = {}
        sink_refs: list[str] = []
        for n in plan.order:
            pl = plan.nets[n]
            if pl.feeder is None and pl.source_ref and pl.source_why.startswith("connector"):
                sink_refs.append(pl.source_ref)
            for l in pl.loads:
                if l.through == "series":
                    continue
                draw[l.ref] = draw.get(l.ref, 0.0) + l.amps
        for n in plan.order:
            pl = plan.nets[n]
            if pl.feeder and pl.feeder.type == "regulator":
                draw[pl.feeder.part] = draw.get(pl.feeder.part, 0.0) - pl.demand
        out: dict[str, NetResult] = {}
        for g in grounds:
            pads = [p for p in board.pads if p.net == g and p.ref not in plan.dnp and p.ring]
            if not pads:
                continue
            sinks = [p for p in pads if p.ref in sink_refs and not _SHIELD_PIN.search(p.number) and not _SHIELD_PIN.search(_pad_function(plan.si, p)[0] or "")]
            if not sinks:
                continue
            by_ref: dict[str, list[Any]] = {}
            for p in pads:
                by_ref.setdefault(p.ref, []).append(p)
            pl = NetPlan(g)
            pl.sources = sinks
            pl.source_ref = sinks[0].ref
            pl.vnom = 0.0
            inject: dict[str, float] = {}
            for ref, a in draw.items():
                if a == 0.0 or ref in sink_refs:
                    continue
                if ref in by_ref:
                    inject[ref] = inject.get(ref, 0.0) + a
                    continue
                # the part has no pad on this ground (a resistor feeding an LED): its current returns through the parts on its far net
                far = {q.net for q in board.pads_of(ref) if q.net and not is_power(q.net) and not is_ground(q.net)}
                heirs = sorted({q.ref for n2 in far for q in board.pads if q.net == n2 and q.ref != ref and q.ref in by_ref and q.ref not in plan.dnp})
                for h_ref in heirs:
                    inject[h_ref] = inject.get(h_ref, 0.0) + a / len(heirs)
            for ref, a in inject.items():
                ps = by_ref[ref]
                gp = [p for p in ps if not _SHIELD_PIN.search(p.number) and not _SHIELD_PIN.search(_pad_function(plan.si, p)[0] or "")] or ps
                if abs(a) > 0.0:
                    pl.loads.append(Load(ref, g, a, "default", "return of the supply current drawn by this part", gp, part_kind(fps[ref]) if ref in fps else "ic", known=True))
            if not pl.loads:
                continue
            terms, pad_terms = self._terminals(board, plan, pl, stack, pads)
            model = build_net_model(board, stack, g, mods, opts, terms, pad_terms)
            nr = NetResult(g, "return", model)
            nr.plan = pl
            self._solve_pair(nr, opts)
            out[g] = nr
        return out

    # ------------------------------------------------------------------ results
    def _assemble(self, res: SimResult, board, stack: Stack, plan: Plan, rails: dict[str, NetResult], grounds: dict[str, NetResult], mods: Mods, opts: dict, inputs: dict, out_dir: Path) -> None:
        pct_in = inputs.get("allowed_drop_pct", DEFAULT_ALLOWED_DROP_PCT)
        # The ground return is solved first. Its numbers join the rails only when it passes its own checks; otherwise it is
        # listed on its own, flagged, and the supply drops are copper of the supply nets only.
        ground_tables = {g: self._net_table(board, stack, plan, nr.plan, nr, {}, {}, opts, pct_in, rails) for g, nr in grounds.items()}
        ground_ok = bool(grounds) and all(t["trust"] == "ok" for t in ground_tables.values())
        used = grounds if ground_ok else {}
        gnd_u: dict[str, dict[str, float]] = {g: dict(nr.sol.term_u) for g, nr in used.items()}      # ground net -> pad id -> rise at assumed currents (V)
        grounds_all, grounds = grounds, used
        detail: dict[str, Any] = {"nets": {}, "return": {}, "return_included": ground_ok, "stack": {"layers": stack.names, "thickness_mm": stack.thickness, "assumed": stack.assumed}}
        checks_all: list[dict] = []
        worst: dict[str, Any] = {"drop_mv": 0.0, "pct": None, "net": None, "ref": None}
        max_k = {"value": 0.0, "net": None, "layer": None}
        via_worst = {"amps": 0.0, "ratio": 0.0, "net": None, "id": None}
        total_assumed = 0
        n_loads = 0

        for n in plan.order:
            nr = rails[n]
            pl = plan.nets[n]
            table = self._net_table(board, stack, plan, pl, nr, grounds, gnd_u, opts, pct_in, rails)
            detail["nets"][n] = table
            for c in table["checks"]:
                checks_all.append({**c, "net": n})
            for l in table["loads"]:
                if l["amps"] > 0 or l["through"]:
                    n_loads += 1
                    total_assumed += 0 if l["current_known"] else 1
                dm = l["total_drop_mv"]
                if dm is not None and dm > worst["drop_mv"] and l["connected"] and _entry_active(l):
                    worst.update(drop_mv=dm, pct=l["total_drop_pct"], net=n, ref=l["ref"])
            hs = table["hotspots"]
            if hs and hs[0]["peak_a_per_mm"] > max_k["value"]:
                max_k.update(value=hs[0]["peak_a_per_mm"], net=n, layer=hs[0]["layer"])
            for v in table["vias"][:1]:
                if v["ratio"] > via_worst["ratio"]:
                    via_worst.update(amps=v["amps"], ratio=v["ratio"], net=n, id=v["id"])
        for g, table in ground_tables.items():
            detail["return"][g] = table
            for c in table["checks"]:
                # a return path that fails a check is flagged and left out of the totals; it does not fail the supply nets
                checks_all.append({**c, "net": g, "status": "flag" if c["status"] == "failed" else c["status"], "return_path": True})
            if ground_ok:
                hs = table["hotspots"]
                if hs and hs[0]["peak_a_per_mm"] > max_k["value"]:
                    max_k.update(value=hs[0]["peak_a_per_mm"], net=g, layer=hs[0]["layer"])
                for v in table["vias"][:1]:
                    if v["ratio"] > via_worst["ratio"]:
                        via_worst.update(amps=v["amps"], ratio=v["ratio"], net=g, id=v["id"])
        if grounds_all and not ground_ok:
            res.notes.append("The ground return did not pass its checks (" + "; ".join(c["detail"] for t in ground_tables.values() for c in t["checks"] if c["status"] == "failed") + "): its rise is left out of the totals and listed under detail.return.")
        elif not grounds_all:
            res.notes.append("No ground return was solved (no connector or source ground pad identified), so the totals are supply copper only.")
        ground_phrase = "supply plus ground" if ground_ok else "supply copper (ground return not included)"

        failed = [c for c in checks_all if c["status"] == "failed"]
        res.trust = {"status": "failed" if failed else "ok", "checks": checks_all}
        res.detail = detail
        m = res.metrics
        m["worst_drop_mv"] = {"value": round(worst["drop_mv"], 4), "unit": "mV", "label": f"Simulated worst {ground_phrase} drop at the assumed currents ({worst['ref']} on {worst['net']}); {total_assumed} of {n_loads} load currents are assumptions"}
        if worst["pct"] is not None:
            m["worst_drop_pct"] = {"value": round(worst["pct"], 4), "unit": "%", "label": "Simulated worst drop as a percent of the nominal voltage (assumed currents)"}
        worst_r = max(((l["r_path_mohm"], n, l["ref"]) for n in plan.order for l in detail["nets"][n]["loads"] if l["r_path_mohm"] is not None and l["connected"] and _entry_active(l)), default=None)
        if worst_r:
            m["worst_path_resistance_mohm"] = {"value": round(worst_r[0], 4), "unit": "mOhm", "label": f"Simulated resistance of the worst supply path, source to {worst_r[2]} on {worst_r[1]}; the drop per amp is this value in mV/A"}
        m["peak_current_density_a_per_mm"] = {"value": round(max_k["value"], 4), "unit": "A/mm", "label": f"Simulated peak sheet current density at the assumed currents ({max_k['net']}, {max_k['layer']})"}
        m["worst_via_current_a"] = {"value": round(via_worst["amps"], 5), "unit": "A", "label": f"Simulated largest current through one via or plated hole ({via_worst['id']} on {via_worst['net']}); {via_worst['ratio']:.0%} of the IPC-2221 estimate"}
        m["total_load_current_a"] = {"value": round(sum(plan.nets[n].demand for n in plan.order if plan.nets[n].feeder is None), 6), "unit": "A", "label": "Sum of load current drawn from the top-level sources (inputs, not simulated)"}
        m["currents_assumed"] = {"value": total_assumed, "unit": "count", "label": f"Load currents that are defaults or estimates, out of {n_loads}; none came from a datasheet unless listed as such"}
        for n in plan.order:
            t = detail["nets"][n]
            m[f"drop_mv:{n}"] = {"value": t["worst_drop_mv"], "unit": "mV", "label": f"Simulated worst drop of net {n} (copper only, assumed currents)"}
            if t["worst_drop_pct"] is not None:
                m[f"drop_pct:{n}"] = {"value": t["worst_drop_pct"], "unit": "%", "label": f"Simulated worst drop of net {n} as a percent of {plan.nets[n].vnom:g} V (assumed currents)"}
            for l in t["loads"]:
                if not _entry_active(l):
                    continue
                if l["connected"] and l["v_pin_min"] is not None:
                    m[f"v_pin:{l['ref']}@{n}"] = {"value": l["v_pin_min"], "unit": "V", "label": f"Simulated voltage at the lowest {l['ref']} supply pin on {n}, {'with the ground rise included' if ground_ok else 'ground return not included'} (current {'assumed' if not l['current_known'] else 'given'})"}
                elif not l["connected"]:
                    m[f"v_pin:{l['ref']}@{n}"] = {"value": None, "unit": "V", "label": f"{l['ref']} on {n} is not connected to the source in copper"}
        self._overlays(res, board, stack, plan, rails, used, opts, out_dir)
        res.notes.append("All numbers are simulated from the copper geometry and the listed assumptions; currents marked as defaults are placeholders, so read the per-amp path resistance for any of them.")
        if stack.assumed:
            res.notes.append("The board has no stackup: " + stack.note + ".")
        res.notes.extend(plan.notes)
        if failed:
            res.notes.append("A trust check failed: " + "; ".join(f"{c['net']} {c['id']}" for c in failed) + ". The numbers of those nets cannot support a finding.")

    def _net_table(self, board, stack: Stack, plan: Plan, pl: NetPlan, nr: NetResult, grounds: dict[str, NetResult], gnd_u: dict[str, dict[str, float]], opts: dict, pct_in: Any, rails: dict[str, NetResult]) -> dict[str, Any]:
        sol, other = nr.sol, nr.coarse
        model = nr.model
        names_load = {t: i for i, t in enumerate(sol.loads)}
        names_term = {t: i for i, t in enumerate(sol.terms)}
        names_load_c = {t: i for i, t in enumerate(other.loads)}
        names_term_c = {t: i for i, t in enumerate(other.terms)}
        vnom = pl.vnom if nr.role == "rail" else None
        allowed = pct_in.get(pl.net, DEFAULT_ALLOWED_DROP_PCT) if isinstance(pct_in, dict) else float(pct_in)
        loads_out: list[dict] = []
        conv_rows: list[tuple[str, float, float, float]] = []
        flagged: list[dict] = []
        # ground reference of the net's source part
        ref_gnd = {g: self._ref_ground(plan, pl, grounds.get(g), gnd_u.get(g, {})) for g in grounds} if nr.role == "rail" else {}
        up_drop, up_note = self._upstream(plan, pl, rails, grounds)
        for l in pl.loads:
            idx = [names_load[p.id] for p in l.pads if p.id in names_load]
            tidx = [names_term[p.id] for p in l.pads if p.id in names_term]
            connected = bool(tidx) and all(sol.term_connected.get(p.id, False) for p in l.pads)
            pad_u = [sol.term_u.get(p.id, float("nan")) for p in l.pads]
            r_mean = r_max = None
            if connected and idx:
                sub = sol.z[np.ix_(tidx, idx)]                       # drop at each pad per amp at each pad of the part
                per_pad = sub.sum(axis=1) / len(idx)                  # the part draws 1 A split equally
                r_mean, r_max = float(per_pad.mean()), float(per_pad.max())
                subc = other.z[np.ix_([names_term_c[p.id] for p in l.pads], [names_load_c[p.id] for p in l.pads])]
                rc = float((subc.sum(axis=1) / len(idx)).mean())
                if _is_active(l):
                    conv_rows.append((l.ref, r_mean, rc, l.amps))
            g_rise = None
            if nr.role == "rail" and grounds:
                g_rise = self._ground_rise(l, grounds, gnd_u, ref_gnd, board)
            drop = max(pad_u) if connected else None
            total = None
            if drop is not None:
                total = drop + (g_rise or 0.0) + up_drop
            v_pin = None
            if vnom is not None and total is not None:
                v_pin = vnom - total
            pct = (total / vnom * 100.0) if (vnom and total is not None) else None
            entry = {
                "ref": l.ref, "kind": l.kind, "role": l.role, "pads": [p.id for p in l.pads], "amps": round(l.amps, 6), "current_source": l.source, "current_known": l.known,
                "current_basis": l.cite, "through": l.through, "connected": connected,
                "drop_mv": None if drop is None else round(drop * 1000, 5),
                "ground_rise_mv": None if g_rise is None else round(g_rise * 1000, 5),
                "upstream_drop_mv": round(up_drop * 1000, 5) if up_drop else 0.0,
                "total_drop_mv": None if total is None else round(total * 1000, 5),
                "total_drop_pct": None if pct is None else round(pct, 5),
                "allowed_drop_pct": allowed, "within_allowed": None if pct is None else bool(pct <= allowed),
                "v_pin_min": None if v_pin is None else round(v_pin, 5),
                "r_path_mohm": None if r_mean is None else round(r_mean * 1000, 5),
                "r_path_worst_pad_mohm": None if r_max is None else round(r_max * 1000, 5),
                "drop_per_amp_mv": None if r_mean is None else round(r_mean * 1000, 5),
                "note": l.note,
            }
            if not connected:
                entry["problem"] = "not connected to the source through copper of this net: no voltage is reported"
                flagged.append({"ref": l.ref, "pads": [p.id for p in l.pads]})
            loads_out.append(entry)
        # probes: voltage at every other pad of the net
        pads_out = []
        for tid, p in model.pad_terms.items():
            role = "source" if any(s.id == tid for s in pl.sources) else "load" if tid in names_load else "probe"
            uu = sol.term_u.get(tid, float("nan"))
            pads_out.append({"pad": tid, "ref": p.ref, "role": role, "drop_mv": None if math.isnan(uu) else round(uu * 1000, 5), "connected": bool(sol.term_connected.get(tid, False))})

        checks: list[dict] = []
        s_amps = sum(sol.source_amps.values())
        i_conn = sum(t.amps for t in model.problem.terminals if t.role == "load" and sol.term_connected.get(t.name))
        if i_conn > 0:
            err = abs(s_amps - i_conn) / i_conn
            checks.append({"id": "current_continuity", "status": "ok" if err <= CONTINUITY_TOL else "failed", "value": err, "limit": CONTINUITY_TOL,
                           "detail": f"current out of the sources {s_amps:.6g} A, current into the connected loads {i_conn:.6g} A"})
        else:
            checks.append({"id": "current_continuity", "status": "ok", "value": 0.0, "limit": CONTINUITY_TOL, "detail": "no load current to balance (all zero)"})
        checks.append({"id": "solver_residual", "status": "ok" if sol.residual <= RESIDUAL_TOL else "failed", "value": sol.residual, "limit": RESIDUAL_TOL,
                       "detail": "relative residual of the sparse solve"})
        worst_rel, worst_ref, worst_abs = 0.0, None, 0.0
        for ref, rf, rc, amps in conv_rows:
            diff = abs(rf - rc)
            if diff * amps <= DROP_FLOOR_V and amps > 0:
                continue
            tol = max(opts["conv_tol"] * rf, RESISTANCE_FLOOR_OHM)
            rel = diff / tol * opts["conv_tol"] if tol > 0 else 0.0
            if rel > worst_rel:
                worst_rel, worst_ref, worst_abs = rel, ref, diff
        conv_ok = worst_rel <= opts["conv_tol"] + 1e-12
        checks.append({"id": "mesh_convergence", "status": "ok" if conv_ok else "failed", "value": round(worst_rel, 5), "limit": opts["conv_tol"],
                       "detail": f"path resistance of the loads at pitch {nr.h:.3f} mm against a grid at {nr.h * nr.check_ratio:.3f} mm: "
                                 f"largest difference {worst_rel:.1%}" + (f" ({worst_abs * 1000:.2f} mOhm at {worst_ref})" if worst_ref else "")
                                 + f"; differences under {RESISTANCE_FLOOR_OHM * 1000:g} mOhm or under {DROP_FLOOR_V * 1000:g} mV at the load current are ignored"})
        if flagged:
            checks.append({"id": "connected_network", "status": "flag", "value": len(flagged), "limit": 0,
                           "detail": "load not connected to its source through copper: " + ", ".join(f["ref"] for f in flagged)})
        else:
            checks.append({"id": "connected_network", "status": "ok", "value": 0, "limit": 0, "detail": "every load is connected to a source"})
        src_missing = [s.id for s in pl.sources if not sol.term_connected.get(s.id)]
        if src_missing:
            checks.append({"id": "source_in_copper", "status": "failed", "value": len(src_missing), "limit": 0, "detail": "source pad has no copper in the model: " + ", ".join(src_missing)})
        if model.flags:
            checks.append({"id": "zone_fill", "status": "failed", "value": len(model.flags), "limit": 0, "detail": "; ".join(f"{f['zone']}: {f['problem']}" for f in model.flags)})
        # hot spots, vias, tracks
        hot = self._hotspots(board, stack, nr, opts)
        vias = self._vias(stack, nr, opts)
        tracks = self._tracks(stack, nr, opts)
        drops = [l["total_drop_mv"] for l in loads_out if l["total_drop_mv"] is not None and l["connected"]]
        pcts = [l["total_drop_pct"] for l in loads_out if l["total_drop_pct"] is not None and l["connected"]]
        paths = [{"from": pl.source_ref, "to": l["ref"], "r_mohm": l["r_path_mohm"], "r_worst_pad_mohm": l["r_path_worst_pad_mohm"]} for l in loads_out if l["r_path_mohm"] is not None]
        return {
            "net": pl.net, "role": nr.role, "nominal_v": vnom, "nominal_source": pl.vnom_source, "source": {"ref": pl.source_ref, "pads": [p.id for p in pl.sources], "why": pl.source_why},
            "feeder": None if pl.feeder is None else {"part": pl.feeder.part, "from_net": pl.feeder.from_net, "type": pl.feeder.type, "note": pl.feeder.note},
            "pitch_mm": round(nr.h, 4), "cells": sol.ncells, "cells_per_layer": dict(zip(stack.names, sol.cells_per_layer)), "solve_s": round(nr.sol.solve_s + nr.coarse.solve_s + nr.sol.raster_s + nr.coarse.raster_s, 3),
            "source_current_a": round(s_amps, 6), "loads": loads_out, "pads": pads_out, "paths": paths, "hotspots": hot, "vias": vias, "tracks_top": tracks,
            "worst_drop_mv": round(max(drops), 5) if drops else 0.0, "worst_drop_pct": round(max(pcts), 5) if pcts else None,
            "checks": checks, "trust": "failed" if any(c["status"] == "failed" for c in checks) else "ok",
            "upstream_note": up_note,
        }

    def _upstream(self, plan: Plan, pl: NetPlan, rails: dict[str, NetResult], grounds: dict[str, NetResult]) -> tuple[float, str]:
        """Drop already taken upstream of a net fed through a series part: the upstream copper up to the part, the part's own
        resistance when the user gave it, and whatever lies further upstream."""
        f = pl.feeder
        if pl.net not in plan.nets or f is None or f.type != "series":
            return 0.0, ""
        up = rails.get(f.from_net)
        if up is None:
            return 0.0, ""
        us = [up.sol.term_u.get(p.id, float("nan")) for p in up.model.pad_terms.values() if p.ref == f.part]
        us = [u for u in us if not math.isnan(u)]
        base = max(us) if us else 0.0
        further, _ = self._upstream(plan, plan.nets[f.from_net], rails, grounds)
        r = plan.series_ohm.get(f.part)
        if r:
            return base + further + r * pl.demand, f"{f.part} {r:g} ohm included"
        return base + further, f"{f.part} is in series and its own drop is not included (give series_ohm.{f.part})"

    def _ref_ground(self, plan: Plan, pl: NetPlan, g: NetResult | None, gu: dict[str, float]) -> float:
        """Ground rise at the part whose ground pin the supply is measured against: the regulator or connector that starts this
        net, looking through series parts such as a fuse."""
        if g is None or pl.source_ref is None:
            return 0.0
        seen = set()
        while pl.feeder is not None and pl.feeder.type == "series" and pl.feeder.from_net in plan.nets and pl.net not in seen:
            seen.add(pl.net)
            pl = plan.nets[pl.feeder.from_net]
        src_ref = pl.source_ref
        if src_ref is None:
            return 0.0
        vals = [gu.get(p.id) for p in g.model.pad_terms.values() if p.ref == src_ref]
        vals = [v for v in vals if v is not None and not math.isnan(v)]
        if vals:
            return float(np.mean(vals))
        return 0.0

    def _ground_rise(self, l: Load, grounds: dict[str, NetResult], gnd_u: dict[str, dict[str, float]], ref_gnd: dict[str, float], board) -> float | None:
        best = None
        for g, nr in grounds.items():
            pads = [p for p in nr.model.pad_terms.values() if p.ref == l.ref]
            vals = [gnd_u[g].get(p.id) for p in pads]
            vals = [v for v in vals if v is not None and not math.isnan(v)]
            if vals:
                v = float(np.mean(vals)) - ref_gnd.get(g, 0.0)
                best = v if best is None else max(best, v)
        return best

    # ------------------------------------------------------------------ fields
    def _hotspots(self, board, stack: Stack, nr: NetResult, opts: dict) -> list[dict]:
        from scipy import ndimage

        sol = nr.sol
        out: list[dict] = []
        rise = opts["rise_c"]
        for li, name in enumerate(stack.names):
            k = sol.k[li]
            if not np.isfinite(k).any():
                continue
            ext = li in (0, len(stack.names) - 1)
            limit = _ipc_limit_a(1.0, stack.thickness[li], rise, ext)          # A per 1 mm of width
            filled = np.where(np.isfinite(k), k, 0.0)
            wt = np.isfinite(k).astype(np.float64)
            sm = ndimage.uniform_filter(filled, size=3, mode="constant") / np.maximum(ndimage.uniform_filter(wt, size=3, mode="constant"), 1e-9)
            sm = np.where(np.isfinite(k), sm, 0.0)
            over = sm > limit
            lab, n = ndimage.label(over, structure=np.ones((3, 3)))
            peaks: list[tuple[float, int, int]] = []
            if n:
                maxes = ndimage.maximum_position(sm, lab, index=range(1, n + 1))
                for pos in maxes:
                    peaks.append((float(sm[pos]), pos[0], pos[1]))
            if not peaks:
                j, i = np.unravel_index(int(np.argmax(sm)), sm.shape)
                if sm[j, i] > 0:
                    peaks.append((float(sm[j, i]), int(j), int(i)))
            for val, j, i in sorted(peaks, reverse=True)[:5]:
                x, y = sol.x0 + (i + 0.5) * sol.h, sol.y0 + (j + 0.5) * sol.h
                out.append({"layer": name, "x": round(x, 3), "y": round(y, 3), "peak_a_per_mm": round(val, 5), "limit_a_per_mm": round(limit, 4),
                            "ratio": round(val / limit, 4), "over_limit": bool(val > limit), "near": self._nearest(board, nr, name, x, y),
                            "limit_basis": f"IPC-2221, {rise:g} C rise, 1 mm wide, {'external' if ext else 'internal'} layer {stack.thickness[li] * 1000:g} um (a guide, not a rating)"})
        out.sort(key=lambda r: -r["peak_a_per_mm"])
        return out[:8]

    @staticmethod
    def _nearest(board, nr: NetResult, layer: str, x: float, y: float) -> str | None:
        best, bid = 0.3, None
        for t in nr.model.track_info:
            if t["layer"] != layer:
                continue
            for a, b in zip(t["pts"][:-1], t["pts"][1:]):
                d = _seg_dist(x, y, a, b) - t["width"] / 2
                if d < best:
                    best, bid = d, t["id"]
        for tid, p in nr.model.pad_terms.items():
            if layer in p.layers:
                d = math.hypot(p.x - x, p.y - y) - max(p.w, p.h) / 2
                if d < best:
                    best, bid = d, tid
        return bid

    def _vias(self, stack: Stack, nr: NetResult, opts: dict) -> list[dict]:
        sol = nr.sol
        out = []
        by_id = {b.id: b for b in nr.model.problem.barrels}
        for bid, amps in sol.barrel_amps.items():
            info = nr.model.via_info.get(bid)
            b = by_id[bid]
            drill = info["drill"] if info else None
            if drill is None:
                pad = nr.model.pad_terms.get(bid)
                drill = pad.drill if pad is not None and pad.drill else 0.3
            lim = _via_limit_a(drill, opts["plating_mm"], opts["rise_c"])
            out.append({"id": bid, "x": round(b.x, 3), "y": round(b.y, 3), "amps": round(amps, 6), "limit_a": round(lim, 4), "ratio": round(amps / lim, 5),
                        "r_mohm": round(sum(b.seg_ohm) * 1000, 4), "drill_mm": drill,
                        "limit_basis": "IPC-2221 internal-layer formula on the plated barrel cross-section, estimate"})
        out.sort(key=lambda r: -r["ratio"])
        return out[:12]

    def _tracks(self, stack: Stack, nr: NetResult, opts: dict) -> list[dict]:
        """Closed-form resistance and the field's current for the tracks that carry the most."""
        sol = nr.sol
        rho = pg.rho_at(opts["temperature_c"])
        rows = []
        for t in nr.model.track_info:
            r = rho * t["length"] / (t["width"] * stack.thickness[t["li"]])
            ks = []
            for a, b in zip(t["pts"][:-1], t["pts"][1:]):
                mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
                i, j = int((mx - sol.x0) / sol.h), int((my - sol.y0) / sol.h)
                if 0 <= i < sol.nx and 0 <= j < sol.ny:
                    kv = sol.k[t["li"], j, i]
                    if np.isfinite(kv):
                        ks.append(float(kv))
            amps = (float(np.mean(ks)) * t["width"]) if ks else 0.0
            rows.append({"id": t["id"], "layer": t["layer"], "width_mm": round(t["width"], 4), "length_mm": round(t["length"], 4), "r_mohm": round(r * 1000, 4),
                         "amps": round(amps, 6), "drop_mv": round(amps * r * 1000, 5)})
        rows.sort(key=lambda r: -r["drop_mv"])
        return rows[:10]

    def _overlays(self, res: SimResult, board, stack: Stack, plan: Plan, rails: dict[str, NetResult], grounds: dict[str, NetResult], opts: dict, out_dir: Path) -> None:
        x0, y0, x1, y1 = board.bbox
        pitch = max(0.1, math.sqrt(max((x1 - x0) * (y1 - y0), 1.0) / 400_000.0))
        nx, ny = int(math.ceil((x1 - x0) / pitch)) + 1, int(math.ceil((y1 - y0) / pitch)) + 1
        xs = x0 + (np.arange(nx) + 0.5) * pitch
        ys = y0 + (np.arange(ny) + 0.5) * pitch
        gx, gy = np.meshgrid(xs, ys)

        def paint(sets: list[NetResult], field: str, li: int, scale: float) -> np.ndarray:
            img = np.full((ny, nx), np.nan, dtype=np.float32)
            for nr in sets:
                s = nr.sol
                arr = getattr(s, field)[li]
                ii = np.floor((gx - s.x0) / s.h).astype(np.int64)
                jj = np.floor((gy - s.y0) / s.h).astype(np.int64)
                ok = (ii >= 0) & (ii < s.nx) & (jj >= 0) & (jj < s.ny)
                vals = np.full((ny, nx), np.nan, dtype=np.float32)
                vals[ok] = arr[jj[ok], ii[ok]]
                take = np.isfinite(vals) & ~np.isfinite(img)
                img[take] = vals[take] * scale
            return img

        groups = [("drop", "Voltage drop, supply copper", "mV", list(rails.values()), "u", 1000.0, "sequential"),
                  ("current", "Current density", "A/mm", list(rails.values()) + list(grounds.values()), "k", 1.0, "sequential")]
        if grounds:
            groups.append(("gndrise", "Voltage rise, ground copper", "mV", list(grounds.values()), "u", 1000.0, "sequential"))
        for oid, label, unit, sets, fld, scale, sc in groups:
            for li, lname in enumerate(stack.names):
                img = paint(sets, fld, li, scale)
                if not np.isfinite(img).any():
                    continue
                vmax = float(np.nanmax(img))
                fname = f"{oid}_{lname.replace('.', '_')}.bin"
                (out_dir).mkdir(parents=True, exist_ok=True)
                img.astype("<f4").tofile(out_dir / fname)
                res.overlays.append(Overlay(f"{oid}:{lname}", f"{label} ({lname}), simulated", unit, lname, float(x0), float(y0), float(pitch), float(pitch), int(nx), int(ny), 0.0, round(vmax, 6) if vmax > 0 else 1.0, fname, sc))

    # ------------------------------------------------------------------ what-if delta
    def _delta(self, res: SimResult, board, ctx, inputs: dict, out_dir: Path) -> None:
        try:
            if out_dir.parent.name == "sims":
                base, _, _ = run_cached(self, board, ctx, inputs, [], out_dir.parent.parent)
            else:
                base = self.run(board, ctx, inputs, [], out_dir / "baseline")
        except Exception as exc:  # noqa: BLE001
            res.notes.append(f"baseline for comparison could not be run: {exc}")
            return
        if base.status != "ok":
            return
        rows = []
        for n, t in res.detail.get("nets", {}).items():
            b = base.detail.get("nets", {}).get(n)
            if not b:
                continue
            bl = {l["ref"]: l for l in b["loads"]}
            for l in t["loads"]:
                o = bl.get(l["ref"])
                if o and o["drop_mv"] is not None and l["drop_mv"] is not None:
                    before = o["drop_mv"] + o["upstream_drop_mv"]
                    after = l["drop_mv"] + l["upstream_drop_mv"]
                    rows.append({"net": n, "ref": l["ref"], "before_mv": round(before, 5), "after_mv": round(after, 5), "change_mv": round(after - before, 5),
                                 "r_before_mohm": o["r_path_mohm"], "r_after_mohm": l["r_path_mohm"]})
        res.detail["delta"] = {"baseline_run": run_hash(self.kind, board, inputs, []), "baseline_worst_drop_mv": base.metrics.get("worst_drop_mv", {}).get("value"), "basis": "supply copper only (drop at the pin plus any upstream copper), so a flagged ground return cannot skew the comparison", "loads": rows}
        res.notes.append("What-if applied in simulation memory only; the design is not changed. Baseline comparison is under detail.delta.")


@dataclass
class NetResult:
    net: str
    role: str
    model: NetModel
    h: float = 0.0
    sol: pg.Solution | None = None
    coarse: pg.Solution | None = None        # the coarser grid of the convergence check, ``check_ratio`` times wider than ``sol``
    check_ratio: float = CHECK_RATIO
    plan: NetPlan | None = None


def _seg_dist(px: float, py: float, a, b) -> float:
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    if L2 == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


SIMULATOR = PowerSimulator()
