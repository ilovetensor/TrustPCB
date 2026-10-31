"""Signal analysis per net: impedance profile, coupling, return path detours and a TDR, with no Docker.

For each selected net the simulator walks the routed copper from a source pad to the farthest pad, takes a
cross-section every fraction of a millimetre, and solves each one with the 2D field solver in
``fieldsolve``: the real stackup, the real neighbouring copper on every layer and the real reference plane
(or the lack of it) at that point. That gives Z0, effective permittivity and delay along the net, the
capacitance and inductance matrices against nearby tracks (crosstalk for a stated rise time, differential
impedance and skew for pairs), and a lossless cascaded-line TDR. A shortest path through the ground copper
(any layer, joined by ground vias) measures how far the return current must detour where the reference is
missing, and the extra loop inductance that costs.

What the numbers are: quasi-static, lossless, no dispersion; every other copper shape is an AC ground in the
impedance solve. ``fieldsolve.valid_to_hz`` states the frequency up to which that holds. All numbers are
estimates from a model, labelled so, and every input is an ``Assumption`` with its source.
"""

from __future__ import annotations

import logging
import math
import re
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from shapely.geometry import Point

from ..analyzers.base import AnalyzerContext
from ..analyzers.classify import fast_nets, is_clock_net, is_diff_net, is_ground, is_power, is_rf_net
from ..analyzers.radiating import radiating
from ..pcb.model import Board
from . import fieldsolve as fs
from . import signal_board as sb
from .base import Assumption, Overlay, SimResult, Simulator

logger = logging.getLogger("trustpcb")

SIGNAL_VERSION = "1"

QUALITY = {
    "screen": {"fine": 0.5, "ratio": 1.4, "dmax_mm": 1.2},
    "confirm": {"fine": 0.3, "ratio": 1.3, "dmax_mm": 0.8},
}
HALF_WINDOW_MM = 12.0
SUPPORT_COVER = 0.8  # share of the band around a track that needs ground on the reference layer
GAP_MERGE_MM = 1.0
GAP_MIN_MM = 0.3
MAX_UNIQUE_SECTIONS = 30
MAX_RETURN_EVENTS = 6
WHATIF_OPS = ("widen_track", "remove_gap", "add_stitching")

# ------------------------------------------------------------------------------------------ interface kinds


@dataclass
class Interface:
    kind: str
    label: str
    rise_ns: float
    source: str  # user | datasheet | default
    cite: str


_USB_RE = re.compile(r"USB|(^|[^A-Z])(DP|DM)([^A-Z]|$)|D\+|D-", re.I)
_I2C_RE = re.compile(r"(^|[^A-Z])(SCL|SDA)([^A-Z]|$)|I2C", re.I)
_SPI_RE = re.compile(r"SPI|SCK|SCLK|MOSI|MISO|QSPI|(^|[^A-Z])CLK", re.I)
_SWD_RE = re.compile(r"SWD|SWCLK|SWDIO|TCK|TMS", re.I)
_FREQ_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([kKMG])\s*Hz", re.I)


def _crystal_hz(board: Board, net: str) -> float | None:
    refs = {p.ref for p in board.pads if p.net == net}
    for ref in sorted(refs):
        fp = board.footprint(ref)
        if fp is None:
            continue
        m = _FREQ_RE.search(fp.value or "")
        if m:
            scale = {"k": 1e3, "m": 1e6, "g": 1e9}[m.group(2).lower()]
            return float(m.group(1)) * scale
    return None


def interface_for(board: Board, ctx: AnalyzerContext, net: str, inputs: dict) -> Interface:
    """The rise time to use for ``net``: the user's, else a labelled default for the kind of interface."""
    user = inputs.get("rise_time_ns")
    if isinstance(user, dict) and net in user:
        return Interface("user", "user input", float(user[net]), "user", "stated by the user")
    if isinstance(user, (int, float)) and user > 0:
        return Interface("user", "user input", float(user), "user", "stated by the user for every net")
    n = net.upper()
    if is_rf_net(net, ctx):
        f = 2.48e9
        return Interface("rf", "2.4 GHz radio feed", 0.35 / f * 1e9, "default", "10 to 90 percent rise time of a 2.48 GHz knee (0.35 / f); the radio is a sine, not an edge. Above the quasi-static limit of this model.")
    if _USB_RE.search(n):
        return Interface("usb_fs", "USB 2.0 full speed", 4.0, "default", "USB 2.0 specification: full-speed driver rise and fall time 4 to 20 ns (10 to 90 percent); the fastest is taken because it is the worst case for crosstalk and reflections.")
    if is_clock_net(net) and re.search(r"XTAL|OSC|(^|[^A-Z])(XI|XO)|HSE|LSE|32K", n):
        f = _crystal_hz(board, net) or 32e6
        found = _crystal_hz(board, net) is not None
        tr = 0.2952 / f * 1e9
        return Interface("crystal", "crystal oscillator", tr, "default", f"10 to 90 percent rise of a sine at {f / 1e6:g} MHz is 0.295 / f. Frequency {'read from the crystal value' if found else 'assumed 32 MHz'}.")
    if _I2C_RE.search(n):
        return Interface("i2c", "I2C", 300.0, "default", "I2C-bus specification (UM10204): fast-mode rise time up to 300 ns; the slowest allowed, so the least severe case.")
    if _SWD_RE.search(n):
        return Interface("swd", "SWD debug", 2.0, "default", "Assumed 2 ns for a CMOS output at standard drive; no datasheet was read.")
    if _SPI_RE.search(n):
        return Interface("spi", "SPI or clock", 2.0, "default", "Assumed 2 ns for a CMOS output at standard drive; no datasheet was read.")
    return Interface("gpio", "general digital signal", 2.0, "default", "Assumed 2 ns for a CMOS output at standard drive; no datasheet was read.")


# ------------------------------------------------------------------------------------------ pairs

_PAIR_SWAPS = [("D+", "D-"), ("DP", "DM"), ("_P", "_N"), ("+", "-")]


def pair_partner(net: str, names: set[str]) -> str | None:
    """The other leg of a differential pair, by name, when it exists on the board."""
    for a, b in _PAIR_SWAPS:
        for x, y in ((a, b), (b, a)):
            i = net.rfind(x)
            if i >= 0:
                cand = net[:i] + y + net[i + len(x) :]
                if cand != net and cand in names:
                    return cand
    return None


# ------------------------------------------------------------------------------------------ section solving


@dataclass
class SecResult:
    z0: float
    ee: float
    tpd: float  # s/m
    c: np.ndarray
    l: np.ndarray  # noqa: E741
    nodes: int
    reused: bool = False


class SectionSolver:
    def __init__(self, quality: str, deadline: float):
        self.params = QUALITY[quality]
        self.deadline = deadline
        self.cache: dict[tuple, SecResult] = {}
        self.solved = 0
        self.hits = 0
        self.reused = 0
        self.solve_s = 0.0
        self.max_nodes = 0
        self.last: dict[int, SecResult] = {}

    def solve_fresh(self, sec: fs.Section, params: dict | None = None) -> SecResult:
        t0 = time.perf_counter()
        sol = fs.solve(sec, **(params or self.params))
        self.solve_s += time.perf_counter() - t0
        self.max_nodes = max(self.max_nodes, sol.nodes)
        return SecResult(sol.z0(0), sol.eps_eff(0), sol.delay_per_m(0), sol.c, sol.l, sol.nodes)

    def get(self, b: sb.SectionBuild) -> SecResult:
        key = (b.key, b.section.n_cond)
        r = self.cache.get(key)
        n = b.section.n_cond
        if r is not None:
            self.hits += 1
            self.last[n] = r
            return r
        if time.perf_counter() > self.deadline and n in self.last:
            self.reused += 1
            o = self.last[n]
            return SecResult(o.z0, o.ee, o.tpd, o.c, o.l, o.nodes, True)
        r = self.solve_fresh(b.section)
        self.solved += 1
        self.cache[key] = r
        self.last[n] = r
        return r


# ------------------------------------------------------------------------------------------ per net records


@dataclass
class Event:
    i0: int
    i1: int
    s0: float
    s1: float
    length_mm: float  # unsupported track length
    layer: str
    x: float
    y: float
    ret: sb.ReturnResult | None = None
    extra_nh: float = 0.0
    analysed: bool = False


@dataclass
class NetResult:
    net: str
    iface: Interface
    path: sb.NetPath
    samples: list[sb.Sample]
    target: float
    target_basis: str
    partner: str | None
    z0: np.ndarray
    ee: np.ndarray
    tpd: np.ndarray  # s/mm
    z_eff: np.ndarray
    tpd_eff: np.ndarray
    coupled: np.ndarray
    cover: np.ndarray
    supported: np.ndarray
    exempt: np.ndarray
    valid: np.ndarray
    events: list[Event] = field(default_factory=list)
    xtalk: list[dict[str, Any]] = field(default_factory=list)
    zdiff: np.ndarray | None = None
    zcm: np.ndarray | None = None
    tdr: dict[str, Any] = field(default_factory=dict)
    layer_geom: dict[str, Any] = field(default_factory=dict)
    sections: int = 0
    unique: int = 0
    quant: float = 0.005
    builds: list[Any] = field(default_factory=list)


_BIN_EDGES = (0.0, 0.15, 0.4, 0.8, 1.5, 3.0, 6.0, 12.0)


def _features(b: sb.SectionBuild) -> np.ndarray:
    """How much of each lateral band around the track is covered by each kind of copper, per layer.

    Bands get wider with distance from the track, so a small shift of copper near the track changes the vector
    and the same shift far away does not. Cross-sections with close vectors share one solve.
    """
    rects = b.section.rects
    vic = [r for r in rects if r.cond == 1 and r.x0 <= 0 <= r.x1]
    vx0, vx1 = (min(r.x0 for r in vic), max(r.x1 for r in vic)) if vic else (0.0, 0.0)
    layers = sorted({(r.z0, r.z1) for r in rects if r.z1 - r.z0 < 0.2} | {(r.z0, r.z1) for r in vic})
    conds = [0] + list(range(2, b.section.n_cond + 1))
    bands = [(vx0, vx1)]
    for lo, hi in zip(_BIN_EDGES, _BIN_EDGES[1:]):
        bands.append((vx1 + lo, vx1 + hi))
        bands.append((vx0 - hi, vx0 - lo))
    out = np.zeros((len(layers), len(conds), len(bands)))
    for r in rects:
        try:
            li = layers.index((r.z0, r.z1))
        except ValueError:
            continue
        if r.cond not in conds:
            continue
        ci = conds.index(r.cond)
        for k, (lo, hi) in enumerate(bands):
            ov = min(r.x1, hi) - max(r.x0, lo)
            if ov > 0:
                out[li, ci, k] = min(1.0, out[li, ci, k] + ov / max(hi - lo, 1e-9))
    return out.ravel()


def _cluster(builds: list[Any], scale: float) -> tuple[list[int | None], list[int]]:
    """Group cross-sections whose surroundings are alike, so each group needs one field solve.

    Returns the representative index of every build and the list of representatives. Two cross-sections group
    when they hold the same nets, the track is the same width, and the largest difference in band coverage
    is under a threshold that ``scale`` widens.
    """
    rep_of: list[int | None] = [None] * len(builds)
    reps: list[int] = []
    feats: dict[int, np.ndarray] = {}
    width: dict[int, float] = {}
    by_sig: dict[tuple, list[int]] = {}
    for i, b in enumerate(builds):
        if b is None:
            continue
        f = _features(b)
        vic = [r for r in b.section.rects if r.cond == 1 and r.x0 <= 0 <= r.x1]
        w = (max(r.x1 for r in vic) - min(r.x0 for r in vic)) if vic else 0.0
        sig = (tuple(b.cond_nets), b.section.n_cond, f.shape[0], tuple(sorted({(r.z0, r.z1) for r in b.section.rects if r.cond == 1})))
        found = None
        for ri in by_sig.get(sig, []):
            if abs(width[ri] - w) > 0.01 * max(w, 0.05):
                continue
            if float(np.max(np.abs(feats[ri] - f))) <= 0.3 * scale:
                found = ri
                break
        if found is None:
            reps.append(i)
            feats[i], width[i] = f, w
            by_sig.setdefault(sig, []).append(i)
            found = i
        rep_of[i] = found
    return rep_of, reps


def _merge_smallest(builds: list[Any], rep_of: list[int | None], reps: list[int], cap: int) -> tuple[list[int | None], list[int]]:
    """Fold the least used groups into their nearest neighbour until at most ``cap`` remain."""
    feats = {r: _features(builds[r]) for r in reps}
    sig = {r: (tuple(builds[r].cond_nets), builds[r].section.n_cond) for r in reps}
    members: dict[int, int] = {r: 0 for r in reps}
    for r in rep_of:
        if r is not None:
            members[r] += 1
    alive = set(reps)
    for r in sorted(reps, key=lambda q: members[q]):
        if len(alive) <= cap:
            break
        near = [(float(np.max(np.abs(feats[r] - feats[o]))), o) for o in alive if o != r and sig[o] == sig[r] and feats[o].shape == feats[r].shape]
        if not near:
            continue
        _, o = min(near)
        alive.discard(r)
        members[o] += members[r]
        rep_of = [o if q == r else q for q in rep_of]
    return rep_of, sorted(alive)


def _pair_modes(c: np.ndarray, l: np.ndarray, j: int) -> dict[str, float]:  # noqa: E741
    """Even and odd mode impedance and delay of a coupled pair (conductors 0 and ``j``), symmetrised."""
    cs = 0.5 * (c[0, 0] + c[j, j])
    ls = 0.5 * (l[0, 0] + l[j, j])
    cm = -0.5 * (c[0, j] + c[j, 0])
    lm = 0.5 * (l[0, j] + l[j, 0])
    c_odd, c_even = cs + cm, cs - cm
    l_odd, l_even = ls - lm, ls + lm
    return {
        "z_odd": math.sqrt(l_odd / c_odd),
        "z_even": math.sqrt(l_even / c_even),
        "tpd_odd": math.sqrt(l_odd * c_odd),
        "tpd_even": math.sqrt(l_even * c_even),
        "cm": cm,
        "lm": lm,
    }


# ------------------------------------------------------------------------------------------ the simulator


class SignalSimulator(Simulator):
    kind = "signal"
    label = "Signal integrity (impedance, crosstalk, return path, TDR)"
    whatif_ops = WHATIF_OPS

    # ---- availability and inputs
    def available(self, board: Board, ctx: Any) -> tuple[bool, str]:
        if not board.tracks:
            return False, "the board has no routed tracks to analyse"
        if len(board.copper_layers()) < 2:
            return False, "the stackup has fewer than two copper layers, so there is no reference plane to model"
        return True, "ok"

    def inputs_needed(self, board: Board, ctx: Any) -> list[dict]:
        actx = _analyzer_ctx(ctx)
        picks = self.pick_nets(board, actx, {})
        return [
            {"name": "nets", "label": "Nets to analyse", "default": picks, "unit": "", "source": "default", "note": "Net names. The default is the fast, radio and crystal nets, capped at max_nets. The review should choose these."},
            {"name": "max_nets", "label": "Most nets in one run", "default": 10, "unit": "", "source": "default", "note": "Cap on the net list; a pair counts as two."},
            {"name": "rise_time_ns", "label": "Rise time, 10 to 90 percent", "default": {n: interface_for(board, actx, n, {}).rise_ns for n in picks}, "unit": "ns", "source": "default", "note": "A number for every net or a map by net. Defaults are labelled per interface kind (USB 2.0 full speed 4 ns, I2C 300 ns, crystal 0.295 / f). Replace with the driver datasheet value."},
            {"name": "source_ohm", "label": "TDR source resistance", "default": 50.0, "unit": "ohm", "source": "default", "note": "Output resistance of the driver or instrument for the TDR plot."},
            {"name": "load_ohm", "label": "TDR load resistance", "default": None, "unit": "ohm", "source": "default", "note": "None is an open end (a CMOS input with no termination)."},
            {"name": "aggressor_swing_v", "label": "Aggressor swing", "default": 3.3, "unit": "V", "source": "default", "note": "Used to turn crosstalk fractions into volts."},
            {"name": "target_ohm", "label": "Impedance target", "default": {n: sb_target(actx, n)[0] for n in picks}, "unit": "ohm", "source": "board", "note": "Single-ended target per net; differential pair legs use half the differential target."},
            {"name": "driver_ref", "label": "Driver part reference", "default": None, "unit": "", "source": "default", "note": "Reference designator at the source end of the TDR. Default: the first IC pad on the net."},
            {"name": "quality", "label": "Mesh quality", "default": "screen", "unit": "", "source": "default", "note": "'screen' or 'confirm' (four times the work, about a third of the discretisation error)."},
            {"name": "time_budget_s", "label": "Solver time budget", "default": 18.0, "unit": "s", "source": "default", "note": "Once spent, remaining sections reuse the nearest solved neighbour and the result says so."},
        ]

    # ---- net selection
    def pick_nets(self, board: Board, ctx: AnalyzerContext, inputs: dict) -> list[str]:
        cap = int(inputs.get("max_nets") or 10)
        names = {n for n in board.net_names()}
        routed = {t.net for t in board.tracks if t.net}
        asked = inputs.get("nets")
        if isinstance(asked, str) and asked != "auto":
            asked = [asked]
        if asked and asked != "auto":
            picks = [n for n in asked if n in routed]
        else:
            fast = fast_nets(board, ctx)
            length: dict[str, float] = {}
            for t in board.tracks:
                if t.net:
                    length[t.net] = length.get(t.net, 0.0) + t.length

            def rank(n: str) -> tuple[int, float]:
                if is_rf_net(n, ctx):
                    return (0, -length.get(n, 0))
                if _USB_RE.search(n.upper()):
                    return (1, -length.get(n, 0))
                if is_clock_net(n):
                    return (2, -length.get(n, 0))
                return (3, -length.get(n, 0))

            picks = sorted(fast, key=rank)
            if not picks:  # no fast nets by name: the longest signal nets
                sig = [n for n in routed if not is_ground(n) and not is_power(n, ctx) and length.get(n, 0) >= 5.0]
                picks = sorted(sig, key=lambda n: -length[n])
        out: list[str] = []
        for n in picks:
            if n in out:
                continue
            out.append(n)
            partner = pair_partner(n, names)
            if partner and partner in routed and partner not in out and is_diff_net(n):
                out.append(partner)
            if len(out) >= cap:
                break
        return out

    # ---- run
    def run(self, board: Board, ctx: Any, inputs: dict, whatif: list[dict], out_dir: Path) -> SimResult:
        try:
            return self._run(board, ctx, inputs, whatif, out_dir)
        except Exception as exc:  # noqa: BLE001 - a failed solve must not take the review down with it
            logger.exception("signal simulation failed")
            return SimResult(self.kind, "not_possible", f"the signal solver failed on this board: {type(exc).__name__}: {exc}")

    def _run(self, board: Board, ctx: Any, inputs: dict, whatif: list[dict], out_dir: Path) -> SimResult:
        t_start = time.perf_counter()
        inputs = dict(inputs or {})
        refused = self.check_whatif(whatif)
        if refused:
            return SimResult(self.kind, "not_possible", refused)
        ok, why = self.available(board, ctx)
        if not ok:
            return SimResult(self.kind, "not_possible", why)
        actx = _analyzer_ctx(ctx)
        quality = inputs.get("quality") or ("confirm" if (inputs.get("mesh") == "fine" or getattr(ctx, "quality", "screen") == "confirm") else "screen")
        if quality not in QUALITY:
            return SimResult(self.kind, "not_possible", f"quality must be one of {sorted(QUALITY)}")
        budget = float(inputs.get("time_budget_s") or 18.0)
        deadline = t_start + budget
        try:
            ops = _parse_whatif(board, whatif)
        except ValueError as exc:
            return SimResult(self.kind, "not_possible", str(exc))
        stack = sb.build_stack(board)
        rad = radiating(board, actx)
        notes: list[str] = []
        assumptions: list[Assumption] = []
        nets = self.pick_nets(board, actx, inputs)
        if not nets:
            return SimResult(self.kind, "not_possible", "no routed signal net was selected")
        power_nets = set(actx.power_nets)
        widths = {o["net"]: o["width_mm"] for o in ops if o["op"] == "widen_track"}
        # what-ifs that need the base geometry first: remove_gap patches and stitching vias
        base_cu = sb.Copper(board, stack, widths, power_nets=power_nets)
        patches: list[tuple[str, Any]] = []
        extra_vias: list[tuple[float, float, float, float]] = []
        whatif_report: list[dict[str, Any]] = []
        for o in ops:
            if o["op"] == "widen_track":
                whatif_report.append({"op": o["op"], "net": o["net"], "width_mm": o["width_mm"], "tracks_changed": sum(1 for t in board.tracks if t.net == o["net"])})
            elif o["op"] == "remove_gap":
                rep = self._whatif_remove_gap(board, actx, stack, base_cu, rad, o, patches)
                whatif_report.append(rep)
            elif o["op"] == "add_stitching":
                rep = self._whatif_stitching(board, actx, stack, base_cu, rad, o, extra_vias)
                whatif_report.append(rep)
        cu = sb.Copper(board, stack, widths, patches, extra_vias, power_nets) if (patches or extra_vias) else base_cu
        solver = SectionSolver(quality, deadline)
        names = set(board.net_names())
        results: dict[str, NetResult] = {}
        skipped: dict[str, str] = {}
        for net in nets:
            iface = interface_for(board, actx, net, inputs)
            src_ref = inputs.get("driver_ref")
            if isinstance(src_ref, dict):
                src_ref = src_ref.get(net)
            path = sb.net_path(board, net, stack, widths, src_ref, rad.track_ids)
            if path is None or path.trace_length_mm < 0.3:
                skipped[net] = "no routed path of 0.3 mm or more outside the antenna element"
                continue
            res = self._analyze_net(board, actx, stack, cu, rad, solver, net, iface, path, pair_partner(net, names) if is_diff_net(net) else None, inputs, notes)
            results[net] = res
        if not results:
            return SimResult(self.kind, "not_possible", "none of the selected nets has a usable route", notes=[f"{n}: {r}" for n, r in skipped.items()])

        t_nets = time.perf_counter()
        # return paths, crosstalk tables and TDR
        for res in results.values():
            self._return_paths(board, cu, stack, res, solver, deadline, notes)
            self._finish_net(res, inputs, board)
        t_ret = time.perf_counter()
        pairs = self._pairs(results)
        trust = self._trust(board, stack, cu, solver, results, quality)
        t_trust = time.perf_counter()

        # outputs
        out_dir.mkdir(parents=True, exist_ok=True)
        overlays = self._overlays(board, results, out_dir)
        curves = {n: _curves(r) for n, r in results.items()}
        (out_dir / "curves.json").write_text(_dumps({"version": SIGNAL_VERSION, "nets": curves, "pairs": pairs}), encoding="utf-8")
        metrics = self._metrics(results, pairs, solver)
        detail = self._detail(board, stack, results, pairs, skipped, solver, whatif_report, quality, inputs, actx)
        detail["timing_s"] = {"setup_and_sections": round(t_nets - t_start, 3), "return_paths_and_tdr": round(t_ret - t_nets, 3), "trust_checks": round(t_trust - t_ret, 3), "outputs": round(time.perf_counter() - t_trust, 3), "field_solves": round(solver.solve_s, 3)}
        assumptions += self._assumptions(board, stack, results, inputs, actx)
        notes += _standing_notes(solver, stack, cu)
        res_obj = SimResult(
            kind=self.kind,
            status="ok",
            reason=None,
            metrics=metrics,
            trust=trust,
            overlays=overlays,
            assumptions=assumptions,
            notes=notes,
            solve_s=round(time.perf_counter() - t_start, 3),
            detail=detail,
        )
        return res_obj

    # ------------------------------------------------------------------ one net
    def _analyze_net(self, board, actx, stack, cu, rad, solver, net, iface, path, partner, inputs, notes) -> NetResult:
        length = path.length_mm
        step = max(0.25, length / 300.0)
        samples = sb.sample_path(path, step)
        n = len(samples)
        xs = np.array([s.x for s in samples])
        ys = np.array([s.y for s in samples])
        raws: list[Any] = [None] * n
        for i, s in enumerate(samples):
            if s.kind == "trace":
                raws[i] = cu.intervals((s.x, s.y), (-s.ty, s.tx), HALF_WINDOW_MM)
        exempt = np.zeros(n, dtype=bool)
        if rad:
            for layer in {s.layer for s in samples}:
                m = np.array([s.layer == layer for s in samples])
                if m.any():
                    exempt[m] = rad.contains_xy(xs[m], ys[m], layer)
        q = 0.005
        builds: list[Any] = [None] * n
        for i, smp in enumerate(samples):
            if smp.kind == "trace":
                builds[i] = sb.build_section(cu, (smp.x, smp.y), (smp.tx, smp.ty), smp.layer, smp.width, net, quant=q, raw=raws[i])
        scale = 1.0
        while True:
            rep_of, reps = _cluster(builds, scale)
            if len(reps) <= MAX_UNIQUE_SECTIONS or scale >= 2.4:
                break
            scale *= 1.25
        if len(reps) > MAX_UNIQUE_SECTIONS:
            rep_of, reps = _merge_smallest(builds, rep_of, reps, MAX_UNIQUE_SECTIONS)
        keys = reps
        if scale > 1.0:
            notes.append(f"{net}: {sum(1 for b in builds if b is not None)} cross-sections share {len(reps)} solves (similar surroundings grouped, tolerance x{scale:.2g}).")
        z0 = np.full(n, np.nan)
        ee = np.full(n, np.nan)
        tpd = np.full(n, np.nan)
        cover = np.ones(n)
        valid = np.ones(n, dtype=bool)
        coupled = np.zeros(n, dtype=bool)
        z_eff = np.full(n, np.nan)
        tpd_eff = np.full(n, np.nan)
        zdiff = np.full(n, np.nan)
        zcm = np.full(n, np.nan)
        sec_res: list[SecResult | None] = [None] * n
        solver.last = {}
        solved_rep: dict[int, SecResult] = {}
        for i, (s, b) in enumerate(zip(samples, builds)):
            if b is None:
                continue
            ri = rep_of[i]
            if ri not in solved_rep:
                solved_rep[ri] = solver.get(builds[ri])
            r = solved_rep[ri]
            sec_res[i] = r
            z0[i], ee[i], tpd[i] = r.z0, r.ee, r.tpd * 1e-3
            cover[i] = b.ref_cover
            valid[i] = b.has_return
            z_eff[i], tpd_eff[i] = r.z0, r.tpd * 1e-3
            if partner and partner in b.cond_nets and not r.reused and r.c[b.cond_nets.index(partner), b.cond_nets.index(partner)] > 0:
                j = b.cond_nets.index(partner)
                m = _pair_modes(r.c, r.l, j)
                z_eff[i] = m["z_odd"]
                tpd_eff[i] = m["tpd_odd"] * 1e-3
                zdiff[i] = 2 * m["z_odd"]
                zcm[i] = m["z_even"] / 2
                coupled[i] = True
            elif partner:
                zdiff[i] = 2 * r.z0
                zcm[i] = r.z0 / 2
        _fill_vias(samples, z0, ee, tpd, z_eff, tpd_eff, zdiff, zcm, stack)
        supported = np.array([(s.kind == "via") or cover[i] >= SUPPORT_COVER for i, s in enumerate(samples)])
        target, basis = sb_target(actx, net)
        res = NetResult(net, iface, path, samples, target, basis, partner, z0, ee, tpd, z_eff, tpd_eff, coupled, cover, supported, exempt, valid)
        res.zdiff = zdiff if partner else None
        res.zcm = zcm if partner else None
        res.sections = sum(1 for b in builds if b is not None)
        res.unique = len(keys)
        res.quant = q
        res.quant = q
        res.builds = builds
        # crosstalk rows
        res.xtalk = self._crosstalk(board, actx, res, sec_res, inputs)
        # geometry summary for validity
        first = next((b for b in builds if b is not None), None)
        if first is not None:
            res.layer_geom = {"kind": first.kind, "height_mm": first.height_mm, "er": first.er_sub, "layer": samples[0].layer}
        return res

    def _crosstalk(self, board, actx, res: NetResult, sec_res, inputs) -> list[dict[str, Any]]:
        samples = res.samples
        swing = float(inputs.get("aggressor_swing_v") or 3.3)
        per: dict[str, list[dict[str, Any]]] = {}
        for i, (s, b) in enumerate(zip(samples, res.builds)):
            if b is None or sec_res[i] is None or len(b.cond_nets) < 2 or sec_res[i].reused:
                continue
            r = sec_res[i]
            for k in range(1, len(b.cond_nets)):
                nm = b.cond_nets[k]
                if nm == res.partner or r.c[k, k] <= 0:
                    continue
                per.setdefault(nm, []).append({"i": i, "ds": s.ds, "cm": -r.c[0, k], "lm": r.l[0, k], "cs": r.c[0, 0], "ls": r.l[0, 0], "tpd": res.tpd[i], "gap": b.gaps.get(nm, float("nan"))})
        rows = []
        for nm, recs in per.items():
            agg_iface = interface_for(board, actx, nm, inputs)
            rise = agg_iface.rise_ns * 1e-9
            stretches: list[list[dict[str, Any]]] = []
            for rec in recs:
                if stretches and rec["i"] - stretches[-1][-1]["i"] <= 4:
                    stretches[-1].append(rec)
                else:
                    stretches.append([rec])
            best = None
            tot_next = 0.0
            tot_len = 0.0
            for st in stretches:
                ln = sum(x["ds"] for x in st)
                if ln < 0.5:
                    continue
                w = np.array([x["ds"] for x in st])
                cm = float(np.average([x["cm"] for x in st], weights=w))
                lm = float(np.average([x["lm"] for x in st], weights=w))
                cs = float(np.average([x["cs"] for x in st], weights=w))
                ls = float(np.average([x["ls"] for x in st], weights=w))
                delay = float(sum(x["ds"] * x["tpd"] for x in st))
                xt = fs.crosstalk(cm, cs, lm, ls, delay, rise)
                tot_next += abs(xt["near_end"])
                tot_len += ln
                rec = {"length_mm": ln, "min_gap_mm": float(np.nanmin([x["gap"] for x in st])), "kb": xt["kb"], "near_end": xt["near_end"], "far_end": xt["far_end"], "cm_over_c": cm / cs, "lm_over_l": lm / ls, "delay_ps": delay * 1e12, "saturated": bool(xt["saturated"]), "s_mm": samples[st[0]["i"]].s}
                if best is None or abs(rec["near_end"]) > abs(best["near_end"]):
                    best = rec
            if best is None:
                continue
            rows.append(
                {
                    "aggressor": nm,
                    "aggressor_rise_ns": agg_iface.rise_ns,
                    "rise_source": agg_iface.source,
                    "coupled_mm": tot_len,
                    "worst_stretch": best,
                    "near_end_pct": 100 * abs(best["near_end"]),
                    "far_end_pct": 100 * best["far_end"],
                    "near_end_v": swing * abs(best["near_end"]),
                    "far_end_v": swing * best["far_end"],
                    "near_end_sum_pct": 100 * tot_next,
                }
            )
        rows.sort(key=lambda r: -r["near_end_pct"])
        return rows

    # ------------------------------------------------------------------ return path
    def _return_paths(self, board, cu: sb.Copper, stack, res: NetResult, solver, deadline, notes) -> None:
        samples = res.samples
        ok = res.supported | res.exempt
        n = len(samples)
        events: list[Event] = []
        i = 0
        while i < n:
            if ok[i]:
                i += 1
                continue
            j = i
            while j + 1 < n and not ok[j + 1]:
                j += 1
            events.append(Event(i, j, samples[i].s, samples[j].s, sum(samples[k].ds for k in range(i, j + 1)), samples[i].layer, samples[(i + j) // 2].x, samples[(i + j) // 2].y))
            i = j + 1
        merged: list[Event] = []
        for ev in events:
            if merged and ev.s0 - merged[-1].s1 <= GAP_MERGE_MM:
                m = merged[-1]
                m.i1, m.s1 = ev.i1, ev.s1
                m.length_mm += ev.length_mm
                mid = (m.i0 + m.i1) // 2
                m.x, m.y = samples[mid].x, samples[mid].y
            else:
                merged.append(ev)
        res.events = [e for e in merged if e.length_mm >= GAP_MIN_MM]
        # reference inductance per metre of the supported line, for the loop estimate
        sup = [k for k in range(n) if samples[k].kind == "trace" and res.supported[k] and not res.exempt[k]]
        l_ref = None
        if sup and res.builds:
            ls = [res.z0[k] * math.sqrt(res.ee[k]) / fs.C_LIGHT for k in sup if np.isfinite(res.z0[k])]
            l_ref = statistics.median(ls) if ls else None
        by_len = sorted(res.events, key=lambda e: -e.length_mm)[:MAX_RETURN_EVENTS]
        for ev in by_len:
            if time.perf_counter() > deadline + 4.0:
                notes.append(f"{res.net}: return paths for some gaps were not computed (time budget).")
                break
            ib = ev.i0 - 1 if ev.i0 > 0 else None
            ia = ev.i1 + 1 if ev.i1 + 1 < n else None
            a_i = ib if ib is not None else ev.i0
            b_i = ia if ia is not None else ev.i1
            pts = [(samples[k].x, samples[k].y) for k in range(a_i, b_i + 1)]
            if len(pts) < 2:
                pts = [pts[0], pts[0]]
            b0 = res.builds[ev.i0] if res.builds[ev.i0] is not None else next((x for x in res.builds if x is not None), None)
            ref_layer = (b0.ref_layers[0] if b0 and b0.ref_layers else None)
            ret = sb.shortest_return(cu, pts[0], pts[-1], pts, ref_layer)
            ev.ret = ret
            ev.analysed = True
            if ret.ok:
                w = max(samples[k].width for k in range(ev.i0, ev.i1 + 1))
                h = b0.height_mm if b0 and b0.height_mm > 0 else 0.2
                t = stack.cu_t.get(ev.layer, 0.035)
                r_s = 0.2235 * (w + t)
                d = math.sqrt(ret.max_offset_mm**2 + h**2)
                ell = 0.5 * (ret.direct_mm + ret.return_mm) * 1e-3
                if d > 2 * r_s:
                    l_div = fs.MU0 / math.pi * ell * math.log(d * 1e-3 / (r_s * 1e-3))
                else:
                    l_div = 0.0
                l_norm = (l_ref or 0.7e-6) * ret.direct_mm * 1e-3
                ev.extra_nh = max(0.0, (l_div - l_norm) * 1e9)

    # ------------------------------------------------------------------ TDR and totals
    def _finish_net(self, res: NetResult, inputs: dict, board: Board) -> None:
        s = res.samples
        ds = np.array([x.ds for x in s])
        tpd_eff = np.where(np.isfinite(res.tpd_eff), res.tpd_eff, np.nanmedian(res.tpd_eff))
        z = np.where(np.isfinite(res.z_eff), res.z_eff, np.nanmedian(res.z_eff))
        delay = ds * tpd_eff  # s per sample
        total = float(delay.sum())
        rs = float(inputs.get("source_ohm") or 50.0)
        rl = inputs.get("load_ohm")
        rl = None if rl in (None, "open", 0) else float(rl)
        rise = res.iface.rise_ns * 1e-9
        f_valid = fs.valid_to_hz(res.layer_geom.get("height_mm") or 0.2, res.layer_geom.get("er") or 4.5, float(np.nanmedian(z)))
        res.layer_geom["valid_to_hz"] = f_valid
        res.tdr = {
            "interface": _tdr(z, delay, total, rise, rs, rl, "interface rise time"),
            "fast": _tdr(z, delay, total, max(0.35 / f_valid, 20e-12), rs, rl, "fastest edge the model supports"),
            "source_ohm": rs,
            "load_ohm": rl,
            "basis": "differential drive, per leg (odd mode)" if res.partner else "single-ended, neighbours quiet",
            "delay_ps": total * 1e12,
        }
        res.layer_geom["delay_ps"] = total * 1e12
        vias = [p for p in res.path.pieces if p.kind == "via"]
        if vias:
            # Johnson and Graham: L = 5.08 h [ln(4h/d) + 1] nH with h and d in inches, i.e. 0.2 h [ln(4h/d) + 1] nH with h in mm
            drill = next((v.drill for v in board.vias if v.net == res.net and v.drill), 0.3)
            hmm = float(np.mean([p.z_len for p in vias]))
            res.layer_geom["via_nh"] = 0.2 * hmm * (math.log(4 * hmm / drill) + 1.0)

    # ------------------------------------------------------------------ pairs
    def _pairs(self, results: dict[str, NetResult]) -> list[dict[str, Any]]:
        out = []
        seen = set()
        for a, ra in results.items():
            b = ra.partner
            if not b or b not in results or (b, a) in seen:
                continue
            seen.add((a, b))
            rb = results[b]
            zd = np.concatenate([ra.zdiff[ra.coupled], rb.zdiff[rb.coupled]]) if ra.zdiff is not None and rb.zdiff is not None else np.array([])
            coupled_a = float(sum(s.ds for s, c in zip(ra.samples, ra.coupled) if c))
            coupled_b = float(sum(s.ds for s, c in zip(rb.samples, rb.coupled) if c))
            da, db = ra.tdr["delay_ps"], rb.tdr["delay_ps"]
            la, lb = ra.path.length_mm, rb.path.length_mm
            diff_target = None
            row = {
                "pair": [a, b],
                "zdiff_median_ohm": float(np.median(zd)) if len(zd) else None,
                "zdiff_min_ohm": float(np.min(zd)) if len(zd) else None,
                "zdiff_max_ohm": float(np.max(zd)) if len(zd) else None,
                "coupled_mm": [coupled_a, coupled_b],
                "length_mm": [la, lb],
                "length_mismatch_mm": abs(la - lb),
                "delay_ps": [da, db],
                "skew_ps": abs(da - db),
                "skew_pct_of_rise": 100 * abs(da - db) / (ra.iface.rise_ns * 1e3) if ra.iface.rise_ns > 0 else None,
                "zcm_median_ohm": float(np.nanmedian(np.concatenate([ra.zcm[ra.coupled], rb.zcm[rb.coupled]]))) if len(zd) else None,
                "basis": "odd-mode impedance of the coupled legs, 2 x Z_odd; sections where the partner is not within coupling range count as 2 x Z0",
            }
            diff_target = 2 * ra.target if ra.target_basis.startswith("differential") else None
            row["zdiff_target_ohm"] = diff_target
            if diff_target and row["zdiff_median_ohm"]:
                row["zdiff_deviation_pct"] = 100 * (row["zdiff_median_ohm"] - diff_target) / diff_target
            out.append(row)
        return out

    # ------------------------------------------------------------------ trust
    def _trust(self, board, stack, cu, solver: SectionSolver, results: dict[str, NetResult], quality: str) -> dict:
        checks: list[dict[str, Any]] = []
        # 1. grid convergence on representative sections
        reps: list[tuple[str, sb.SectionBuild]] = []
        for net, r in results.items():
            sup = [i for i, b in enumerate(r.builds) if b is not None and r.supported[i] and r.valid[i]]
            if sup:
                reps.append((net, r.builds[sup[len(sup) // 2]]))
            if len(reps) >= 3:
                break
        if not reps:
            reps = [(n, next(b for b in r.builds if b is not None)) for n, r in list(results.items())[:2]]
        fine_params = {"fine": solver.params["fine"] * 0.5, "ratio": max(1.15, solver.params["ratio"] * 0.9), "dmax_mm": solver.params["dmax_mm"] * 0.7}
        worst = 0.0
        sym_worst = 0.0
        recip_worst = 0.0
        t0 = time.perf_counter()
        for net, b in reps:
            base = fs.solve(b.section, **solver.params)
            fine = fs.solve(b.section, **fine_params)
            worst = max(worst, abs(fine.z0(0) / base.z0(0) - 1.0))
            mir = fs.solve(fs.mirror(b.section), **solver.params)
            sym_worst = max(sym_worst, abs(mir.z0(0) / base.z0(0) - 1.0))
            recip_worst = max(recip_worst, float(np.max(np.abs(base.c - base.c.T)) / np.max(np.abs(base.c))))
            if not (np.all(np.diag(base.c) > 0) and np.all(base.c - np.diag(np.diag(base.c)) <= 1e-18)):
                recip_worst = max(recip_worst, 1.0)
        lim_conv = 0.02 if quality == "screen" else 0.01
        checks.append({"name": "grid_convergence", "status": "ok" if worst <= lim_conv else "failed", "value": round(100 * worst, 3), "limit": round(100 * lim_conv, 3), "unit": "percent", "detail": f"Z0 of {len(reps)} representative cross-sections solved again on a mesh about twice as fine; the largest change is shown."})
        # a symmetric coupled pair on this stackup must have equal self capacitance on both lines
        asym = 0.0
        first = next(((n, r) for n, r in results.items() if r.samples and r.layer_geom.get("kind") == "microstrip"), None)
        if first is not None:
            w0 = next(x.width for x in first[1].samples if x.kind == "trace")
            h0, er0 = first[1].layer_geom["height_mm"], first[1].layer_geom["er"]
            sol = fs.solve(fs.coupled_microstrip_section(w0, w0, h0, stack.cu_t.get(first[1].samples[0].layer, 0.035), er0, box=25.0, side=25.0), **solver.params)
            asym = abs(sol.c[0, 0] - sol.c[1, 1]) / sol.c[0, 0]
        sym_worst = max(sym_worst, asym)
        checks.append({"name": "symmetry", "status": "ok" if sym_worst <= 0.003 else "failed", "value": round(100 * sym_worst, 4), "limit": 0.3, "unit": "percent", "detail": "The sections mirrored left to right must give the same Z0, and a symmetric coupled pair on this stackup must have equal capacitance on its two lines."})
        checks.append({"name": "capacitance_matrix", "status": "ok" if recip_worst <= 1e-6 else "failed", "value": recip_worst, "limit": 1e-6, "unit": "", "detail": "The capacitance matrix is symmetric, its diagonal positive and its mutual terms negative."})
        # 2. Z0 against closed form on this board's own stackup and track widths
        worst_cf = 0.0
        cf_rows = []
        seen: set[tuple[str, float]] = set()
        for net, r in results.items():
            for s in r.samples:
                if s.kind != "trace" or (s.layer, round(s.width, 4)) in seen or len(seen) >= 3:
                    continue
                seen.add((s.layer, round(s.width, 4)))
                refs = stack.refs(s.layer)
                if len(refs) != 1:
                    continue
                h, er = stack.gap(s.layer, refs[0])
                if h <= 0:
                    continue
                t = stack.cu_t.get(s.layer, 0.035)
                w = s.width
                if not (0.1 <= w / h <= 8):
                    continue
                zh, _ = fs.hammerstad_jensen(w, h, t, er)
                sec = fs.microstrip_section(w, h, t, er, box=25.0, side=25.0)
                fe = fs.solve(sec, **solver.params).z0(0)
                err = abs(fe / zh - 1)
                worst_cf = max(worst_cf, err)
                cf_rows.append({"layer": s.layer, "width_mm": w, "height_mm": h, "er": er, "fe_ohm": round(fe, 2), "closed_form_ohm": round(zh, 2), "error_pct": round(100 * (fe / zh - 1), 2)})
        checks.append({"name": "closed_form_microstrip", "status": "ok" if worst_cf <= 0.025 else "failed", "value": round(100 * worst_cf, 3), "limit": 2.5, "unit": "percent", "detail": "Ideal microstrip of this board's stackup and track widths against Hammerstad and Jensen; the closed form is itself accurate to about 1 percent with copper thickness.", "rows": cf_rows})
        # 3. the solver finished without reuse
        if solver.reused:
            checks.append({"name": "time_budget", "status": "ok", "value": solver.reused, "limit": 0, "unit": "sections", "detail": f"{solver.reused} sections reused the nearest solved neighbour after the time budget ran out; results there are approximate."})
        solver.solve_s += time.perf_counter() - t0
        status = "ok" if all(c["status"] == "ok" for c in checks) else "failed"
        return {"status": status, "checks": checks}

    # ------------------------------------------------------------------ overlays
    def _overlays(self, board: Board, results: dict[str, NetResult], out_dir: Path) -> list[Overlay]:
        bb = board.bbox
        x0, y0 = bb[0] - 1.0, bb[1] - 1.0
        w, h = bb[2] - bb[0] + 2.0, bb[3] - bb[1] + 2.0
        d = 0.1
        while (w / d) * (h / d) > 2.5e6:
            d *= 1.5
        nx, ny = int(math.ceil(w / d)), int(math.ceil(h / d))
        layers = sorted({s.layer for r in results.values() for s in r.samples if s.kind == "trace"})
        grids = {la: np.full((ny, nx), np.nan, dtype=np.float32) for la in layers}
        for r in results.values():
            dev = 100.0 * (r.z_eff - r.target) / r.target
            for i, s in enumerate(r.samples):
                if s.kind != "trace" or not np.isfinite(dev[i]):
                    continue
                p0 = (s.x - s.tx * s.ds / 2, s.y - s.ty * s.ds / 2)
                p1 = (s.x + s.tx * s.ds / 2, s.y + s.ty * s.ds / 2)
                _paint(grids[s.layer], x0, y0, d, p0, p1, max(s.width / 2, 0.6 * d), float(dev[i]))
        overlays: list[Overlay] = []
        lim = 0.0
        for g in grids.values():
            if np.isfinite(g).any():
                lim = max(lim, float(np.nanmax(np.abs(g))))
        lim = float(min(100.0, max(25.0, math.ceil(lim / 10) * 10)))
        for la in layers:
            g = grids[la]
            if not np.isfinite(g).any():
                continue
            fname = f"z0_deviation_{la}.bin"
            g.astype("<f4").tofile(out_dir / fname)
            overlays.append(Overlay(f"z0_deviation_{la}", f"Impedance deviation from target on {la} (quasi-static estimate)", "percent", la, x0, y0, d, d, nx, ny, -lim, lim, fname, "diverging"))
        # return detour: tracks over a gap and the return path itself
        g = np.full((ny, nx), np.nan, dtype=np.float32)
        vmax = 5.0
        for r in results.values():
            for ev in r.events:
                if ev.ret is not None and ev.ret.ok:
                    v = ev.ret.detour_mm
                elif ev.analysed:
                    v = -1.0
                else:
                    continue
                vmax = max(vmax, v)
        any_event = False
        for r in results.values():
            for ev in r.events:
                if not ev.analysed:
                    continue
                v = ev.ret.detour_mm if (ev.ret is not None and ev.ret.ok) else vmax
                any_event = True
                for k in range(ev.i0, ev.i1 + 1):
                    s = r.samples[k]
                    if s.kind != "trace":
                        continue
                    p0 = (s.x - s.tx * s.ds / 2, s.y - s.ty * s.ds / 2)
                    p1 = (s.x + s.tx * s.ds / 2, s.y + s.ty * s.ds / 2)
                    _paint(g, x0, y0, d, p0, p1, max(s.width / 2, 0.7 * d), float(v))
                if ev.ret is not None and ev.ret.ok and len(ev.ret.path) > 1:
                    pts = [(q[0], q[1]) for q in ev.ret.path]
                    for a, b in zip(pts, pts[1:]):
                        _paint(g, x0, y0, d, a, b, 0.8 * d, float(v))
        if any_event:
            fname = "return_detour.bin"
            g.astype("<f4").tofile(out_dir / fname)
            overlays.append(Overlay("return_detour", "Return current detour around gaps in the reference (extra path length, shortest path through ground copper)", "mm", "board", x0, y0, d, d, nx, ny, 0.0, float(math.ceil(vmax)), fname, "sequential"))
        return overlays

    # ------------------------------------------------------------------ metrics and detail
    def _metrics(self, results: dict[str, NetResult], pairs, solver: SectionSolver) -> dict[str, dict]:
        m: dict[str, dict] = {}

        def put(name, value, unit, label):
            m[name] = {"value": None if value is None else round(float(value), 4), "unit": unit, "label": label}

        for net, r in results.items():
            ok = r.valid & np.array([s.kind == "trace" for s in r.samples])
            ze = r.z_eff[ok] if ok.any() else r.z_eff[np.isfinite(r.z_eff)]
            dev = 100 * (ze - r.target) / r.target
            unsupported = sum(s.ds for s, sup, ex in zip(r.samples, r.supported, r.exempt) if not (sup or ex))
            put(f"{net}.z0_median", np.median(ze), "ohm", f"{net}: median impedance (quasi-static estimate)")
            put(f"{net}.z0_min", ze.min(), "ohm", f"{net}: lowest impedance along the route")
            put(f"{net}.z0_max", ze.max(), "ohm", f"{net}: highest impedance along the route")
            put(f"{net}.z0_target", r.target, "ohm", f"{net}: impedance target ({r.target_basis})")
            put(f"{net}.z0_worst_deviation", dev[np.argmax(np.abs(dev))], "percent", f"{net}: largest deviation from target")
            m[f"{net}.z0_worst_deviation"].update({"better": "lower", "limit": 10.0, "limit_label": "impedance tolerance used by the layout checks (10 percent, default)"})
            put(f"{net}.length", r.path.length_mm, "mm", f"{net}: routed length of the analysed route")
            put(f"{net}.delay", r.tdr["delay_ps"], "ps", f"{net}: one-way propagation delay (estimate)")
            put(f"{net}.rise_time", r.iface.rise_ns, "ns", f"{net}: rise time used ({r.iface.source}: {r.iface.label})")
            put(f"{net}.reference_gap_length", unsupported, "mm", f"{net}: track length with no ground on the reference layer")
            put(f"{net}.reference_gap_events", len(r.events), "count", f"{net}: separate gap crossings")
            det = [e.ret.detour_mm for e in r.events if e.ret is not None and e.ret.ok]
            put(f"{net}.max_return_detour", max(det) if det else 0.0, "mm", f"{net}: largest extra return path length")
            put(f"{net}.extra_loop_inductance", sum(e.extra_nh for e in r.events), "nH", f"{net}: extra loop inductance from detours (order of magnitude)")
            if r.xtalk:
                top = r.xtalk[0]
                put(f"{net}.crosstalk_near_end", top["near_end_pct"], "percent", f"{net}: worst near-end crosstalk from {top['aggressor']} (fraction of aggressor swing)")
                put(f"{net}.crosstalk_far_end", top["far_end_pct"], "percent", f"{net}: far-end crosstalk from {top['aggressor']} (signed)")
            nv = sum(1 for p in r.path.pieces if p.kind == "via")
            if nv:
                put(f"{net}.via_inductance", r.layer_geom.get("via_nh", 0.0) * nv, "nH", f"{net}: inductance of the {nv} via(s) on the route (listed, not in the TDR; order of magnitude)")
            put(f"{net}.quasi_static_limit", r.layer_geom.get("valid_to_hz", 0) / 1e9, "GHz", f"{net}: frequency up to which the model holds")
        for p in pairs:
            nm = "/".join(p["pair"])
            if p["zdiff_median_ohm"]:
                put(f"{nm}.zdiff_median", p["zdiff_median_ohm"], "ohm", f"{nm}: differential impedance over the coupled length")
            put(f"{nm}.skew", p["skew_ps"], "ps", f"{nm}: propagation delay difference of the two legs")
            put(f"{nm}.length_mismatch", p["length_mismatch_mm"], "mm", f"{nm}: routed length difference")
        put("nets_analysed", len(results), "count", "Nets analysed")
        put("sections_solved", solver.solved, "count", "Distinct cross-sections solved")
        put("cross_section_nodes", solver.max_nodes, "count", "Largest mesh used")
        return m

    def _detail(self, board, stack, results, pairs, skipped, solver, whatif_report, quality, inputs, actx) -> dict[str, Any]:
        nets = {}
        for net, r in results.items():
            ev = []
            for e in r.events:
                row = {"s_mm": [round(e.s0, 3), round(e.s1, 3)], "length_mm": round(e.length_mm, 3), "layer": e.layer, "at": [round(e.x, 3), round(e.y, 3)], "return_path_computed": e.analysed}
                if e.ret is not None:
                    row.update({"ok": e.ret.ok, "reason": e.ret.reason or None})
                    if e.ret.ok:
                        row.update(
                            {
                                "direct_mm": round(e.ret.direct_mm, 3),
                                "return_mm": round(e.ret.return_mm, 3),
                                "detour_mm": round(e.ret.detour_mm, 3),
                                "max_offset_mm": round(e.ret.max_offset_mm, 3),
                                "loop_area_mm2": round(e.ret.loop_area_mm2, 3),
                                "layers_used": e.ret.layers_used,
                                "vias_used": e.ret.vias_used,
                                "extra_loop_nh": round(e.extra_nh, 3),
                            }
                        )
                ev.append(row)
            nets[net] = {
                "interface": {"kind": r.iface.kind, "label": r.iface.label, "rise_ns": r.iface.rise_ns, "source": r.iface.source, "cite": r.iface.cite},
                "target_ohm": r.target,
                "target_basis": r.target_basis,
                "source_pad": r.path.source,
                "sink_pad": r.path.sink,
                "route_length_mm": round(r.path.length_mm, 3),
                "net_copper_length_mm": round(r.path.total_length_mm, 3),
                "branch_length_mm": round(r.path.branch_length_mm, 3),
                "vias_on_route": sum(1 for p in r.path.pieces if p.kind == "via"),
                "sections": r.sections,
                "distinct_sections": r.unique,
                "position_rounding_mm": r.quant,
                "partner": r.partner,
                "layers": sorted({s.layer for s in r.samples}),
                "quasi_static_limit_ghz": round(r.layer_geom.get("valid_to_hz", 0) / 1e9, 3),
                "edge_content_ghz": round(0.35 / r.iface.rise_ns, 3),
                "within_quasi_static_limit": 0.35 / r.iface.rise_ns * 1e9 <= r.layer_geom.get("valid_to_hz", 0),
                "gap_events": ev,
                "crosstalk": r.xtalk[:6],
                "tdr": {k: {kk: vv for kk, vv in v.items() if kk in ("rise_ns", "label", "peak_z_ohm", "min_z_ohm")} for k, v in r.tdr.items() if isinstance(v, dict)},
                "reference_cover_min": round(float(np.min(r.cover)), 3),
            }
        return {
            "version": SIGNAL_VERSION,
            "curves_file": "curves.json",
            "series": _series(results),
            "quality": quality,
            "nets": nets,
            "pairs": pairs,
            "skipped": skipped,
            "whatif": whatif_report,
            "solver": {"sections_solved": solver.solved, "cache_hits": solver.hits, "reused_after_budget": solver.reused, "largest_mesh_nodes": solver.max_nodes, "solver_seconds": round(solver.solve_s, 3), "mesh": solver.params},
            "cross_check": _cross_check(board, stack, results, solver),
            "limits": LIMITS,
        }

    def _assumptions(self, board, stack, results, inputs, actx) -> list[Assumption]:
        out: list[Assumption] = []
        src = "board" if not stack.assumed else "default"
        cite = "KiCad stackup in the board file" if not stack.assumed else "No stackup in the file: 1.6 mm FR4, epsilon_r 4.5, 35 micron outer copper"
        out.append(Assumption("stackup", f"{len(stack.copper)} copper layers, {stack.top:.3f} mm", "", src, cite))
        out.append(Assumption("dielectric_loss_and_skin_effect", "ignored", "", "default", "lossless quasi-static model; valid to the limit stated per net"))
        out.append(Assumption("other_copper", "AC ground", "", "default", "all copper on other nets, planes and power nets is held at ground in the impedance solve; nearby signal tracks are separate conductors in the coupling solve"))
        out.append(Assumption("solder_mask", "ignored", "", "default", "a 20 micron mask lowers Z0 by about 1 to 3 percent on outer layers"))
        out.append(Assumption("source_resistance", float(inputs.get("source_ohm") or 50.0), "ohm", "user" if inputs.get("source_ohm") else "default", "TDR instrument convention; set the driver output resistance from its datasheet"))
        out.append(Assumption("load", "open" if inputs.get("load_ohm") in (None, "open", 0) else float(inputs["load_ohm"]), "ohm" if inputs.get("load_ohm") not in (None, "open", 0) else "", "user" if inputs.get("load_ohm") not in (None, "open", 0) else "default", "receiver input treated as an unterminated open end with no capacitance"))
        out.append(Assumption("aggressor_swing", float(inputs.get("aggressor_swing_v") or 3.3), "V", "user" if inputs.get("aggressor_swing_v") else "default", "logic swing used to turn crosstalk fractions into volts"))
        out.append(Assumption("terminations", "matched", "", "default", "crosstalk formulas assume lines terminated in their own impedance and weak coupling"))
        for net, r in results.items():
            out.append(Assumption(f"rise_time:{net}", round(r.iface.rise_ns, 4), "ns", r.iface.source, r.iface.cite))
            out.append(Assumption(f"z0_target:{net}", round(r.target, 2), "ohm", "user" if net in actx.net_targets else "default", r.target_basis))
            out.append(Assumption(f"route:{net}", f"{r.path.source} to {r.path.sink}", "", "default" if not inputs.get("driver_ref") else "user", "source pad is the first IC pad on the net (or the driver_ref input); the sink is the farthest pad over the routed copper"))
        return out

    # ------------------------------------------------------------------ what-ifs
    def _support_scan(self, board, actx, stack, cu, rad, path) -> tuple[list[sb.Sample], np.ndarray]:
        samples = sb.sample_path(path, max(0.25, path.length_mm / 300.0))
        cover = np.ones(len(samples))
        for i, s in enumerate(samples):
            if s.kind != "trace":
                continue
            b = sb.build_section(cu, (s.x, s.y), (s.tx, s.ty), s.layer, s.width, path.net)
            cover[i] = b.ref_cover
            if rad and rad.contains_xy(np.array([s.x]), np.array([s.y]), s.layer)[0]:
                cover[i] = 1.0
        return samples, cover

    def _whatif_remove_gap(self, board, actx, stack, cu, rad, op, patches) -> dict[str, Any]:
        net = op["net"]
        path = sb.net_path(board, net, stack, None, None, rad.track_ids)
        rep: dict[str, Any] = {"op": "remove_gap", "net": net, "patches": 0}
        if path is None:
            rep["note"] = "net has no route"
            return rep
        samples, cover = self._support_scan(board, actx, stack, cu, rad, path)
        half = float(op.get("patch_width_mm") or 0.0) / 2.0
        at = op.get("at")
        added = 0
        for s, c in zip(samples, cover):
            if s.kind != "trace" or c >= SUPPORT_COVER:
                continue
            if at is not None and math.hypot(s.x - at[0], s.y - at[1]) > float(op.get("radius_mm") or 5.0):
                continue
            refs = stack.refs(s.layer)
            if not refs:
                continue
            h, _ = stack.gap(s.layer, refs[0])
            hw = half or max(1.0, 1.5 * h + s.width / 2)
            circ = Point(s.x, s.y).buffer(hw, quad_segs=3)
            patches.append((refs[0], circ))
            added += 1
        rep["patches"] = added
        rep["description"] = "ground copper added on the reference layer under the unsupported part of the route, in simulation only"
        return rep

    def _whatif_stitching(self, board, actx, stack, cu, rad, op, extra_vias) -> dict[str, Any]:
        dia = float(op.get("via_diameter_mm") or 0.6)
        drill = float(op.get("drill_mm") or 0.3)
        rep: dict[str, Any] = {"op": "add_stitching", "vias": 0}
        pts: list[tuple[float, float]] = []
        if op.get("at"):
            pts = [(float(p[0]), float(p[1])) for p in op["at"]]
        else:
            net = op["net"]
            path = sb.net_path(board, net, stack, None, None, rad.track_ids)
            if path is None:
                rep["note"] = "net has no route"
                return rep
            spacing = float(op.get("spacing_mm") or 2.0)
            offs = op.get("offset_mm")
            offsets = [float(offs)] if isinstance(offs, (int, float)) else [float(x) for x in (offs or (1.0, 1.6, 2.5))]
            samples = sb.sample_path(path, 0.25)
            last = -1e9
            for s in samples:
                if s.kind != "trace" or s.s - last < spacing:
                    continue
                last = s.s
                for sign in (1, -1):
                    # the nearest offset at which ground copper exists on every layer, so the via really joins them
                    for off in offsets:
                        x, y = s.x - sign * s.ty * (off + s.width / 2), s.y + sign * s.tx * (off + s.width / 2)
                        if all(cu.ground_covers(la, np.array([x]), np.array([y]))[0] for la in stack.copper):
                            pts.append((x, y))
                            break
        limit = int(op.get("max_vias") or 60)
        placed: list[tuple[float, float]] = []
        for x, y in pts:
            if len(placed) >= limit:
                break
            if any(math.hypot(x - px, y - py) < 0.8 for px, py in placed):
                continue
            placed.append((x, y))
            extra_vias.append((x, y, dia, drill))
        rep["vias"] = len(placed)
        if not placed:
            rep["note"] = "no position was found where ground copper exists on every layer beside the route, so nothing was added"
        rep["positions"] = [[round(x, 3), round(y, 3)] for x, y in placed[:40]]
        rep["description"] = "ground vias added in simulation only; they join the ground copper of every layer where it exists"
        return rep


# ------------------------------------------------------------------------------------------ helpers

LIMITS = [
    "Quasi-static: the field is solved as if static, so there is no dispersion, no skin effect and no dielectric loss. The limit per net is stated in quasi_static_limit_ghz; an edge with more content than that is flagged.",
    "Cross-sections are 2D. Where the reference is missing for a stretch shorter than a few substrate heights, or at a bend, a via or a pad, the true 3D field differs; the return detour estimate covers that case only roughly.",
    "Other copper is an AC ground in the impedance solve. A floating island or a pour that is not decoupled behaves differently.",
    "Crosstalk uses weak-coupling formulas with matched terminations; strong coupling or reflections change the numbers.",
    "Return detour is a shortest path through ground copper, not a current simulation. Extra loop inductance is an order-of-magnitude estimate (a factor of about two).",
    "Vias, pads and stubs are not modelled as discontinuities in the TDR; branches off the main route are counted but not included.",
    "The TDR is lossless: no slowing of the edge, no resistive drop.",
    "Solder mask, copper roughness and etch trapezoid are not modelled.",
]


def _standing_notes(solver: SectionSolver, stack: sb.Stack, cu: sb.Copper) -> list[str]:
    notes = [
        "All values are estimates from a quasi-static 2D model on the board's own copper; they are not measurements.",
        f"Mesh: about {solver.max_nodes} nodes per cross-section; {solver.solved} distinct sections solved ({solver.hits} reused from the cache).",
    ]
    if stack.assumed:
        notes.append("The board file has no stackup, so 1.6 mm FR4 (epsilon_r 4.5) was assumed; impedances scale with that.")
    if cu.approx_zones:
        notes.append("A ground zone on " + ", ".join(sorted(cu.approx_zones)) + " has no fill data, so its outline was used as copper; gaps may be understated.")
    return notes


_CTX_KEYS = ("part_roles", "fast_nets", "target_ohms", "rf_nets", "power_nets", "important_nets", "net_targets", "diff_target_ohms", "thresholds")


def _analyzer_ctx(ctx: Any) -> AnalyzerContext:
    """The analyzer context, from what the caller has: an analyzer context itself, or a request (``goal``) that may name nets and targets."""
    if isinstance(ctx, AnalyzerContext):
        return ctx
    out = AnalyzerContext()
    sources = []
    goal = getattr(ctx, "goal", None)
    if isinstance(goal, dict):
        sources.append(goal)
    if isinstance(ctx, dict):
        sources.append(ctx)
    for src in sources:
        for k in _CTX_KEYS:
            v = src.get(k)
            if v is not None:
                try:
                    setattr(out, k, type(getattr(out, k))(v) if not isinstance(v, type(getattr(out, k))) else v)
                except (TypeError, ValueError):
                    continue
    return out


def sb_target(actx: AnalyzerContext, net: str) -> tuple[float, str]:
    if net in actx.net_targets:
        return float(actx.net_targets[net]), "set in the brief"
    if is_diff_net(net) and not is_rf_net(net, actx):
        return float(actx.diff_target_ohms) / 2.0, f"differential target {actx.diff_target_ohms:g} ohm halved per leg"
    return float(actx.target_ohms), "default single-ended target"


def _parse_whatif(board: Board, whatif: list[dict]) -> list[dict]:
    nets = {t.net for t in board.tracks if t.net}
    out = []
    for op in whatif or []:
        name = op.get("op")
        if name == "widen_track":
            net = op.get("net")
            w = op.get("width_mm")
            if net not in nets:
                raise ValueError(f"widen_track: net {net!r} has no tracks")
            if not isinstance(w, (int, float)) or not (0.05 <= float(w) <= 6.0):
                raise ValueError("widen_track: width_mm must be between 0.05 and 6 mm")
            out.append({"op": name, "net": net, "width_mm": float(w)})
        elif name == "remove_gap":
            net = op.get("net")
            if net not in nets:
                raise ValueError(f"remove_gap: net {net!r} has no tracks")
            out.append({**op, "op": name})
        elif name == "add_stitching":
            if not op.get("net") and not op.get("at"):
                raise ValueError("add_stitching: give a net (vias along its route) or at: [[x, y], ...]")
            if op.get("net") and op["net"] not in nets:
                raise ValueError(f"add_stitching: net {op['net']!r} has no tracks")
            out.append({**op, "op": name})
        else:
            raise ValueError(f"what-if {name!r} is not accepted by the signal simulator (accepts: {', '.join(WHATIF_OPS)})")
    return out


def _fill_vias(samples, z0, ee, tpd, z_eff, tpd_eff, zdiff, zcm, stack) -> None:
    """Via pieces take the impedance of the nearest trace sample; their delay uses the mean stackup permittivity."""
    n = len(samples)
    er = np.mean([e for (_, _, e) in stack.slabs if e > 1.0] or [4.5])
    for i, s in enumerate(samples):
        if s.kind != "via":
            continue
        j = next((k for k in range(i - 1, -1, -1) if samples[k].kind == "trace"), None)
        if j is None:
            j = next((k for k in range(i + 1, n) if samples[k].kind == "trace"), None)
        if j is None:
            continue
        z0[i], ee[i], z_eff[i] = z0[j], er, z_eff[j]
        tpd[i] = tpd_eff[i] = math.sqrt(er) / fs.C_LIGHT * 1e-3
        zdiff[i], zcm[i] = zdiff[j], zcm[j]


def _tdr(z: np.ndarray, delay: np.ndarray, total: float, rise: float, rs: float, rl: float | None, label: str) -> dict[str, Any]:
    n_pref = 60
    t_end = 2 * total * 3 + 3 * rise
    t_show = 2 * total + 2.0 * rise
    tau = max(min(rise / 10.0, total / n_pref), t_end / 6000.0)
    n = max(2, int(round(total / tau)))
    zr, tau = fs.resample_by_delay(z, delay, n)
    r = fs.tdr_response(zr, tau, rise, rs, rl, 1.0, t_end)
    keep = max(1, len(r.t_s) // 500)
    zap = np.where(r.t_s <= t_show, r.z_apparent, np.nan)  # the apparent impedance means something until the first round trip has come back
    return {
        "label": label,
        "rise_ns": rise * 1e9,
        "t_ns": (r.t_s[::keep] * 1e9).tolist(),
        "v_source": r.v_source[::keep].tolist(),
        "v_load": r.v_load[::keep].tolist(),
        "z_apparent_ohm": [None if not np.isfinite(v) else round(float(min(max(v, 0.0), 1e4)), 3) for v in zap[::keep]],
        "z_apparent_note": "Rs * v / (Vs - v): the impedance seen looking into the line, shown until the first round trip has returned",
        "round_trip_ps": 2 * total * 1e12,
        "peak_z_ohm": float(np.nanmax(z)),
        "min_z_ohm": float(np.nanmin(z)),
        "sections": n,
        "step_ps": tau * 1e12,
    }


def _curves(r: NetResult) -> dict[str, Any]:
    s = [x.s for x in r.samples]
    keep = max(1, len(s) // 400)

    def a(arr):
        return [None if not np.isfinite(v) else round(float(v), 4) for v in np.asarray(arr)[::keep]]

    prof = {
        "s_mm": a(s),
        "x_mm": a([x.x for x in r.samples]),
        "y_mm": a([x.y for x in r.samples]),
        "layer": [x.layer for x in r.samples][::keep],
        "width_mm": a([x.width for x in r.samples]),
        "z0_ohm": a(r.z0),
        "z_eff_ohm": a(r.z_eff),
        "eps_eff": a(r.ee),
        "delay_ps_per_mm": a(r.tpd_eff * 1e12),
        "reference_cover": a(r.cover),
        "reference_supported": [bool(v) for v in (r.supported | r.exempt)[::keep]],
        "model_valid": [bool(v) for v in r.valid[::keep]],
        "target_ohm": r.target,
    }
    if r.zdiff is not None:
        prof["zdiff_ohm"] = a(r.zdiff)
        prof["coupled"] = [bool(v) for v in r.coupled[::keep]]
    return {"profile": prof, "tdr": r.tdr, "crosstalk": r.xtalk}


def _series(results: dict[str, NetResult]) -> list[dict[str, Any]]:
    """Curves for the web app: ``{id, x, y, x_unit, y_unit, target, tolerance_pct}`` entries.

    ``profile`` (impedance along the route) and ``tdr`` (apparent impedance against time, at the interface rise
    time) are the net with the largest impedance deviation; ``profile:<net>`` and ``tdr:<net>`` exist for every net.
    """
    def worst(r: NetResult) -> float:
        ok = [v for v, s in zip(r.z_eff, r.samples) if s.kind == "trace" and np.isfinite(v)]
        return max((abs(100 * (v - r.target) / r.target) for v in ok), default=0.0)

    out: list[dict[str, Any]] = []
    lead = max(results, key=lambda n: worst(results[n]))
    for net, r in results.items():
        keep = max(1, len(r.samples) // 300)
        xs = [round(x.s, 3) for x in r.samples][::keep]
        ys = r.z_eff[::keep]
        pts = [(x, round(float(y), 3)) for x, y in zip(xs, ys) if np.isfinite(y)]
        t = r.tdr["interface"]
        tp = [(a, b) for a, b in zip(t["t_ns"], t["z_apparent_ohm"]) if b is not None]
        entries = [
            {"id": "profile", "net": net, "label": f"{net}: impedance along the route", "x": [p[0] for p in pts], "y": [p[1] for p in pts], "x_unit": "mm", "y_unit": "ohm", "target": r.target, "tolerance_pct": 10.0},
            {"id": "tdr", "net": net, "label": f"{net}: TDR, {t['rise_ns']:.3g} ns rise (lossless model)", "x": [round(p[0], 5) for p in tp], "y": [p[1] for p in tp], "x_unit": "ns", "y_unit": "ohm", "target": r.target, "tolerance_pct": 10.0},
        ]
        for e in entries:
            out.append({**e, "id": f"{e['id']}:{net}"})
            if net == lead:
                out.append(dict(e))
    return out


def _dumps(obj: Any) -> str:
    import json

    def default(o):
        if isinstance(o, np.generic):
            return o.item()
        if isinstance(o, np.ndarray):
            return o.tolist()
        raise TypeError(type(o).__name__)

    return json.dumps(obj, default=default, separators=(",", ":"))


def _paint(grid: np.ndarray, x0: float, y0: float, d: float, p0, p1, radius: float, value: float) -> None:
    ix0 = int(math.floor((min(p0[0], p1[0]) - radius - x0) / d))
    ix1 = int(math.ceil((max(p0[0], p1[0]) + radius - x0) / d))
    iy0 = int(math.floor((min(p0[1], p1[1]) - radius - y0) / d))
    iy1 = int(math.ceil((max(p0[1], p1[1]) + radius - y0) / d))
    ix0, iy0 = max(ix0, 0), max(iy0, 0)
    ix1, iy1 = min(ix1, grid.shape[1] - 1), min(iy1, grid.shape[0] - 1)
    if ix1 < ix0 or iy1 < iy0:
        return
    xs = x0 + (np.arange(ix0, ix1 + 1) + 0.5) * d
    ys = y0 + (np.arange(iy0, iy1 + 1) + 0.5) * d
    X, Y = np.meshgrid(xs, ys)
    vx, vy = p1[0] - p0[0], p1[1] - p0[1]
    ll = vx * vx + vy * vy
    if ll < 1e-12:
        dist = np.hypot(X - p0[0], Y - p0[1])
    else:
        t = np.clip(((X - p0[0]) * vx + (Y - p0[1]) * vy) / ll, 0, 1)
        dist = np.hypot(X - (p0[0] + t * vx), Y - (p0[1] + t * vy))
    sub = grid[iy0 : iy1 + 1, ix0 : ix1 + 1]
    sub[dist <= radius] = value


def _cross_check(board: Board, stack: sb.Stack, results: dict[str, NetResult], solver: SectionSolver) -> list[dict[str, Any]]:
    """The field solver's ideal microstrip against the closed-form impedance analyzer, for this board's own stackup."""
    try:
        from ..analyzers import impedance as imp
    except Exception:  # noqa: BLE001
        return []
    rows = []
    seen: set[tuple[str, float]] = set()
    for net, r in results.items():
        for s in r.samples:
            key = (s.layer, round(s.width, 4))
            if s.kind != "trace" or key in seen or len(seen) >= 3:
                continue
            seen.add(key)
            est = imp.estimate(board, s.layer, s.width)
            refs = stack.refs(s.layer)
            if est is None or len(refs) != 1:
                continue
            h, er = stack.gap(s.layer, refs[0])
            t = stack.cu_t.get(s.layer, 0.035)
            fe = fs.solve(fs.microstrip_section(s.width, h, t, er, box=25.0, side=25.0), **solver.params).z0(0)
            sel = [i for i, q in enumerate(r.samples) if q.kind == "trace" and (q.layer, round(q.width, 4)) == key and r.valid[i]]
            real = float(np.median(r.z0[sel])) if sel else None
            rows.append(
                {
                    "net": net,
                    "layer": s.layer,
                    "width_mm": s.width,
                    "analyzer_closed_form_ohm": round(est["z0"], 2),
                    "field_solver_ideal_microstrip_ohm": round(fe, 2),
                    "ideal_difference_pct": round(100 * (fe / est["z0"] - 1), 2),
                    "field_solver_on_the_real_board_median_ohm": None if real is None else round(real, 2),
                    "real_board_difference_pct": None if real is None else round(100 * (real / est["z0"] - 1), 2),
                    "why_they_differ": "the closed form assumes a plane under the track and nothing else; the real board has coplanar ground copper, gaps in the plane and neighbouring tracks",
                }
            )
    return rows


SIMULATOR = SignalSimulator()
