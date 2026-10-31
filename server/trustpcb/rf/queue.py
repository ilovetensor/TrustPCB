"""Simulation queue: one job at a time in Docker, progress, timeout, cancel, cache.

The queue runs ``docker run --rm --cpus N --memory M`` on a job directory built by
``scenario.build_job``. The job directory is ``store.scenario_dir(hash)``, so an identical scenario
finds its finished result there and costs nothing (a cache hit). Progress lines written by the
worker to ``progress.jsonl`` are read while the container runs and handed to a callback. Cancel
kills the container. ``recover_interrupted`` marks jobs left running by an earlier process.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from . import postprocess

IMAGE = "trustpcb-ems:dev"            # TRUSTPCB_EMS_IMAGE overrides it when a queue is made
#: The repository's worker/ folder. It is mounted read-only over /app in the container, so a change to the worker
#: needs no image rebuild. Set TRUSTPCB_WORKER_MOUNT=0 to run the copy baked into the image instead.
WORKER_DIR = Path(__file__).resolve().parents[3] / "worker"
CONTAINER_PREFIX = "trustpcb-ems-"
ProgressCb = Callable[[dict[str, Any]], Awaitable[None] | None]

PHASE_FRACTION = {"setup": 0.02, "solved": 0.86, "ports": 0.87, "nf2ff": 0.9, "fields": 0.94, "current": 0.96, "pulse": 0.98, "done": 1.0}


def progress_fraction(line: dict[str, Any], end_db: float, prev: float = 0.0) -> float:
    """Map one progress.jsonl line to 0..1. Solve takes 0.03 to 0.85, post-processing the rest."""
    ph = line.get("phase")
    if ph == "solve":
        ts, mx = line.get("timestep") or 0, line.get("max_timesteps") or 0
        f_ts = ts / mx if mx else 0.0
        e = line.get("energy_db")
        f_e = min(1.0, max(0.0, e / end_db)) if (e is not None and end_db < 0 and ts > 4000) else 0.0
        return max(prev, 0.03 + 0.82 * max(f_ts, f_e))
    if ph == "post":
        return max(prev, PHASE_FRACTION.get(line.get("step"), 0.9))
    if ph in PHASE_FRACTION:
        return max(prev, PHASE_FRACTION[ph])
    return prev


@dataclass
class SimHandle:
    job_id: str
    scenario_hash: str
    cached: bool
    _fut: asyncio.Future = field(repr=False, default=None)

    async def wait(self) -> dict[str, Any]:
        """The finished summary (see postprocess.finalize) plus ``status`` and ``cached``."""
        return await asyncio.shield(self._fut)


class SimQueue:
    def __init__(self, store, *, image: str | None = None, cpus: int = 6, memory: str = "6g", timeout_s: float = 1500.0,
                 docker: str = "docker", poll_s: float = 1.0, worker_dir: str | Path | None = None):
        self.store = store
        self.image, self.cpus, self.memory = image or os.environ.get("TRUSTPCB_EMS_IMAGE") or IMAGE, cpus, memory
        if worker_dir is None and os.environ.get("TRUSTPCB_WORKER_MOUNT", "1") != "0" and (WORKER_DIR / "run_scenario.py").is_file():
            worker_dir = WORKER_DIR
        self.worker_dir = Path(worker_dir) if worker_dir else None
        self.timeout_s, self.docker, self.poll_s = timeout_s, docker, poll_s
        self._q: asyncio.Queue | None = None
        self._worker: asyncio.Task | None = None
        self._jobs: dict[str, dict[str, Any]] = {}
        self.stats = {"submitted": 0, "cache_hits": 0, "ran": 0}

    # ---- public ------------------------------------------------------------

    def recover_interrupted(self) -> int:
        """Mark jobs left queued or running by an earlier process as interrupted. Call at start-up."""
        n = 0
        for status in ("queued", "running"):
            for j in self.store.list_sim_jobs(status=status):
                self.store.update_sim_job(j["id"], status="interrupted", error="the app restarted while this simulation was unfinished")
                n += 1
        return n

    async def kill_orphans(self) -> int:
        """Kill solver containers an earlier process left running (it was killed before it could stop them). Best effort: no Docker means nothing to do.

        Call at start-up only. Only containers named with this app's prefix are touched.
        """
        if shutil.which(self.docker) is None and not Path(self.docker).is_file():
            return 0
        try:
            listing = await asyncio.create_subprocess_exec(self.docker, "ps", "-q", "--filter", f"name={CONTAINER_PREFIX}", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(listing.communicate(), timeout=10)
            ids = out.decode().split() if listing.returncode == 0 else []
            for container in ids:
                await self._kill(container)
            return len(ids)
        except (asyncio.TimeoutError, OSError):
            return 0

    def cached_summary(self, scenario_hash: str) -> dict[str, Any] | None:
        d = self.store.scenario_dir(scenario_hash)
        res, summ = d / "result.json", d / "summary.json"
        if not (res.exists() and summ.exists()):
            return None
        try:
            r = json.loads(res.read_text())
            lines = (d / "progress.jsonl").read_text().strip().splitlines()
            last = json.loads(lines[-1]) if lines else {}
        except (OSError, ValueError):
            return None
        if "error" in r or last.get("phase") != "done":
            return None
        return json.loads(summ.read_text())

    async def submit(self, scenario_hash: str, job_dir: str | Path, *, review_id: str | None = None, investigation_id: str | None = None,
                     on_progress: ProgressCb | None = None, scenario: dict | None = None) -> SimHandle:
        """Queue a built job. ``job_dir`` must be ``store.scenario_dir(scenario_hash)``. A finished
        result for the same hash is returned at once without running anything."""
        loop = asyncio.get_running_loop()
        self.stats["submitted"] += 1
        hit = self.cached_summary(scenario_hash)
        if hit is not None:
            self.stats["cache_hits"] += 1
            row = self.store.create_sim_job(scenario_hash, review_id, investigation_id, scenario, job_dir, status="done")
            self.store.update_sim_job(row["id"], progress=1.0, trust=json.dumps(hit["trust"]), result=hit)
            fut = loop.create_future()
            fut.set_result({**hit, "status": "done", "cached": True, "job_id": row["id"]})
            return SimHandle(row["id"], scenario_hash, True, fut)
        if self._q is None:
            self._q = asyncio.Queue()
        row = self.store.create_sim_job(scenario_hash, review_id, investigation_id, scenario, job_dir, status="queued")
        fut = loop.create_future()
        self._jobs[row["id"]] = {"hash": scenario_hash, "dir": Path(job_dir), "cb": on_progress, "fut": fut, "cancel": False, "proc": None,
                                 "name": CONTAINER_PREFIX + uuid.uuid4().hex[:10]}
        await self._q.put(row["id"])
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._loop())
        return SimHandle(row["id"], scenario_hash, False, fut)

    async def cancel(self, job_id: str) -> bool:
        """Cancel a queued or running job. A running container is killed."""
        j = self._jobs.get(job_id)
        if j is None or j["fut"].done():
            return False
        j["cancel"] = True
        if j.get("running"):
            await self._kill(j["name"])
        return True

    async def close(self) -> None:
        for jid, j in list(self._jobs.items()):
            if not j["fut"].done():
                await self.cancel(jid)
        if self._worker and not self._worker.done():
            self._worker.cancel()

    # ---- internals ---------------------------------------------------------

    async def _kill(self, name: str) -> None:
        p = await asyncio.create_subprocess_exec(self.docker, "kill", name, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await p.wait()

    async def _loop(self) -> None:
        while True:
            jid = await self._q.get()
            j = self._jobs.get(jid)
            if j is None or j["fut"].done():
                continue
            try:
                out = await self._run(jid, j)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                out = {"status": "failed", "error": f"{type(e).__name__}: {e}"}
                self.store.update_sim_job(jid, status="failed", error=out["error"])
            out.setdefault("job_id", jid)
            out.setdefault("cached", False)
            if not j["fut"].done():
                j["fut"].set_result(out)

    async def _emit(self, j, jid, payload) -> None:
        cb = j["cb"]
        if cb is None:
            return
        r = cb({"job_id": jid, **payload})
        if asyncio.iscoroutine(r):
            await r

    async def _run(self, jid: str, j: dict[str, Any]) -> dict[str, Any]:
        d: Path = j["dir"]
        if j["cancel"]:
            self.store.update_sim_job(jid, status="canceled")
            return {"status": "canceled"}
        sc = json.loads((d / "scenario.json").read_text())
        end_db = float(sc.get("end_criteria", -40.0))
        for stale in ("result.json", "summary.json", "progress.jsonl"):
            (d / stale).unlink(missing_ok=True)
        self.store.update_sim_job(jid, status="running", progress=0.0)
        self.stats["ran"] += 1
        j["running"] = True
        cmd = [self.docker, "run", "--rm", "--name", j["name"], "--cpus", str(self.cpus), "--memory", self.memory, "--network", "none",
               "--pids-limit", "1024", "-v", f"{d}:/job",
               *(["-v", f"{self.worker_dir}:/app:ro"] if self.worker_dir else []), self.image, "python3", "/app/run_scenario.py", "/job"]
        t0 = time.time()
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        j["proc"] = proc
        tail = bytearray()

        async def drain():
            while True:
                b = await proc.stdout.read(4096)
                if not b:
                    return
                tail.extend(b)
                del tail[:-4000]

        drain_task = asyncio.create_task(drain())
        pos, frac, timed_out = 0, 0.0, False
        pf = d / "progress.jsonl"
        try:
            while True:
                try:
                    await asyncio.wait_for(asyncio.shield(proc.wait()), timeout=self.poll_s)
                    break
                except asyncio.TimeoutError:
                    pass
                if time.time() - t0 > self.timeout_s:
                    timed_out = True
                    await self._kill(j["name"])
                    break
                if j["cancel"]:
                    await self._kill(j["name"])
                    break
                frac, pos = await self._read_progress(j, jid, pf, pos, end_db, frac)
            await proc.wait()
            await drain_task
            frac, pos = await self._read_progress(j, jid, pf, pos, end_db, frac)
        finally:
            j["running"] = False
        elapsed = round(time.time() - t0, 1)
        if j["cancel"]:
            self.store.update_sim_job(jid, status="canceled", error="canceled")
            return {"status": "canceled", "elapsed_s": elapsed}
        if timed_out:
            msg = f"simulation exceeded the {self.timeout_s:.0f} s limit and was stopped"
            self.store.update_sim_job(jid, status="timeout", error=msg)
            return {"status": "timeout", "error": msg, "elapsed_s": elapsed}
        try:
            summary = postprocess.finalize(d)
        except FileNotFoundError:
            msg = "the solver produced no result: " + tail.decode("utf8", "replace")[-600:]
            self.store.update_sim_job(jid, status="failed", error=msg)
            return {"status": "failed", "error": msg, "elapsed_s": elapsed}
        summary["elapsed_s"] = elapsed
        if proc.returncode != 0 or "error" in summary:
            msg = summary.get("error") or f"worker exited with code {proc.returncode}"
            self.store.update_sim_job(jid, status="failed", error=msg, trust=json.dumps(summary["trust"]))
            return {**summary, "status": "failed", "error": msg}
        self.store.update_sim_job(jid, status="done", progress=1.0, trust=json.dumps(summary["trust"]), result=summary)
        await self._emit(j, jid, {"phase": "done", "fraction": 1.0, "status": summary["trust"]["status"]})
        return {**summary, "status": "done"}

    async def _read_progress(self, j, jid, pf: Path, pos: int, end_db: float, frac: float):
        if not pf.exists():
            return frac, pos
        with open(pf, "rb") as f:
            f.seek(pos)
            data = f.read()
        if not data:
            return frac, pos
        end = data.rfind(b"\n")
        if end < 0:
            return frac, pos
        last = None
        for ln in data[: end + 1].splitlines():
            try:
                last = json.loads(ln)
            except ValueError:
                continue
            frac = progress_fraction(last, end_db, frac)
        if last is not None:
            self.store.update_sim_job(jid, progress=round(frac, 4))
            await self._emit(j, jid, {**last, "fraction": round(frac, 4)})
        return frac, pos + end + 1
