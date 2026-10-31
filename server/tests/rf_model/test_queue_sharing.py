"""Several reviews share one solver queue: fair order, one run for an identical scenario, waiting is not charged, a hard limit on waiting.

Everything runs against the stand-in docker client (fake_docker.py); no solver is started.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from trustpcb.datalock import DataLock
from trustpcb.rf import scenario as S
from trustpcb.rf.queue import SimQueue
from trustpcb.storage import Store

FAKE = Path(__file__).parent / "fake_docker.py"


@pytest.fixture()
def lab_env(tmp_path, monkeypatch, nrf_board, nrf_setup, request):
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("FAKE_DOCKER_STATE", str(state))
    monkeypatch.setenv("FAKE_DOCKER_STEP", "0.1")
    monkeypatch.setenv("TRUSTPCB_DOCKER_PASS_ENV", "FAKE_DOCKER_STATE,FAKE_DOCKER_MODE,FAKE_DOCKER_STEP")
    wrapper = tmp_path / "docker"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE} \"$@\"\n")
    wrapper.chmod(0o755)
    store = Store(tmp_path / "data")
    request.addfinalizer(DataLock(store.data_dir).acquire().release)
    q = SimQueue(store, docker=str(wrapper), poll_s=0.05, timeout_s=30)
    sc = S.scenario_for(nrf_board, nrf_setup, "screen")
    source = store.scenario_dir(sc.hash())
    S.build_job(nrf_board, nrf_setup, sc, source, debug_png=False)

    def job(name: str) -> Path:
        """A job folder of its own (same model, another name), like another scenario."""
        folder = store.scenario_dir(name)
        (folder / "scenario.json").write_text((source / "scenario.json").read_text())
        (folder / "meta.json").write_text((source / "meta.json").read_text().replace(sc.hash(), name))
        return folder

    return q, store, state, job


def started_order(state: Path) -> list[str]:
    return (state / "started.log").read_text().split()


async def test_reviews_share_the_solver_fairly(lab_env):
    """Review A asks for three runs before review B asks for two. B's first run goes before A's second, and so on:
    one review that asks for a lot cannot keep another waiting at the back."""
    q, store, state, job = lab_env
    handles = []
    for owner, names in (("A", ["a1", "a2", "a3"]), ("B", ["b1", "b2"])):
        for name in names:
            handles.append(await q.submit(name, job(name), review_id=owner))
    outs = await asyncio.gather(*(h.wait() for h in handles))
    assert [o["status"] for o in outs] == ["done"] * 5
    assert started_order(state) == ["a1", "b1", "a2", "b2", "a3"]
    assert q.stats["ran"] == 5


async def test_a_waiting_job_is_told_its_place_and_when_it_starts(lab_env):
    q, store, state, job = lab_env
    seen: dict[str, list[dict]] = {"A": [], "B": []}
    a = await q.submit("a1", job("a1"), review_id="A", on_progress=lambda p: seen["A"].append(p))
    await asyncio.sleep(0.05)                                       # the solver takes A's job
    b = await q.submit("b1", job("b1"), review_id="B", on_progress=lambda p: seen["B"].append(p))
    assert q.snapshot()["waiting"] == [{"job_id": b.job_id, "review_id": "B", "position": 1}]
    await asyncio.gather(a.wait(), b.wait())
    phases = [p["phase"] for p in seen["B"]]
    assert phases[0] == "waiting" and seen["B"][0]["position"] == 1 and seen["B"][0]["ahead"] == 1
    assert "started" in phases and phases.index("started") > 0 and phases.count("waiting") == 1
    assert "waiting" not in [p["phase"] for p in seen["A"]] and seen["A"][0]["phase"] == "started"     # the first job never waited
    assert q.snapshot() == {"running": None, "waiting": []}


async def test_identical_scenario_is_run_once_and_shared(lab_env):
    q, store, state, job = lab_env
    folder = job("same")
    a = await q.submit("same", folder, review_id="A")
    b = await q.submit("same", folder, review_id="B")               # asked while A's run is queued or running
    assert q.in_flight("same")
    ra, rb = await asyncio.gather(a.wait(), b.wait())
    assert ra["status"] == "done" and rb["status"] == "done"
    assert not ra["cached"] and rb["cached"] and rb["shared"]       # the second review used no solver time of its own
    assert rb["job_id"] != ra["job_id"] and rb["trust"] == ra["trust"]
    assert q.stats["ran"] == 1 and q.shared == 1
    assert started_order(state) == ["same"]
    assert [store.get_sim_job(h.job_id)["status"] for h in (a, b)] == ["done", "done"]
    assert not q.in_flight("same")
    c = await q.submit("same", folder, review_id="C")               # afterwards it is an ordinary cache hit
    assert c.cached and q.stats["ran"] == 1


async def test_a_shared_run_survives_one_review_leaving_and_stops_when_all_leave(lab_env):
    q, store, state, job = lab_env
    folder = job("same")
    a = await q.submit("same", folder, review_id="A")
    b = await q.submit("same", folder, review_id="B")
    for _ in range(100):
        if (state / "running").exists():
            break
        await asyncio.sleep(0.05)
    assert await q.cancel(a.job_id)                                 # A is stopped or out of time: B still wants the result
    assert (await a.wait())["status"] == "canceled"
    assert (await b.wait())["status"] == "done" and not list(state.glob("kill-*"))
    assert store.get_sim_job(a.job_id)["status"] == "canceled" and store.get_sim_job(b.job_id)["status"] == "done"

    other = job("other")
    c = await q.submit("other", other, review_id="C")
    d = await q.submit("other", other, review_id="D")
    for _ in range(100):
        if "other" in (state / "started.log").read_text():
            break
        await asyncio.sleep(0.05)
    await q.cancel(c.job_id)
    assert not list(state.glob("kill-trustpcb-ems-*"))              # D is still waiting for it
    await q.cancel(d.job_id)
    assert list(state.glob("kill-trustpcb-ems-*"))                  # nobody is left: the container is stopped
    assert (await d.wait())["status"] == "canceled"


async def test_a_waiting_job_that_is_cancelled_never_starts(lab_env):
    q, store, state, job = lab_env
    a = await q.submit("a1", job("a1"), review_id="A")
    b = await q.submit("b1", job("b1"), review_id="B")
    assert await q.cancel(b.job_id)
    assert (await b.wait())["status"] == "canceled" and (await a.wait())["status"] == "done"
    assert started_order(state) == ["a1"] and store.get_sim_job(b.job_id)["status"] == "canceled"


async def test_the_lab_builds_an_identical_scenario_once_and_does_not_charge_waiting(lab_env, monkeypatch):
    """Through RealLab: two reviews ask for the same scenario at once (one folder build, one run) while a third waits behind them;
    its time limit counts from when its run starts, not from when it asked."""
    from trustpcb.review.lab_real import RealLab

    q, store, state, job = lab_env
    monkeypatch.setenv("FAKE_DOCKER_STEP", "0.3")
    lab = RealLab(store, queue=q)
    builds: list[str] = []
    sc_for = {"x": job("x"), "y": job("y")}

    async def no_problem():
        return None

    async def prepare(version_id, scenario):
        name = scenario["name"]
        return None, None, type("Sc", (), {"quality": "screen", "whatif_ops": [], "to_dict": lambda self: {}})(), name, {}, []

    async def siblings(*args):
        return []

    def build(board, setup, sc, folder):
        builds.append(folder.name)

    monkeypatch.setattr(lab, "solver_problem", no_problem)
    monkeypatch.setattr(lab, "_prepare", prepare)
    monkeypatch.setattr(lab, "_sibling_legacy_names", siblings)
    monkeypatch.setattr(lab, "_reuse_geometry_run", lambda sc, setup, job_hash, *a, **k: (job_hash, None))
    monkeypatch.setattr("trustpcb.rf.whatif.validate_ops", lambda *a, **k: None)
    monkeypatch.setattr("trustpcb.rf.scenario.build_job", build)

    def run(review, name, max_seconds=None, feed=None):
        return lab.run_scenario(review_id=review, version_id="v", scenario={"name": name}, scenario_hash=name, investigation_id=None, max_seconds=max_seconds, on_progress=feed)

    first = asyncio.create_task(run("A", "x"))
    await asyncio.sleep(0.05)
    second = asyncio.create_task(run("B", "x"))
    events: list[dict] = []

    async def feed(update):
        events.append(update)

    # Y waits behind X (about 1.6 s of solver time). Its own limit of 3 s is enough for its run (about 1.6 s) but not for waiting plus running.
    third = asyncio.create_task(run("C", "y", max_seconds=3.0, feed=feed))
    ra, rb, rc = await asyncio.gather(first, second, third)
    assert ra["status"] == rb["status"] == "done" and rb["cached"] and not ra["cached"]
    assert builds == ["x", "y"] and q.stats["ran"] == 2 and q.shared == 1
    assert rc["status"] == "done", rc.get("error")
    assert [e["phase"] for e in events if e.get("phase") in ("waiting", "started")] == ["waiting", "started"]


async def test_waiting_longer_than_the_hard_limit_drops_the_run(lab_env, monkeypatch):
    from trustpcb.review.lab_real import RealLab

    q, store, state, job = lab_env
    monkeypatch.setenv("FAKE_DOCKER_STEP", "0.4")
    lab = RealLab(store, queue=q)
    lab.max_queue_wait_s = 0.3
    job("slow"), job("late")

    async def no_problem():
        return None

    async def prepare(version_id, scenario):
        return None, None, type("Sc", (), {"quality": "screen", "whatif_ops": [], "to_dict": lambda self: {}})(), scenario["name"], {}, []

    async def siblings(*args):
        return []

    monkeypatch.setattr(lab, "solver_problem", no_problem)
    monkeypatch.setattr(lab, "_prepare", prepare)
    monkeypatch.setattr(lab, "_sibling_legacy_names", siblings)
    monkeypatch.setattr(lab, "_reuse_geometry_run", lambda sc, setup, job_hash, *a, **k: (job_hash, None))
    monkeypatch.setattr("trustpcb.rf.whatif.validate_ops", lambda *a, **k: None)
    monkeypatch.setattr("trustpcb.rf.scenario.build_job", lambda *a, **k: None)
    first = asyncio.create_task(lab.run_scenario(review_id="A", version_id="v", scenario={"name": "slow"}, scenario_hash="slow", investigation_id=None, max_seconds=None, on_progress=None))
    await asyncio.sleep(0.1)
    late = await lab.run_scenario(review_id="B", version_id="v", scenario={"name": "late"}, scenario_hash="late", investigation_id=None, max_seconds=None, on_progress=None)
    assert late["status"] == "timeout" and late["solve_s"] == 0 and late["waited_out"] and "busy" in late["error"]
    assert (await first)["status"] == "done"
    assert started_order(state) == ["slow"] and store.get_sim_job(late["job_id"])["status"] == "canceled"


async def test_the_one_solver_at_a_time_rule_still_holds(lab_env):
    q, store, state, job = lab_env
    handles = [await q.submit(n, job(n), review_id=n) for n in ("p", "q", "r")]
    running = 0
    for _ in range(40):
        running = max(running, sum(1 for j in q._jobs.values() if j.get("running")))
        await asyncio.sleep(0.05)
    await asyncio.gather(*(h.wait() for h in handles))
    assert running == 1 and q.stats["ran"] == 3
