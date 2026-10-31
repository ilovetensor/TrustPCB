from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from trustpcb.rf import scenario as S
from trustpcb.rf.queue import SimQueue, progress_fraction
from trustpcb.storage import Store

HERE = Path(__file__).parent
FAKE = HERE / "fake_docker.py"


@pytest.fixture()
def env(tmp_path, monkeypatch, nrf_board, nrf_setup):
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("FAKE_DOCKER_STATE", str(state))
    wrapper = tmp_path / "docker"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE} \"$@\"\n")
    wrapper.chmod(0o755)
    store = Store(tmp_path / "data")
    q = SimQueue(store, docker=str(wrapper), poll_s=0.1, timeout_s=20)
    sc = S.scenario_for(nrf_board, nrf_setup, "screen")
    job = store.scenario_dir(sc.hash())
    S.build_job(nrf_board, nrf_setup, sc, job, debug_png=False)
    # the fake worker's result needs meta.json for post-processing: build_job wrote it
    return q, store, sc, job, state


async def test_run_progress_and_cache(env):
    q, store, sc, job, state = env
    seen = []
    h = await q.submit(sc.hash(), job, on_progress=lambda p: seen.append(p))
    out = await h.wait()
    assert out["status"] == "done" and not out["cached"] and out["trust"]["status"] in ("ok", "fail")
    cmd = json.loads((state / "last_cmd.json").read_text())
    assert ["--cpus", "6"] == cmd[cmd.index("--cpus"):cmd.index("--cpus") + 2] and "--memory" in cmd and "--rm" in cmd
    mounts = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-v"]
    assert any(m.endswith(":/app:ro") and m.split(":")[0].endswith("/worker") for m in mounts)   # the repo worker, no rebuild
    fr = [p["fraction"] for p in seen]
    assert fr == sorted(fr) and fr[-1] == 1.0 and any(0 < f < 1 for f in fr)
    row = store.get_sim_job(h.job_id)
    assert row["status"] == "done" and row["progress"] == 1.0 and row["trust"]
    # the same hash again: cache hit, no second run
    h2 = await q.submit(sc.hash(), job)
    assert h2.cached and (await h2.wait())["cached"] and q.stats == {"submitted": 2, "cache_hits": 1, "ran": 1}


async def test_cancel_kills_container(env):
    q, store, sc, job, state = env
    h = await q.submit(sc.hash(), job)
    for _ in range(50):
        await asyncio.sleep(0.1)
        if (state / "running").exists():
            break
    assert await q.cancel(h.job_id)
    out = await h.wait()
    assert out["status"] == "canceled" and store.get_sim_job(h.job_id)["status"] == "canceled"
    assert list(state.glob("kill-trustpcb-ems-*"))


async def test_timeout(env, monkeypatch):
    q, store, sc, job, state = env
    monkeypatch.setenv("FAKE_DOCKER_MODE", "hang")
    q.timeout_s = 1.0
    out = await (await q.submit(sc.hash(), job)).wait()
    assert out["status"] == "timeout" and "limit" in out["error"]


async def test_failed_run_is_not_cached(env, monkeypatch):
    q, store, sc, job, state = env
    monkeypatch.setenv("FAKE_DOCKER_MODE", "fail")
    out = await (await q.submit(sc.hash(), job)).wait()
    assert out["status"] == "failed" and "boom" in out["error"]
    assert q.cached_summary(sc.hash()) is None


async def test_one_at_a_time(env):
    q, store, sc, job, state = env
    sc2 = sc.with_quality("confirm")
    job2 = store.scenario_dir(sc2.hash())
    (job2 / "scenario.json").write_text((job / "scenario.json").read_text())
    (job2 / "meta.json").write_text((job / "meta.json").read_text().replace(sc.hash(), sc2.hash()))
    a = await q.submit(sc.hash(), job)
    b = await q.submit(sc2.hash(), job2)
    ra = await a.wait()
    assert not b._fut.done()  # second waits for the first
    assert (await b.wait())["status"] == "done"


def test_recover_interrupted(tmp_path):
    store = Store(tmp_path / "d")
    a = store.create_sim_job("h1", status="running")
    b = store.create_sim_job("h2", status="queued")
    c = store.create_sim_job("h3", status="done")
    assert SimQueue(store).recover_interrupted() == 2
    assert [store.get_sim_job(x["id"])["status"] for x in (a, b, c)] == ["interrupted", "interrupted", "done"]


def test_progress_fraction_is_monotone_and_bounded():
    prev, seq = 0.0, []
    lines = [{"phase": "setup"}, {"phase": "solve", "timestep": 1000, "max_timesteps": 100000, "energy_db": -1},
             {"phase": "solve", "timestep": 50000, "max_timesteps": 100000, "energy_db": -30},
             {"phase": "solved"}, {"phase": "post", "step": "fields"}, {"phase": "done"}]
    for ln in lines:
        prev = progress_fraction(ln, -40.0, prev)
        seq.append(prev)
    assert seq == sorted(seq) and seq[-1] == 1.0 and max(seq[:3]) < 0.86


async def test_stopping_a_review_while_it_simulates_kills_the_container(env, monkeypatch):
    """The review's Stop cancels the task awaiting the lab; the lab must kill the running container, not leave it burning CPU."""
    from trustpcb.review.lab_real import RealLab
    q, store, sc, job, state = env
    lab = RealLab(store, queue=q)

    async def no_problem():
        return None

    async def prepare(version_id, scenario):
        return None, None, sc, sc.hash(), {}, []

    async def siblings(*args):
        return []

    monkeypatch.setattr(lab, "solver_problem", no_problem)
    monkeypatch.setattr(lab, "_prepare", prepare)
    monkeypatch.setattr(lab, "_sibling_legacy_names", siblings)
    monkeypatch.setattr(lab, "_reuse_geometry_run", lambda *a, **k: (sc.hash(), None))
    monkeypatch.setattr("trustpcb.rf.whatif.validate_ops", lambda *a, **k: None)
    monkeypatch.setattr("trustpcb.rf.scenario.build_job", lambda *a, **k: None)
    task = asyncio.create_task(lab.run_scenario(review_id="r", version_id="v", scenario={}, scenario_hash="h", investigation_id=None, max_seconds=None, on_progress=None))
    for _ in range(100):
        await asyncio.sleep(0.1)
        if (state / "running").exists():
            break
    assert (state / "running").exists(), "the container never started"
    task.cancel()                                           # what Stop does to the review's task
    with pytest.raises(asyncio.CancelledError):
        await task
    assert list(state.glob("kill-trustpcb-ems-*")), "Stop did not kill the container"
    for _ in range(60):                                     # the queue records the job as canceled once the killed container is gone
        if [j["status"] for j in store.list_sim_jobs()] == ["canceled"]:
            break
        await asyncio.sleep(0.1)
    assert [j["status"] for j in store.list_sim_jobs()] == ["canceled"]


async def test_orphaned_containers_of_an_earlier_process_are_killed_at_start(env):
    q, store, sc, job, state = env
    (state / "orphans").write_text("trustpcb-ems-aaaa\ntrustpcb-ems-bbbb\n")
    assert await q.kill_orphans() == 2
    assert {p.name for p in state.glob("kill-*")} == {"kill-trustpcb-ems-aaaa", "kill-trustpcb-ems-bbbb"}
    (state / "orphans").unlink()
    assert await q.kill_orphans() == 0
