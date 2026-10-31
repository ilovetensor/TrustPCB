"""Combine the worker output with the model metadata into one result with a trust verdict.

``finalize(job_dir)`` reads result.json, s11.json, zin.json, pattern.json and meta.json, adds
the as-built (matching network) result, runs the trust checks in code, flags physical
plausibility, and writes ``summary.json`` into the job directory. The agent sees ``trust``
with every result; a result whose trust is not ``ok`` cannot support a finding.

Field and current sidecars are in scenario millimetres, where scenario y is minus board y.
``board_plane`` converts a sidecar to board coordinates.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np

from . import matching
from .types import MatchElement, MatchNetwork

NOTE = "Simulated. Not a substitute for a real antenna measurement."
# screen against confirm agreement limits
AGREE = {"band_worst_db": 2.0, "zin_rel": 0.35, "first_resonance_rel": 0.06, "dmax_db": 1.0}


def _read(job: Path, name: str):
    p = job / name
    return json.loads(p.read_text()) if p.exists() else None


def network_from_dict(d: dict[str, Any] | None) -> MatchNetwork | None:
    if not d:
        return None
    els = [MatchElement(e["ref"], e["kind"], e["value"], e["value_text"], e["populated"], e["topology"], tuple(e["nets"]),
                        e.get("chain_net"), e.get("position", 0)) for e in d["elements"]]
    return MatchNetwork(d["chain"], els, d.get("source"), d.get("understood", True), d.get("notes", []))


def trust_checks(res: dict[str, Any], meta: dict[str, Any], scenario_json: dict[str, Any] | None) -> dict[str, Any]:
    """The checks of spec section 7, computed from the worker result and the model metadata."""
    wt = res.get("trust", {})
    mesh = meta.get("mesh", {})
    target = (scenario_json or {}).get("end_criteria", -40.0)
    checks = []

    def add(name, ok, detail):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    decay = wt.get("energy_decay_db")
    add("energy_decayed", decay is not None and decay <= target + 0.5,
        f"energy decayed to {decay} dB, target {target} dB" if decay is not None else "solver energy not reported")
    add("passive", wt.get("passive", False), "S11 never above 0 dB" if wt.get("passive") else "S11 above 0 dB")
    cpw = wt.get("cells_per_wavelength")
    own = mesh.get("cells_per_wavelength_substrate")
    add("mesh_wavelength", (cpw or 0) >= 20 and (own or 0) >= 20,
        f"{cpw} cells per wavelength in the substrate (solver), {own} (model); need 20")
    across = mesh.get("min_cells_across_feature")
    add("mesh_across_feature", across is None or across >= 2.0,
        f"{across} cells across the narrowest antenna feature ({mesh.get('narrowest_feature_mm')} mm, {mesh.get('narrowest_feature')}); need 2")
    port_reasons = [r for r in wt.get("reasons", []) if "port" in r]
    add("port_on_copper_with_ground", not port_reasons, "; ".join(port_reasons) or "port on copper, ground reference below")
    err = res.get("error")
    other = [r for r in wt.get("reasons", []) if r not in port_reasons and not any(k in r for k in ("energy", "S11 reaches", "cells per wavelength", "narrowest"))]
    if err or other:
        add("solver_clean", False, err or "; ".join(other))
    reasons = [f"{c['check']}: {c['detail']}" for c in checks if not c["ok"]]
    return {"status": "ok" if not reasons else "fail", "checks": checks, "reasons": reasons}


def plausibility(res: dict[str, Any], band_hz: tuple[float, float], stats: dict[str, Any]) -> list[str]:
    """Flags for a human or the agent; these do not change trust."""
    flags = []
    d = res.get("dmax_dbi")
    if d is not None and not (-3.0 <= d <= 7.0):
        flags.append(f"max directivity {d} dBi is outside the 0 to 3 dBi range expected for a small antenna (allowed -3 to 7)")
    e = res.get("efficiency_est")
    if e is not None and e > 1.05:
        flags.append(f"radiated/accepted power ratio {e} is above 1: energy is not conserved, check the model")
    f = res.get("f_res")
    if f and not (band_hz[0] * 0.8 <= f <= band_hz[1] * 1.2):
        flags.append(f"deepest S11 at {f / 1e9:.3f} GHz is more than 20 percent outside the {band_hz[0] / 1e9:.2f} to {band_hz[1] / 1e9:.2f} GHz band")
    if res.get("s11_min_db", 0) > -3:
        flags.append("no resonance: S11 stays above -3 dB across the sweep")
    return flags


def modes(job: Path) -> dict[str, Any]:
    """Resonance features from S11 and Zin: local S11 dips and the first series resonance.

    The deepest dip alone is fragile (two dips of similar depth swap places between mesh qualities),
    so the summary lists every dip below -3 dB and the first frequency where Im(Zin) rises through zero.
    """
    s11, zin = _read(job, "s11.json"), _read(job, "zin.json")
    out: dict[str, Any] = {"s11_dips": [], "first_series_resonance_hz": None}
    if s11:
        f, db = np.array(s11["freq_hz"]), np.array(s11["s11_db"])
        for i in range(1, len(f) - 1):
            if db[i] < db[i - 1] and db[i] <= db[i + 1] and db[i] <= -3.0:
                out["s11_dips"].append({"f_hz": float(f[i]), "s11_db": round(float(db[i]), 2)})
    if zin:
        f, x = np.array(zin["freq_hz"]), np.array(zin["z_im"])
        for i in range(len(f) - 1):
            if x[i] < 0 <= x[i + 1]:
                out["first_series_resonance_hz"] = float(f[i] - x[i] * (f[i + 1] - f[i]) / (x[i + 1] - x[i]))
                break
    return out


def finalize(job_dir: str | Path, *, write: bool = True) -> dict[str, Any]:
    job = Path(job_dir)
    res = _read(job, "result.json")
    meta = _read(job, "meta.json")
    sc = _read(job, "scenario.json")
    if res is None or meta is None:
        raise FileNotFoundError("job has no result.json or meta.json")
    s11 = _read(job, "s11.json")
    summary: dict[str, Any] = {
        "scenario_hash": meta["scenario_hash"],
        "quality": meta["scenario"]["quality"],
        "note": NOTE,
        "setup": {k: meta["setup"][k] for k in ("kind", "antenna_id", "antenna_net", "port_xy", "port_on", "band_ghz", "region", "narrowest_mm", "assumptions", "warnings")},
        "mesh": meta["mesh"],
        "whatif": meta["scenario"]["whatif_ops"],
        "whatif_notes": meta.get("whatif_notes", []),
        "material": meta.get("material"),
    }
    summary["trust"] = trust_checks(res, meta, sc)
    if "error" in res or s11 is None:
        summary["error"] = res.get("error", "no S11 output")
        summary["trust"]["status"] = "fail"
        if write:
            (job / "summary.json").write_text(json.dumps(summary, indent=1, default=float))
        return summary
    band_hz = tuple(g * 1e9 for g in meta["setup"]["band_ghz"])
    f = np.array(s11["freq_hz"])
    g = np.array(s11["s11_re"]) + 1j * np.array(s11["s11_im"])
    network = network_from_dict(meta["setup"].get("network"))
    ab = matching.as_built(network, f, g, band_hz)
    gamma_ab = ab.pop("as_built_gamma", None)
    if gamma_ab is not None:
        (job / "s11_as_built.json").write_text(json.dumps({"freq_hz": s11["freq_hz"], "s11_db": [round(float(v), 4) for v in 20 * np.log10(np.maximum(np.abs(gamma_ab), 1e-9))],
                                                           "s11_re": [round(float(v), 6) for v in gamma_ab.real], "s11_im": [round(float(v), 6) for v in gamma_ab.imag]}))
    summary["s11"] = ab
    summary["sim"] = {k: res.get(k) for k in ("f_res", "s11_min_db", "bandwidth", "zin_at_res", "dmax_dbi", "efficiency_est", "pattern_f_hz", "cells", "timesteps", "solve_s", "nf2ff_s", "total_s")}
    summary["sim"].update(modes(job))
    summary["zin_at_band_centre"] = _zin_at(job, sum(band_hz) / 2)
    summary["plausibility"] = plausibility(res, band_hz, ab["antenna_only"])
    pat = _read(job, "pattern.json")
    if pat:
        summary["pattern"] = {"f_hz": pat["f_hz"], "dmax_dbi": pat["dmax_dbi"], "dmax_theta_deg": pat["dmax_theta_deg"], "dmax_phi_deg": pat["dmax_phi_deg"],
                              "note": "scenario axes: z up, phi from +x toward +y where scenario y is minus board y"}
    summary["files"] = sorted(p.name for p in job.iterdir() if p.is_file())
    if write:
        (job / "summary.json").write_text(json.dumps(summary, indent=1, default=float))
    return summary


def _zin_at(job: Path, f_hz: float) -> dict[str, float] | None:
    z = _read(job, "zin.json")
    if not z:
        return None
    i = int(np.argmin(np.abs(np.array(z["freq_hz"]) - f_hz)))
    return {"f_hz": z["freq_hz"][i], "re": z["z_re"][i], "im": z["z_im"][i]}


def compare_runs(screen: dict[str, Any], confirm: dict[str, Any]) -> dict[str, Any]:
    """Screen and confirm agree within limits, or the confirm result replaces the screen result.

    Compared: in-band worst S11 (dB), impedance at the band centre (relative difference), the first
    series resonance (relative), and maximum directivity. The deepest-dip frequency is reported but
    not used on its own: it jumps between modes of similar depth.
    """
    a, b = screen["sim"], confirm["sim"]
    d: dict[str, float] = {}
    lim = dict(AGREE)
    d["band_worst_db"] = abs(screen["s11"]["antenna_only"]["band_worst_db"] - confirm["s11"]["antenna_only"]["band_worst_db"])
    za, zb = screen.get("zin_at_band_centre"), confirm.get("zin_at_band_centre")
    if za and zb:
        d["zin_rel"] = abs(complex(za["re"], za["im"]) - complex(zb["re"], zb["im"])) / max(abs(complex(zb["re"], zb["im"])), 1.0)
    fa, fb = a.get("first_series_resonance_hz"), b.get("first_series_resonance_hz")
    if fa and fb:
        d["first_resonance_rel"] = abs(fa - fb) / fb
    d["dmax_db"] = abs((a["dmax_dbi"] or 0) - (b["dmax_dbi"] or 0))
    d["deepest_dip_hz"] = abs(a["f_res"] - b["f_res"])  # informational
    bad = [f"{k} differs by {d[k]:.3g} (limit {lim[k]:.3g})" for k in lim if k in d and d[k] > lim[k]]
    if bool(fa) != bool(fb):
        bad.append("only one of the two runs shows a series resonance")
    return {"agree": not bad, "deltas": d, "limits": lim, "reasons": bad,
            "verdict": "screen and confirm agree" if not bad else "confirm result replaces the screen result"}


def board_plane(sidecar: dict[str, Any]) -> dict[str, Any]:
    """Sidecar plane extents in board millimetres (y negated). Rows still run in scenario y."""
    out = dict(sidecar)
    if "v_mm" in sidecar and sidecar.get("v_axis") == "y":
        lo, hi = sidecar["v_mm"]
        out["v_mm_board"] = [-hi, -lo]
        out["rows_run"] = "decreasing board y (row 0 is the largest board y)"
    return out
