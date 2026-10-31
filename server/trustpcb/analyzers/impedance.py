"""impedance: closed-form estimate of track impedance from width, layer and stackup.

Microstrip uses the Hammerstad and Jensen closed form (thickness-corrected strip width, effective
permittivity from the strip ratio). A track between two copper layers uses a centred stripline
approximation. Every value is an estimate: it assumes a solid reference plane on the adjacent copper
layer and ignores solder mask, coupling to neighbours and etch factor.

Candidates are tracks on controlled nets (fast, RF, or listed in ``net_targets``) more than 10
percent from their target, grouped into sections of equal width. Each candidate also carries:

* ``width_for_target``: the width that gives the target on the same stackup (same closed form);
* ``stackup_assumptions``: where the stackup came from and the numbers used (permittivity, dielectric
  height, copper thickness, the reference plane assumed);
* ``reference``: how much of the section has no ground copper on the reference layer (a gap in the
  plane makes the estimate optimistic), and ``net_reference`` for the whole net;
* ``net_class``: the project's own net class for the net with its track width, and
  ``class_width_z0_estimate`` (what that width would give on this stackup), when the project file is there. Tracks that are part of a printed antenna
(see ``radiating``) are left out: they are meant to be that shape. ``overlays`` returns a colour for
every track for the UI, labelled as an estimate.
"""

from __future__ import annotations

import math
from types import SimpleNamespace
from typing import Any

import numpy as np
import shapely

from ..pcb.model import Board
from .base import AnalyzerContext, Emitter, mark, meas
from .classify import is_diff_net, is_fast_net, is_ground, is_power, is_rf_net
from .project import net_class_info
from .radiating import radiating
from .return_path import adjacent_layers, ground_for, sample_run
from .topology import get_topology

RULE = "impedance"
ETA0 = 376.730313668
DEFAULT_ER = 4.5
DEFAULT_CU = 0.035


# ---------------------------------------------------------------- closed forms


def _z01(u: float) -> float:
    """Impedance of a strip of ratio ``u`` = w/h in air (Hammerstad and Jensen)."""
    f = 6.0 + (2 * math.pi - 6.0) * math.exp(-((30.666 / u) ** 0.7528))
    return ETA0 / (2 * math.pi) * math.log(f / u + math.sqrt(1 + (2 / u) ** 2))


def _eeff(u: float, er: float) -> float:
    a = 1 + math.log((u**4 + (u / 52) ** 2) / (u**4 + 0.432)) / 49 + math.log(1 + (u / 18.1) ** 3) / 18.7
    b = 0.564 * ((er - 0.9) / (er + 3)) ** 0.053
    return (er + 1) / 2 + (er - 1) / 2 * (1 + 10 / u) ** (-a * b)


def microstrip_z0(w: float, h: float, t: float, er: float) -> tuple[float, float]:
    """(Z0 in ohms, effective permittivity) for a strip of width ``w``, copper ``t``, over height ``h``."""
    u = max(w, 1e-4) / h
    if t > 0:
        x = t / h
        coth2 = 1.0 / math.tanh(math.sqrt(6.517 * u)) ** 2
        du1 = x / math.pi * math.log(1 + 4 * math.e / (x * coth2))
        dur = 0.5 * (1 + 1 / math.cosh(math.sqrt(max(er - 1, 0)))) * du1
        u = u + dur
    ee = _eeff(u, er)
    return _z01(u) / math.sqrt(ee), ee


def stripline_z0(w: float, b: float, t: float, er: float) -> float:
    """Centred stripline between planes ``b`` apart (IPC-2141 form), ohms."""
    return 60.0 / math.sqrt(er) * math.log(1.9 * b / (0.8 * w + t))


# ---------------------------------------------------------------- stackup


def layer_geometry(board: Board, layer: str) -> dict[str, Any] | None:
    """Dielectric height, permittivity and copper thickness around a copper layer, from the stackup."""
    stack = board.stackup.get("layers") or []
    names = [s.get("name") for s in stack]
    if layer not in names:
        return None
    i = names.index(layer)
    cu_t = stack[i].get("thickness_mm") or DEFAULT_CU

    def walk(step: int) -> tuple[float, float, str] | None:
        h = 0.0
        eh = 0.0
        j = i + step
        while 0 <= j < len(stack):
            s = stack[j]
            if s.get("kind") == "copper":
                return (h, eh / h, s.get("name")) if h > 0 else None
            if s.get("kind") == "dielectric" and s.get("thickness_mm"):
                th = float(s["thickness_mm"])
                h += th
                eh += th * float(s.get("epsilon_r") or DEFAULT_ER)
            j += step
        return None

    up, down = walk(-1), walk(+1)
    return {"t": float(cu_t), "up": up, "down": down}


def estimate(board: Board, layer: str, width: float) -> dict[str, Any] | None:
    g = layer_geometry(board, layer)
    if g is None or width <= 0:
        return None
    up, down = g["up"], g["down"]
    t = g["t"]
    if up and down:
        h1, e1, n1 = up
        h2, e2, n2 = down
        er = (e1 * h1 + e2 * h2) / (h1 + h2)
        b = h1 + h2 + t
        return {"z0": stripline_z0(width, b, t, er), "model": "stripline", "height_mm": h1 + h2, "er": er, "t": t, "reference_layers": [n1, n2]}
    side = up or down
    if not side:
        return None
    h, er, ref = side
    z0, ee = microstrip_z0(width, h, t, er)
    return {"z0": z0, "model": "microstrip", "height_mm": h, "er": er, "er_eff": ee, "t": t, "reference_layers": [ref]}


def width_for_target(board: Board, layer: str, target: float) -> float | None:
    """Track width in mm that gives ``target`` ohms on this layer and stackup (same closed form as the estimate).

    Z0 falls as the track widens, so the width is found by bisection between 0.01 mm and 40 times the
    dielectric height. None when the target cannot be reached in that range.
    """
    probe = estimate(board, layer, 1.0)
    if probe is None or target <= 0:
        return None
    lo, hi = 0.01, max(probe["height_mm"] * 40.0, 1.0)
    z_lo, z_hi = estimate(board, layer, lo)["z0"], estimate(board, layer, hi)["z0"]
    if not (z_hi <= target <= z_lo):
        return None
    for _ in range(80):
        mid = (lo + hi) / 2
        if estimate(board, layer, mid)["z0"] > target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def stackup_assumptions(board: Board, est: dict[str, Any]) -> dict[str, Any]:
    """The numbers an estimate rests on, as data, so a reader can see what was assumed."""
    stack = board.stackup or {}
    out: dict[str, Any] = {
        "stackup_source": stack.get("source"),
        "stackup_assumed": bool(stack.get("assumed")),
        "model": est["model"],
        "epsilon_r": round(est["er"], 3),
        "dielectric_height_mm": round(est["height_mm"], 4),
        "copper_thickness_mm": round(est["t"], 4),
        "reference_layers": est["reference_layers"],
        "reference_plane_assumed": "a solid ground plane on " + " and ".join(est["reference_layers"]) + ", with no gaps",
        "ignored": "solder mask, coupling to neighbouring tracks, etch factor and surface roughness",
    }
    if "er_eff" in est:
        out["epsilon_r_effective"] = round(est["er_eff"], 3)
    return out


def reference_support(board: Board, ctx: AnalyzerContext, segs: list, layer: str) -> dict[str, Any]:
    """How much of ``segs`` (on ``layer``) lies over ground copper on every reference layer.

    Samples every ``return_path_step_mm`` and weights each sample by the track it stands for, the same
    way ``return_path`` does. Ground zones with no fill data count as their outline, which can only hide a gap.
    """
    gnd, approx = ground_for(board, ctx)
    refs = adjacent_layers(board, layer)
    samples = sample_run(SimpleNamespace(segs=segs), float(ctx.t("return_path_step_mm")))
    total = sum(x[4] for x in samples)
    if not samples or not refs:
        return {"reference_layers": refs, "length_mm": round(total, 2), "unsupported_mm": 0.0 if refs else round(total, 2), "no_ground_on_reference": not refs, "from_outline": False}
    xs = np.array([x[0] for x in samples])
    ys = np.array([x[1] for x in samples])
    supported = np.ones(len(samples), dtype=bool)
    for ref in refs:
        g = gnd.get(ref)
        supported &= shapely.contains_xy(g, xs, ys) if g is not None else np.zeros(len(samples), dtype=bool)
    missing = sum(x[4] for x, ok in zip(samples, supported) if not ok)
    return {
        "reference_layers": refs,
        "length_mm": round(total, 2),
        "unsupported_mm": round(missing, 2),
        "no_ground_on_reference": all(gnd.get(r) is None for r in refs),
        "from_outline": any(r in approx for r in refs),
    }


# ---------------------------------------------------------------- targets


def net_target(net: str | None, ctx: AnalyzerContext) -> tuple[float, str]:
    if net and net in ctx.net_targets:
        return float(ctx.net_targets[net]), "brief"
    if net and is_diff_net(net) and not is_rf_net(net, ctx):
        return float(ctx.diff_target_ohms) / 2.0, "differential target halved (each leg, uncoupled)"
    return float(ctx.target_ohms), "target_ohms"


def controlled(net: str | None, ctx: AnalyzerContext) -> bool:
    if not net or is_ground(net) or is_power(net, ctx):
        return False
    return net in ctx.net_targets or is_fast_net(net, ctx)


def _color(dev_pct: float, tol: float) -> tuple[str, str]:
    a = abs(dev_pct)
    if a <= tol:
        return "#2e9e5b", "within tolerance"
    if a <= 2.5 * tol:
        return "#e0b43a", "10 to 25 percent off" if tol == 10 else "near"
    if a <= 5 * tol:
        return "#e8793a", "25 to 50 percent off" if tol == 10 else "far"
    return "#d63b3b", "more than 50 percent off" if tol == 10 else "very far"


def analyze(board: Board, ctx: AnalyzerContext) -> list[dict[str, Any]]:
    topo = get_topology(board, ctx)
    tol = float(ctx.t("impedance_tolerance_pct"))
    min_section = float(ctx.t("impedance_min_section_mm"))
    cache: dict[tuple[str, float], dict[str, Any] | None] = {}
    rad = radiating(board, ctx)
    # sections: consecutive segments of equal width in a run, pooled by (net, layer, width)
    pooled: dict[tuple[str, str, float], dict[str, Any]] = {}
    order: list[tuple[str, str, float]] = []
    for run in topo.runs:
        if not controlled(run.net, ctx):
            continue
        i = 0
        while i < len(run.segs):
            if run.segs[i].parent in rad.track_ids:
                i += 1  # radiating element: its width and shape are the antenna design, not a line to match
                continue
            j = i
            while j + 1 < len(run.segs) and abs(run.segs[j + 1].width - run.segs[i].width) < 1e-6 and run.segs[j + 1].parent not in rad.track_ids:
                j += 1
            segs = run.segs[i : j + 1]
            i = j + 1
            length = sum(s.length for s in segs)
            if length < min_section:
                continue
            key = (run.net, run.layer, round(segs[0].width, 4))
            if key not in pooled:
                pooled[key] = {"tracks": [], "segs": [], "length": 0.0, "sections": 0, "best": (0.0, None)}
                order.append(key)
            p = pooled[key]
            p["tracks"].extend(s.parent for s in segs)
            p["segs"].extend(segs)
            p["length"] += length
            p["sections"] += 1
            if length > p["best"][0]:
                p["best"] = (length, segs[len(segs) // 2].pts[0])
    net_segs: dict[tuple[str, str], list] = {}
    for run in topo.runs:
        if controlled(run.net, ctx):
            net_segs.setdefault((run.net, run.layer), []).extend(sg for sg in run.segs if sg.parent not in rad.track_ids)
    net_ref: dict[tuple[str, str], dict[str, Any]] = {}
    em = Emitter(RULE)
    for key in order:
        net, layer, width = key
        p = pooled[key]
        ck = (layer, width)
        if ck not in cache:
            cache[ck] = estimate(board, layer, width)
        est = cache[ck]
        if est is None:
            continue
        target, basis = net_target(net, ctx)
        dev = 100.0 * (est["z0"] - target) / target
        if abs(dev) <= tol:
            continue
        tracks = list(dict.fromkeys(p["tracks"]))
        ref = reference_support(board, ctx, p["segs"], layer)
        if (net, layer) not in net_ref:
            net_ref[(net, layer)] = reference_support(board, ctx, net_segs.get((net, layer), []), layer)
        nref = net_ref[(net, layer)]
        need = width_for_target(board, layer, target)
        cls = net_class_info(board, ctx, net)
        m_extra: dict[str, Any] = {}
        if need is not None:
            m_extra["width_for_target"] = meas(need, "mm", 3)
        extra: dict[str, Any] = {}
        if cls is not None:
            extra["net_class"] = cls
            if "track_width_mm" in cls:
                m_extra["class_track_width"] = meas(cls["track_width_mm"], "mm", 4)
                cz = estimate(board, layer, cls["track_width_mm"])
                if cz is not None:
                    m_extra["class_width_z0_estimate"] = meas(cz["z0"], "ohm", 1)
        m_extra["reference_gap_length"] = meas(ref["unsupported_mm"], "mm", 2)
        m_extra["net_length"] = meas(nref["length_mm"], "mm", 2)
        m_extra["net_reference_gap_length"] = meas(nref["unsupported_mm"], "mm", 2)
        em.add(
            f"Estimated {est['z0']:.0f} ohm on {net} ({p['length']:.1f} mm at width {width:g} mm), target {target:g} ohm",
            {
                "z0_estimate": meas(est["z0"], "ohm", 1),
                "target": meas(target, "ohm", 1),
                "deviation": meas(dev, "percent", 1),
                "width": meas(width, "mm", 3),
                "dielectric_height": meas(est["height_mm"], "mm", 3),
                "epsilon_r": meas(est["er"], "", 2),
                "section_length": meas(p["length"], "mm", 2),
                "sections": meas(p["sections"], "count"),
                **m_extra,
            },
            tracks,
            [mark("path", tracks, label=f"~{est['z0']:.0f} ohm (estimate)", value=meas(est["z0"], "ohm est.", 0))],
            "medium" if abs(dev) > 3 * tol else "low",
            position=p["best"][1],
            net=net,
            layer=layer,
            estimate=True,
            model=est["model"],
            target_basis=basis,
            stackup_assumptions=stackup_assumptions(board, est),
            reference=ref,
            net_reference=nref,
            reference_plane_missing=ref["unsupported_mm"] > 0.0,
            **extra,
        )
    return em.items


def overlays(board: Board, ctx: AnalyzerContext) -> dict[str, Any]:
    tol = float(ctx.t("impedance_tolerance_pct"))
    items = []
    cache: dict[tuple[str, float], dict[str, Any] | None] = {}
    for t in sorted(board.tracks, key=lambda t: t.id):
        key = (t.layer, round(t.width, 4))
        if key not in cache:
            cache[key] = estimate(board, t.layer, t.width)
        est = cache[key]
        if est is None:
            continue
        target, _ = net_target(t.net, ctx)
        dev = 100.0 * (est["z0"] - target) / target
        color, bucket = _color(dev, tol)
        items.append(
            {
                "track": t.id,
                "net": t.net,
                "layer": t.layer,
                "width_mm": round(t.width, 4),
                "z0_ohm": round(est["z0"], 1),
                "target_ohm": round(target, 1),
                "deviation_pct": round(dev, 1),
                "color": color,
                "bucket": bucket,
                "controlled": controlled(t.net, ctx),
            }
        )
    return {
        "label": "Estimated impedance (closed-form, single-ended; assumes a solid reference plane on the adjacent layer)",
        "estimate": True,
        "method": "Hammerstad-Jensen microstrip, IPC-2141 stripline",
        "stackup": {"source": board.stackup.get("source"), "assumed": board.stackup.get("assumed")},
        "target_ohms": ctx.target_ohms,
        "tolerance_pct": tol,
        "legend": [
            {"color": "#2e9e5b", "label": f"within {tol:g} percent of target"},
            {"color": "#e0b43a", "label": f"{tol:g} to {2.5 * tol:g} percent off"},
            {"color": "#e8793a", "label": f"{2.5 * tol:g} to {5 * tol:g} percent off"},
            {"color": "#d63b3b", "label": f"more than {5 * tol:g} percent off"},
        ],
        "tracks": items,
    }
