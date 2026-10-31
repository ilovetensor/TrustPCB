"""The calls the Review lane uses.

    setup = plan_setup(board, detection)                    # RfSetup or NotSimulatable
    scenario = scenario_for(board, setup, "screen", ops)    # content hash is the cache key
    result = await run_scenario(board, setup, scenario, queue=queue, on_progress=cb)
    tuned = tune_matching(queue.store.scenario_dir(scenario.hash()))   # no solver time

``result`` always carries ``trust``. A result with ``trust.status != "ok"`` cannot support a finding.
Nothing here writes a KiCad file; what-if operations live only inside the simulation model.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from ..pcb.model import Board
from . import matching, postprocess
from .queue import SimQueue, ProgressCb
from .scenario import Scenario, build_job, scenario_for  # noqa: F401  (re-exported)
from .setup import plan_setup  # noqa: F401
from .types import RfSetup
from .whatif import WhatIfError, op_from_dict, validate_ops  # noqa: F401


async def run_scenario(
    board: Board,
    setup: RfSetup,
    scenario: Scenario,
    *,
    queue: SimQueue,
    review_id: str | None = None,
    investigation_id: str | None = None,
    on_progress: ProgressCb | None = None,
    wait: bool = True,
) -> dict[str, Any]:
    """Build the job, queue it, and (by default) wait for the processed result.

    A duplicate scenario returns the cached result at once (``cached: true``) and costs no solver
    time. Raises ``WhatIfError`` when a what-if operation breaks a hard limit. With ``wait=False``
    the ``SimHandle`` is returned so the caller can ``cancel`` it.
    """
    h = scenario.hash()
    job_dir = queue.store.scenario_dir(h)
    if queue.cached_summary(h) is None:
        build_job(board, setup, scenario, job_dir)
    handle = await queue.submit(h, job_dir, review_id=review_id, investigation_id=investigation_id, on_progress=on_progress, scenario=scenario.to_dict())
    return await handle.wait() if wait else handle


def tune_matching(job_dir: str | Path, *, target_db: float = -10.0, band_ghz: tuple[float, float] | None = None) -> dict[str, Any]:
    """E24 matching search on a finished job. Changes no geometry and costs no solver time."""
    job = Path(job_dir)
    s11 = json.loads((job / "s11.json").read_text())
    meta = json.loads((job / "meta.json").read_text())
    f = np.array(s11["freq_hz"])
    g = np.array(s11["s11_re"]) + 1j * np.array(s11["s11_im"])
    band = tuple(g_ * 1e9 for g_ in (band_ghz or meta["setup"]["band_ghz"]))
    return matching.tune_matching(postprocess.network_from_dict(meta["setup"].get("network")), f, g, band, target_db=target_db)


def match_changes(job_dir: str | Path, changes: list[dict[str, Any]], *, band_ghz: tuple[float, float] | None = None) -> dict[str, Any]:
    """Explicit part changes (ref, kind L or C or None, value in SI units) applied to a finished job, with the response fields
    the agent reads (dips, first series resonance, impedance at the band centre) and the S11 curve. No solver time."""
    job = Path(job_dir)
    s11 = json.loads((job / "s11.json").read_text())
    meta = json.loads((job / "meta.json").read_text())
    f = np.array(s11["freq_hz"])
    g = np.array(s11["s11_re"]) + 1j * np.array(s11["s11_im"])
    band = tuple(g_ * 1e9 for g_ in (band_ghz or meta["setup"]["band_ghz"]))
    out = matching.apply_changes(postprocess.network_from_dict(meta["setup"].get("network")), f, g, band, changes)
    if not out.get("ok"):
        return out
    gamma = out.pop("gamma")
    out.update(matching.response_fields(f, gamma, band))
    out["curve"] = {"freq_hz": [round(v) for v in f.tolist()], "s11_db": [round(float(v), 2) for v in 20 * np.log10(np.maximum(np.abs(gamma), 1e-9))], "basis": "as_built_with_changes"}
    return out
