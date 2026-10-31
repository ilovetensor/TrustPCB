"""Matching network on top of a finished antenna result. No solver time.

The solver gives the antenna's S11 at the port (the antenna side of the matching network). The
as-built result puts the schematic parts on that curve; ``tune_matching`` searches standard E24
values for the same positions. The as-built cascade is built with scikit-rf; the search uses the
equivalent impedance recurrence on arrays so it can test a million combinations quickly, and the
winner is checked again with scikit-rf.

Parts are ideal lumped parts (no parasitics, no tolerance). The source is a plain 50 ohm.
Recommended changes are worded for the engineer to apply in their own tool.
"""

from __future__ import annotations

import itertools
import math
from typing import Any

import numpy as np

from .types import MatchElement, MatchNetwork

E24 = [1.0, 1.1, 1.2, 1.3, 1.5, 1.6, 1.8, 2.0, 2.2, 2.4, 2.7, 3.0, 3.3, 3.6, 3.9, 4.3, 4.7, 5.1, 5.6, 6.2, 6.8, 7.5, 8.2, 9.1]
L_RANGE = (0.5e-9, 56e-9)
C_RANGE = (0.3e-12, 22e-12)
Z0 = 50.0


def e24_values(kind: str) -> list[float]:
    lo, hi = L_RANGE if kind == "L" else C_RANGE
    base = 1e-9 if kind == "L" else 1e-12
    out = []
    for dec in (0.1, 1.0, 10.0):
        for m in E24:
            v = m * dec * base
            if lo * 0.999 <= v <= hi * 1.001:
                out.append(v)
    return sorted(set(out))


def nearest_e24(kind: str, value: float) -> float:
    vals = e24_values(kind)
    return min(vals, key=lambda v: abs(math.log(v / value)))


def fmt_value(kind: str, v: float) -> str:
    if kind == "L":
        return f"{v * 1e9:g} nH"
    if kind == "C":
        return f"{v * 1e12:g} pF"
    return f"{v:g} ohm"


# ------------------------------------------------------------------ impedance recurrence


def gamma_to_z(g: np.ndarray, z0: float = Z0) -> np.ndarray:
    return z0 * (1 + g) / (1 - g)


def z_to_gamma(z: np.ndarray, z0: float = Z0) -> np.ndarray:
    return (z - z0) / (z + z0)


def _impedance(kind: str, value: float, w: np.ndarray) -> np.ndarray:
    if kind == "L":
        return 1j * w * value
    if kind == "C":
        return 1.0 / (1j * w * value)
    return np.full_like(w, value, dtype=complex)


def cascade_gamma(g_ant: np.ndarray, freqs: np.ndarray, parts: list[tuple[str, str, float]]) -> np.ndarray:
    """Input reflection seen by the source. ``parts`` run from the antenna toward the source as
    (topology, kind, value), topology series or shunt; a missing part is simply left out."""
    w = 2 * np.pi * np.asarray(freqs, float)
    z = gamma_to_z(np.asarray(g_ant, complex))
    for topo, kind, val in parts:
        zp = _impedance(kind, val, w)
        z = z + zp if topo == "series" else 1.0 / (1.0 / z + 1.0 / zp)
    return z_to_gamma(z)


def skrf_cascade(g_ant: np.ndarray, freqs: np.ndarray, parts: list[tuple[str, str, float]]) -> np.ndarray:
    """The same cascade built with scikit-rf networks (used for the as-built result)."""
    import skrf as rf

    f = rf.Frequency.from_f(np.asarray(freqs, float), unit="hz")
    media = rf.media.DefinedGammaZ0(frequency=f, z0=Z0)
    ant = rf.Network(frequency=f, s=np.asarray(g_ant, complex).reshape(-1, 1, 1), z0=Z0)
    chain = None
    for topo, kind, val in reversed(parts):  # source side first
        if kind == "L":
            n = media.inductor(val) if topo == "series" else media.shunt_inductor(val)
        elif kind == "C":
            n = media.capacitor(val) if topo == "series" else media.shunt_capacitor(val)
        else:
            n = media.resistor(val) if topo == "series" else media.shunt_resistor(val)
        chain = n if chain is None else chain ** n
    net = ant if chain is None else chain ** ant
    return net.s[:, 0, 0]


# ------------------------------------------------------------------ as built


def parts_as_built(network: MatchNetwork, overrides: dict[str, float | None] | None = None) -> tuple[list[tuple[str, str, float]] | None, list[str]]:
    """Fitted parts from the antenna side. ``overrides`` maps a ref to a new value (None removes it)."""
    overrides = overrides or {}
    parts, why = [], []
    for e in sorted(network.elements, key=lambda e: e.position):
        if e.ref in overrides:
            v = overrides[e.ref]
            if v is None:
                continue
            parts.append((e.topology, e.kind, v))
            continue
        if not e.populated:
            continue
        if e.value is None or e.kind not in ("L", "C", "R"):
            why.append(f"{e.ref} is fitted but its value {e.value_text!r} cannot be used")
            continue
        parts.append((e.topology, e.kind, e.value))
    if why:
        return None, why
    return parts, why


def accepted_power_pct(gamma: np.ndarray) -> np.ndarray:
    """Share of the source power that gets past the mismatch at each frequency: 100 * (1 - |S11|^2).

    This is a mismatch figure only. It is not radiated power and not efficiency: loss in the board and the parts is not in it.
    """
    return 100.0 * (1.0 - np.minimum(np.abs(gamma) ** 2, 1.0))


def band_stats(freqs: np.ndarray, gamma: np.ndarray, band_hz: tuple[float, float]) -> dict[str, Any]:
    mag_db = 20 * np.log10(np.maximum(np.abs(gamma), 1e-9))
    m = (freqs >= band_hz[0]) & (freqs <= band_hz[1])
    if not m.any():
        idx = np.argmin(np.abs(freqs - np.mean(band_hz)))
        m = np.zeros_like(m)
        m[idx] = True
    i = int(np.argmin(mag_db))
    below = mag_db <= -10.0
    bw = None
    if below[i]:
        a = b = i
        while a > 0 and below[a - 1]:
            a -= 1
        while b < len(freqs) - 1 and below[b + 1]:
            b += 1
        bw = {"f_low_hz": float(freqs[a]), "f_high_hz": float(freqs[b])}
    return {
        "f_min_hz": float(freqs[i]),
        "s11_min_db": round(float(mag_db[i]), 3),
        "band_worst_db": round(float(mag_db[m].max()), 3),
        "band_best_db": round(float(mag_db[m].min()), 3),
        "band_mean_db": round(float(20 * np.log10(max(float(np.mean(np.abs(gamma[m]))), 1e-9))), 3),
        # accepted power per frequency, then the worst (lowest) and the mean across the band: mismatch only, not radiated power or efficiency
        "band_worst_accepted_power_pct": round(float(accepted_power_pct(gamma[m]).min()), 1),
        "band_mean_accepted_power_pct": round(float(accepted_power_pct(gamma[m]).mean()), 1),
        "band_covered_10db": bool(np.all(mag_db[m] <= -10.0)),
        "minus10db_range": bw,
    }


def as_built(network: MatchNetwork | None, freqs: np.ndarray, s11_complex: np.ndarray, band_hz: tuple[float, float]) -> dict[str, Any]:
    """Antenna-only and as-built S11 summaries. As-built is None when the network is unknown."""
    out: dict[str, Any] = {"antenna_only": band_stats(freqs, s11_complex, band_hz), "as_built": None, "network_note": None}
    if network is None:
        out["network_note"] = "no matching network found at the feed"
        return out
    if not network.understood:
        out["network_note"] = "matching network not fully understood: " + "; ".join(network.notes)
        return out
    parts, why = parts_as_built(network)
    if parts is None:
        out["network_note"] = "; ".join(why)
        return out
    g = skrf_cascade(s11_complex, freqs, parts) if parts else s11_complex
    out["as_built"] = band_stats(freqs, g, band_hz)
    out["as_built"]["parts"] = [{"topology": t, "kind": k, "value": v, "text": fmt_value(k, v)} for t, k, v in parts]
    out["as_built_gamma"] = g
    return out


# ------------------------------------------------------------------ explicit changes and what the agent reads


def _as_built_item(e: MatchElement) -> tuple[str, float] | None:
    return (e.kind, e.value) if e.populated and e.value and e.kind in ("L", "C") else None


def _slot_changed(e: MatchElement, item: tuple[str, float] | None) -> bool:
    """Does `item` differ from what the board has in this position?"""
    was = _as_built_item(e)
    if (was is None) != (item is None):
        return True
    return was is not None and item is not None and (was[0] != item[0] or abs(math.log(item[1] / was[1])) > 0.03)


def apply_changes(network: MatchNetwork | None, freqs: np.ndarray, s11_complex: np.ndarray, band_hz: tuple[float, float],
                  changes: list[dict[str, Any]]) -> dict[str, Any]:
    """The as-built network with explicit part changes applied; positions not named keep their fitted part.

    Each change is {ref, kind: "L" | "C" | None, value} with the value in henry or farad. kind None takes the part out
    (a series position then becomes a 0 ohm link). Costs no solver time.
    """
    if network is None:
        return {"ok": False, "reason": "no matching network found at the feed"}
    if not network.understood:
        return {"ok": False, "reason": "matching network not fully understood: " + "; ".join(network.notes)}
    by_ref = {c["ref"]: c for c in changes}
    unknown = sorted(set(by_ref) - {e.ref for e in network.elements})
    if unknown:
        return {"ok": False, "reason": "these parts are not in the matching network: " + ", ".join(unknown)}
    parts, rows = [], []
    for e in sorted(network.elements, key=lambda e: e.position):
        part: tuple[str, str, float] | None = None
        if e.ref in by_ref:
            kind, value = by_ref[e.ref].get("kind"), by_ref[e.ref].get("value")
            if kind in ("L", "C"):
                if not isinstance(value, (int, float)) or value <= 0:
                    return {"ok": False, "reason": f"{e.ref} needs a positive value"}
                part = (e.topology, kind, float(value))
        elif e.populated:
            if e.value is None or e.kind not in ("L", "C", "R"):
                return {"ok": False, "reason": f"{e.ref} is fitted but its value {e.value_text!r} cannot be used"}
            part = (e.topology, e.kind, e.value)
        if part:
            parts.append(part)
        item = (part[1], part[2]) if part and part[1] in ("L", "C") else None
        was = f"{fmt_value(e.kind, e.value)} (fitted)" if e.populated and e.value is not None else "not fitted"
        now = fmt_value(part[1], part[2]) if part else ("leave empty" if e.topology == "shunt" else "0 ohm link")
        rows.append({"ref": e.ref, "topology": e.topology, "was": was, "now": now, "kind": item[0] if item else None,
                     "value": item[1] if item else None, "changed": _slot_changed(e, item)})
    g = skrf_cascade(s11_complex, freqs, parts) if parts else s11_complex
    return {"ok": True, "parts": rows, "result": band_stats(freqs, g, band_hz), "gamma": g}


def response_fields(freqs: np.ndarray, gamma: np.ndarray, band_hz: tuple[float, float]) -> dict[str, Any]:
    """What the agent reads about a response: every S11 dip below -3 dB, the first series resonance and the
    impedance at the band centre with its character (inductive or capacitive)."""
    freqs = np.asarray(freqs, float)
    db = 20 * np.log10(np.maximum(np.abs(gamma), 1e-9))
    dips = [{"f_hz": float(freqs[i]), "s11_db": round(float(db[i]), 2)} for i in range(1, len(freqs) - 1)
            if db[i] < db[i - 1] and db[i] <= db[i + 1] and db[i] <= -3.0]
    z = gamma_to_z(np.asarray(gamma, complex))
    first = None
    for i in range(len(freqs) - 1):
        if z.imag[i] < 0 <= z.imag[i + 1]:
            first = float(freqs[i] - z.imag[i] * (freqs[i + 1] - freqs[i]) / (z.imag[i + 1] - z.imag[i]))
            break
    centre = float(np.mean(band_hz))
    k = int(np.argmin(np.abs(freqs - centre)))
    return {"dips": dips, "first_series_resonance_hz": first,
            "zin_band_centre": {"f_hz": float(freqs[k]), "re": round(float(z.real[k]), 3), "im": round(float(z.imag[k]), 3)}}


# ------------------------------------------------------------------ tuning


def tune_matching(
    network: MatchNetwork | None,
    freqs: np.ndarray,
    s11_complex: np.ndarray,
    band_hz: tuple[float, float],
    *,
    target_db: float = -10.0,
    allow_slots: str = "all",
) -> dict[str, Any]:
    """Search E24 values for the matching positions on a finished result.

    Every position in the network (fitted or not) may take any E24 inductor or capacitor in range,
    or stay empty (a series position empty is a 0 ohm link). Returns the as-built baseline, the best
    single-part change and the best overall by worst in-band S11. Costs no solver time.
    """
    if network is None:
        return {"ok": False, "reason": "no matching network found at the feed"}
    if not network.understood:
        return {"ok": False, "reason": "matching network not fully understood: " + "; ".join(network.notes)}
    freqs = np.asarray(freqs, float)
    m = (freqs >= band_hz[0]) & (freqs <= band_hz[1])
    if not m.any():
        return {"ok": False, "reason": "the band lies outside the simulated sweep"}
    # a few points across the band; always include its edges
    idx = np.nonzero(m)[0]
    pick = idx[np.unique(np.linspace(0, len(idx) - 1, min(len(idx), 9)).astype(int))]
    f = freqs[pick]
    g_ant = s11_complex[pick]
    slots = sorted(network.elements, key=lambda e: e.position)
    if not slots:
        return {"ok": False, "reason": "no matching positions on this feed"}
    if len(slots) > 4:
        return {"ok": False, "reason": "more than four matching positions"}
    opts = []
    for s in slots:
        o: list[tuple[str, float] | None] = [None]
        if s.kind in ("L", "C", "R"):
            for kind in ("L", "C"):
                o += [(kind, v) for v in e24_values(kind)]
        opts.append(o)
    w = 2 * np.pi * f
    z_ant = gamma_to_z(g_ant)
    # per slot, impedance (series) or admittance (shunt) of every option over the band
    tabs = []
    for s, o in zip(slots, opts):
        arr = np.zeros((len(o), len(f)), complex)
        for k, item in enumerate(o):
            if item is None:
                arr[k] = 0.0 if s.topology == "series" else 0.0
            else:
                zz = _impedance(item[0], item[1], w)
                arr[k] = zz if s.topology == "series" else 1.0 / zz
        tabs.append(arr)

    def apply(z, s, arr_row):
        return z + arr_row if s.topology == "series" else 1.0 / (1.0 / z + arr_row)

    best = (1e9, None)
    # iterate the first slot, broadcast the rest
    counts = [len(o) for o in opts]
    results: list[tuple[float, float, tuple[int, ...]]] = []
    shapes = counts[1:]
    for c0 in range(counts[0]):
        z = apply(z_ant, slots[0], tabs[0][c0])  # (F,)
        z = np.broadcast_to(z, tuple(shapes) + (len(f),)).copy() if shapes else z
        for si in range(1, len(slots)):
            shape = [1] * len(shapes) + [len(f)]
            shape[si - 1] = counts[si]
            arr = tabs[si].reshape(shape)
            z = apply(z, slots[si], arr)
        g = np.abs(z_to_gamma(z))
        worst = g.max(axis=-1)
        mean = g.mean(axis=-1)
        flat = worst.ravel()
        # keep the best few of this slice
        k = min(6, flat.size)
        top = np.argpartition(flat, k - 1)[:k]
        for t in top:
            sub = np.unravel_index(t, worst.shape) if shapes else ()
            results.append((float(flat[t]), float(mean.ravel()[t]), (c0,) + tuple(int(v) for v in sub)))
    # rank: worst in-band |S11|, then fewer fitted parts, then mean
    def nparts(sel):
        return sum(1 for c, o in zip(sel, opts) if o[c] is not None)

    results.sort(key=lambda r: (round(20 * math.log10(max(r[0], 1e-9)), 1), nparts(r[2]), r[1]))
    freqs_all = freqs
    baseline_parts, _ = parts_as_built(network)

    def describe(sel):
        parts, rows = [], []
        for s, c, o in zip(slots, sel, opts):
            item = o[c]
            cur = f"{fmt_value(s.kind, s.value)} ({'fitted' if s.populated else 'not fitted'})" if s.value is not None else ("not fitted" if not s.populated else s.value_text)
            if item is None:
                new = "leave empty" if s.topology == "shunt" else "0 ohm link"
                parts_txt = new
            else:
                parts.append((s.topology, item[0], item[1]))
                parts_txt = fmt_value(item[0], item[1])
            rows.append({"ref": s.ref, "topology": s.topology, "was": cur, "now": parts_txt,
                         "kind": None if item is None else item[0], "value": None if item is None else item[1],
                         "changed": _slot_changed(s, item)})
        g = skrf_cascade(s11_complex, freqs_all, parts) if parts else s11_complex
        st = band_stats(freqs_all, g, band_hz)
        return {"parts": rows, "result": st, "meets_target": st["band_worst_db"] <= target_db, "fitted_parts": len(parts)}

    base = None
    if baseline_parts is not None:
        g = skrf_cascade(s11_complex, freqs_all, baseline_parts) if baseline_parts else s11_complex
        base = band_stats(freqs_all, g, band_hz)
    # best single-part change from the as-built state
    single = None
    if baseline_parts is not None:
        cur_sel = []
        for s, o in zip(slots, opts):
            if not s.populated:
                cur_sel.append(0)
            else:
                cur_sel.append(next((k for k, it in enumerate(o) if it and it[0] == s.kind and s.value and abs(math.log(it[1] / s.value)) < 0.03), None))
        if None not in cur_sel:
            cand = []
            for ri, o in enumerate(opts):
                for c in range(len(o)):
                    sel = list(cur_sel)
                    sel[ri] = c
                    z = z_ant.copy()
                    for si, s in enumerate(slots):
                        z = apply(z, s, tabs[si][sel[si]])
                    cand.append((float(np.abs(z_to_gamma(z)).max()), nparts(sel), tuple(sel)))
            cand.sort(key=lambda r: (round(20 * math.log10(max(r[0], 1e-9)), 1), r[1]))
            if cand and tuple(cur_sel) != cand[0][2]:
                single = describe(cand[0][2])
    best_d = describe(results[0][2])
    top3 = [describe(r[2]) for r in results[:3]]
    return {
        "ok": True,
        "target_db": target_db,
        "band_hz": list(band_hz),
        "as_built": base,
        "best_single_change": single,
        "best": best_d,
        "top": top3,
        "searched": int(np.prod(counts)),
        "note": "Ideal lumped parts, 50 ohm source, no tolerance or parasitics. Apply any change in your own tool and re-measure.",
    }
