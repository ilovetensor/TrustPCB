"""Real solver runs through the queue. Slow: minutes each. Needs the trustpcb-ems:dev image."""
from __future__ import annotations

import shutil
import subprocess

import pytest

from trustpcb.rf import scenario as S
from trustpcb.rf.lab import run_scenario, tune_matching
from trustpcb.rf.queue import SimQueue
from trustpcb.storage import Store

pytestmark = pytest.mark.slow


def _have_image():
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "image", "inspect", "trustpcb-ems:dev"], capture_output=True).returncode == 0


@pytest.mark.skipif(not _have_image(), reason="docker image trustpcb-ems:dev not built")
async def test_patch_board_resonates_near_design_frequency(tmp_path, patch_board, patch_setup):
    store = Store(tmp_path)
    q = SimQueue(store, timeout_s=900)
    sc = S.scenario_for(patch_board, patch_setup, "screen")
    out = await run_scenario(patch_board, patch_setup, sc, queue=q)
    assert out["status"] == "done" and out["trust"]["status"] == "ok", out["trust"]
    # the board's own design estimate is 2.5 GHz; allow a wide window, a patch of this size cannot sit elsewhere
    assert 2.2e9 < out["sim"]["f_res"] < 2.8e9 and out["sim"]["s11_min_db"] < -6
    assert 3.0 < out["sim"]["dmax_dbi"] < 9.0
    again = await run_scenario(patch_board, patch_setup, sc, queue=q)
    assert again["cached"] and q.stats["ran"] == 1
    assert tune_matching(store.scenario_dir(sc.hash()))["ok"]
