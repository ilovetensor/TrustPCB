"""Simulation queue: one job at a time in Docker, shared fairly by all reviews, progress, timeout, cancel, cache.

The queue runs ``docker run --rm --cpus N --memory M`` on a job directory built by
``scenario.build_job``. The job directory is ``store.scenario_dir(hash)``, so an identical scenario
finds its finished result there and costs nothing (a cache hit). Progress lines written by the
worker to ``progress.jsonl`` are read while the container runs and handed to a callback. Cancel
kills the container. ``recover_interrupted`` marks jobs left running by an earlier process.

Several reviews use one queue. The solver still runs one job at a time, so the queue chooses the next job
fairly: the owner (review) served least recently goes first, and jobs of one owner keep their order, so one
review that asks for many runs cannot starve another. A job that waits tells its callback
``{"phase": "waiting", "position": n}`` and, when it gets the solver, ``{"phase": "started"}``; time spent waiting
is the caller's to leave out of any budget. Two reviews that want the identical scenario share one run: the
second attaches to the first one's job, gets the same result and costs no solver time, and a run is stopped
only when nobody is left waiting for it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from ..pcb.env import child_environment
from . import postprocess
from .scenario import default_threads

logger = logging.getLogger("trustpcb")

IMAGE = "trustpcb-ems:dev"            # TRUSTPCB_EMS_IMAGE overrides it when a queue is made
#: The repository's worker/ folder. It is mounted read-only over /app in the container, so a change to the worker
#: needs no image rebuild. Set TRUSTPCB_WORKER_MOUNT=0 to run the copy baked into the image instead.
WORKER_DIR = Path(__file__).resolve().parents[3] / "worker"
CONTAINER_PREFIX = "trustpcb-ems-"
OWNER_LABEL = "trustpcb.data"        # label on every solver container: which data folder started it
ProgressCb = Callable[[dict[str, Any]], Awaitable[None] | None]

PHASE_FRACTION = {"setup": 0.02, "solved": 0.86, "ports": 0.87, "nf2ff": 0.9, "fields": 0.94, "current": 0.96, "pulse": 0.98, "done": 1.0}


def default_cpus() -> int:
    """The ``--cpus`` limit of a solver container: as many CPUs as the solver has threads (``scenario.default_threads``), or
    ``TRUSTPCB_EMS_CPUS``. Measured: the limit costs no speed when it equals the thread count, and a larger one lets the solver's
    waiting threads crowd the rest of the machine."""
    raw = os.environ.get("TRUSTPCB_EMS_CPUS", "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return default_threads()


def docker_environment() -> dict[str, str]:
    """What the docker client may see: the basics, ``DOCKER*`` settings and the ssh agent socket, not the server's secrets.

    The client does not hand its own environment to the container. ``TRUSTPCB_DOCKER_PASS_ENV`` (comma separated names)
    adds variables for a client that needs them, such as a stand-in client in tests.
    """
    extra = [n.strip() for n in os.environ.get("TRUSTPCB_DOCKER_PASS_ENV", "").split(",") if n.strip()]
    return child_environment(extra_names=("SSH_AUTH_SOCK", *extra), extra_prefixes=("DOCKER",))


def container_user() -> str | None:
    """The ``uid:gid`` the solver container runs as: the app's own, so the job folder it writes stays readable and deletable
    by the host on Linux Docker. ``TRUSTPCB_EMS_USER`` overrides it (set it to an empty string to leave the image's user,
    for example with rootless Docker, where the host uid does not map into the container). None where the OS has no uids."""
    override = os.environ.get("TRUSTPCB_EMS_USER")
    if override is not None:
        return override.strip() or None
    if hasattr(os, "getuid"):
        return f"{os.getuid()}:{os.getgid()}"
    return None


def owner_label(data_dir: str | Path | None) -> str:
    """A label value naming the data folder a container belongs to, so one app never stops another app's solver."""
    return hashlib.sha1(str(data_dir or "").encode()).hexdigest()[:16]


def docker_run_command(docker: str, name: str, image: str, job_dir: str | Path, *, cpus: int | float, memory: str,
                       worker_dir: str | Path | None = None, owner: str | None = None) -> list[str]:
    """The ``docker run`` argv for one solver job.

    The scenario comes from an uploaded board, so the container gets the least it can work with: no network, no Linux
    capabilities, no privilege gain, a read-only root file system, a small private /tmp, and a non-root user. The job
    folder is the only writable host path, and the worker code is mounted read-only.
    """
    user = container_user()
    return [docker, "run", "--rm", "--name", name, "--cpus", str(cpus), "--memory", memory, "--network", "none",
            "--pids-limit", "1024", *(["--label", f"{OWNER_LABEL}={owner}"] if owner else []), "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--read-only",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=512m", *(["--user", user] if user else []),
            "-v", f"{job_dir}:/job", *(["-v", f"{worker_dir}:/app:ro"] if worker_dir else []),
            image, "python3", "/app/run_scenario.py", "/job"]


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
    #: set when the job leaves the queue and begins (at once for a cache hit); a caller that wants to bound only the waiting waits on it
    started: asyncio.Event = field(repr=False, default_factory=asyncio.Event)

    async def wait(self) -> dict[str, Any]:
        """The finished summary (see postprocess.finalize) plus ``status`` and ``cached``."""
        return await asyncio.shield(self._fut)


class SimQueue:
    def __init__(self, store, *, image: str | None = None, cpus: int | None = None, memory: str = "6g", timeout_s: float = 1500.0,
                 docker: str = "docker", poll_s: float = 1.0, worker_dir: str | Path | None = None):
        self.store = store
        self.image, self.cpus, self.memory = image or os.environ.get("TRUSTPCB_EMS_IMAGE") or IMAGE, cpus if cpus is not None else default_cpus(), memory
        if worker_dir is None and os.environ.get("TRUSTPCB_WORKER_MOUNT", "1") != "0" and (WORKER_DIR / "run_scenario.py").is_file():
            worker_dir = WORKER_DIR
        self.worker_dir = Path(worker_dir) if worker_dir else None
        self.timeout_s, self.docker, self.poll_s = timeout_s, docker, poll_s
        self.owner = owner_label(getattr(store, "data_dir", None))
        self._pending: list[str] = []                 # job ids waiting for the solver
        self._wake: asyncio.Event | None = None
        self._worker: asyncio.Task | None = None
        self._jobs: dict[str, dict[str, Any]] = {}
        self._inflight: dict[str, str] = {}            # scenario hash -> the job id that is queued or running it
        self._served: dict[str, int] = {}              # owner -> when it last got the solver (for fair choice)
        self._tick = 0
        self._seq = 0
        self.stats = {"submitted": 0, "cache_hits": 0, "ran": 0}
        self.shared = 0                                # submits that attached to a run already queued or running

    # ---- public ------------------------------------------------------------

    def owns_data_dir(self) -> bool:
        """True when this process holds the data folder's lock (see ``datalock``). Anything that acts on work this process did not start
        (marking jobs interrupted, stopping leftover containers) is allowed only then: otherwise it would hit another running app's jobs."""
        from ..datalock import holds
        return holds(getattr(self.store, "data_dir", None))

    def recover_interrupted(self) -> int:
        """Mark jobs left queued or running by an earlier process as interrupted. Call at start-up, by the owner of the data folder only."""
        if not self.owns_data_dir():
            logger.warning("Not marking simulation jobs interrupted: this process does not own the data folder %s.", getattr(self.store, "data_dir", None))
            return 0
        n = 0
        for status in ("queued", "running"):
            for j in self.store.list_sim_jobs(status=status):
                self.store.update_sim_job(j["id"], status="interrupted", error="the app restarted while this simulation was unfinished")
                n += 1
        return n

    async def kill_orphans(self) -> int:
        """Kill solver containers an earlier process left running (it was killed before it could stop them). Best effort: no Docker means nothing to do.

        Call at start-up only. Only containers with this app's name prefix and this data folder's label are touched, so a
        second app (or a test run) on another data folder leaves the first one's solver alone. Two apps on the SAME folder share
        the label, so this also does nothing unless this process owns the folder's lock: a second app can never stop the first one's solver.
        """
        if not self.owns_data_dir():
            logger.warning("Not looking for leftover solver containers: this process does not own the data folder %s.", getattr(self.store, "data_dir", None))
            return 0
        if shutil.which(self.docker) is None and not Path(self.docker).is_file():
            return 0
        try:
            listing = await asyncio.create_subprocess_exec(self.docker, "ps", "-q", "--filter", f"name={CONTAINER_PREFIX}",
                                                           "--filter", f"label={OWNER_LABEL}={self.owner}", env=docker_environment(),
                                                           stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(listing.communicate(), timeout=10)
            ids = out.decode().split() if listing.returncode == 0 else []
            for container in ids:
                await self._kill(container)
            return len(ids)
        except (asyncio.TimeoutError, OSError) as exc:
            logger.warning("Could not look for leftover solver containers: %s", exc)
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
        except (OSError, ValueError) as exc:
            logger.warning("Ignoring the cached result in %s, which cannot be read: %s", d, exc)
            return None
        if "error" in r or last.get("phase") != "done":
            return None
        return json.loads(summ.read_text())

    def in_flight(self, scenario_hash: str) -> bool:
        """True while a run of this scenario is queued or running: its job folder must not be rebuilt or touched."""
        jid = self._inflight.get(scenario_hash)
        j = self._jobs.get(jid) if jid else None
        return bool(j and not j["cancel"] and not j.get("finished"))

    def snapshot(self) -> dict[str, Any]:
        """Who has the solver and who waits, in the order jobs will start: for the runs list and the progress panel."""
        running = next((j for j in self._jobs.values() if j.get("running")), None)
        return {"running": {"job_id": running["id"], "review_id": running["owner_review"]} if running else None,
                "waiting": [{"job_id": jid, "review_id": self._jobs[jid]["owner_review"], "position": i + 1} for i, jid in enumerate(self._order())]}

    async def submit(self, scenario_hash: str, job_dir: str | Path, *, review_id: str | None = None, investigation_id: str | None = None,
                     on_progress: ProgressCb | None = None, scenario: dict | None = None) -> SimHandle:
        """Queue a built job. ``job_dir`` must be ``store.scenario_dir(scenario_hash)``. A finished
        result for the same hash is returned at once without running anything, and a request for a scenario that is
        already queued or running attaches to that run instead of starting a second one."""
        loop = asyncio.get_running_loop()
        self.stats["submitted"] += 1
        hit = self.cached_summary(scenario_hash)
        if hit is not None:
            self.stats["cache_hits"] += 1
            row = self.store.create_sim_job(scenario_hash, review_id, investigation_id, scenario, job_dir, status="done")
            self.store.update_sim_job(row["id"], progress=1.0, trust=json.dumps(hit["trust"]), result=hit)
            fut = loop.create_future()
            fut.set_result({**hit, "status": "done", "cached": True, "job_id": row["id"]})
            handle = SimHandle(row["id"], scenario_hash, True, fut)
            handle.started.set()
            return handle
        row = self.store.create_sim_job(scenario_hash, review_id, investigation_id, scenario, job_dir, status="queued")
        fut = loop.create_future()
        handle = SimHandle(row["id"], scenario_hash, False, fut)
        primary = self._jobs.get(self._inflight.get(scenario_hash, ""))
        if primary is not None and not primary["cancel"] and not primary.get("finished"):
            self.shared += 1
            self._jobs[row["id"]] = {"id": row["id"], "hash": scenario_hash, "dir": Path(job_dir), "cb": on_progress, "fut": fut, "cancel": False, "follows": primary["id"],
                                     "started": handle.started, "owner_review": review_id, "queued_at": time.monotonic()}
            primary["followers"].append(row["id"])
            if primary.get("running"):
                self.store.update_sim_job(row["id"], status="running", progress=primary.get("fraction", 0.0))
                handle.started.set()
                await self._notify(self._jobs[row["id"]], row["id"], {"phase": "started", "fraction": 0.0, "waited_s": 0.0, "shared": True})
            else:
                await self._announce()
            return handle
        self._seq += 1
        self._jobs[row["id"]] = {"id": row["id"], "hash": scenario_hash, "dir": Path(job_dir), "cb": on_progress, "fut": fut, "cancel": False, "proc": None,
                                 "name": CONTAINER_PREFIX + uuid.uuid4().hex[:10], "followers": [], "owner": review_id or row["id"], "owner_review": review_id,
                                 "seq": self._seq, "queued_at": time.monotonic(), "started": handle.started, "announced": None}
        self._inflight[scenario_hash] = row["id"]
        self._pending.append(row["id"])
        if self._wake is None:
            self._wake = asyncio.Event()
        self._wake.set()
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._loop())
        await self._announce()
        return handle

    async def cancel(self, job_id: str) -> bool:
        """Cancel a queued or running job for one caller. The run itself (and its container) stops only when nobody is left waiting for it:
        a review that stops, or runs out of time, must not take the result away from another review that wants the same scenario."""
        j = self._jobs.get(job_id)
        if j is None or j["fut"].done():
            return False
        primary = self._jobs.get(j["follows"]) if j.get("follows") else j
        if j is not primary and job_id in primary["followers"]:
            primary["followers"].remove(job_id)
        self._finish_handle(j, {"status": "canceled"}, status="canceled", error="canceled")
        if j is primary:                                        # the run's own caller leaves; later callers may still want it
            j["cb"] = None
        live = self._live_followers(primary)
        if live:
            primary["owner_review"] = self._jobs[live[0]]["owner_review"]
            if j is primary:
                primary["owner"] = primary["owner_review"] or live[0]
            return True
        if primary["fut"].done() or j is primary:               # nobody is left: stop the run
            primary["cancel"] = True
            if primary["id"] in self._pending:                  # still waiting: it never starts
                self._pending.remove(primary["id"])
                self._finish_job(primary, {"status": "canceled"})
                await self._announce()
            elif primary.get("running"):
                await self._kill(primary["name"])
        return True

    async def close(self) -> None:
        for jid, j in list(self._jobs.items()):
            if not j["fut"].done():
                await self.cancel(jid)
        if self._worker and not self._worker.done():
            self._worker.cancel()

    # ---- internals ---------------------------------------------------------

    async def _kill(self, name: str) -> None:
        p = await asyncio.create_subprocess_exec(self.docker, "kill", name, env=docker_environment(), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await p.wait()

    def _live_followers(self, j: dict[str, Any]) -> list[str]:
        return [f for f in j.get("followers", []) if f in self._jobs and not self._jobs[f]["fut"].done()]

    def _order(self) -> list[str]:
        """Waiting jobs in the order they will start: the owner served longest ago first, then the order of asking."""
        return sorted(self._pending, key=lambda jid: (self._served.get(self._jobs[jid]["owner"], 0), self._jobs[jid]["seq"]))

    def _set(self, j: dict[str, Any], **fields) -> None:
        """Write a job's state to the row of every caller still waiting for it."""
        for jid in [j["id"], *j.get("followers", [])]:
            if jid in self._jobs and not self._jobs[jid]["fut"].done():
                self.store.update_sim_job(jid, **fields)

    def _finish_handle(self, j: dict[str, Any], out: dict[str, Any], *, status: str | None = None, error: str | None = None) -> None:
        if status is not None:
            self.store.update_sim_job(j["id"], status=status, **({"error": error} if error else {}))
        if not j["fut"].done():
            j["fut"].set_result({**out, "job_id": j["id"], "cached": out.get("cached", False)})

    def _finish_job(self, j: dict[str, Any], out: dict[str, Any]) -> None:
        """The run is over: answer its caller and every caller that attached to it."""
        j["finished"] = True
        if self._inflight.get(j["hash"]) == j["id"]:
            del self._inflight[j["hash"]]
        out = {**out}
        if not j["fut"].done():
            j["fut"].set_result({**out, "job_id": j["id"], "cached": out.get("cached", False)})
        for fid in j.get("followers", []):
            f = self._jobs.get(fid)
            if f is not None and not f["fut"].done():
                f["fut"].set_result({**out, "cached": True, "shared": True, "job_id": fid})
        for fid in [j["id"], *j.get("followers", [])]:          # the finished run holds no memory of its callbacks
            if fid in self._jobs:
                self._jobs[fid]["cb"] = None

    async def _notify(self, j: dict[str, Any], jid: str, payload: dict[str, Any]) -> None:
        cb = j.get("cb")
        if cb is None:
            return
        try:
            r = cb({"job_id": jid, **payload})
            if asyncio.iscoroutine(r):
                await r
        except Exception:  # noqa: BLE001 - a caller's progress callback must never stop the solver queue
            logger.warning("A progress callback for simulation job %s failed", jid, exc_info=True)

    async def _broadcast(self, j: dict[str, Any], payload: dict[str, Any]) -> None:
        await self._notify(j, j["id"], payload)
        for fid in self._live_followers(j):
            await self._notify(self._jobs[fid], fid, payload)

    async def _announce(self) -> None:
        """Tell each waiting job its place whenever it changes: {"phase": "waiting", "position": n, "ahead": jobs in front, including the one running}."""
        running = any(x.get("running") for x in self._jobs.values())
        for i, jid in enumerate(self._order()):
            j = self._jobs[jid]
            if j.get("announced") == i or i + (1 if running else 0) == 0:       # nothing in front of it: it is about to start, not waiting
                continue
            j["announced"] = i
            await self._broadcast(j, {"phase": "waiting", "fraction": 0.0, "position": i + 1, "ahead": i + (1 if running else 0)})

    async def _loop(self) -> None:
        while True:
            if not self._pending:
                self._wake.clear()
                await self._wake.wait()
                continue
            jid = self._order()[0]
            self._pending.remove(jid)
            j = self._jobs.get(jid)
            if j is None:
                continue
            if j["cancel"] or (j["fut"].done() and not self._live_followers(j)):      # nobody waits for it any more
                self._finish_job(j, {"status": "canceled"})
                continue
            self._tick += 1
            self._served[j["owner"]] = self._tick
            now = time.monotonic()
            for fid in [jid, *self._live_followers(j)]:
                h = self._jobs[fid]
                h["started"].set()
                if fid != jid:
                    self.store.update_sim_job(fid, status="running", progress=0.0)
                await self._notify(h, fid, {"phase": "started", "fraction": 0.0, "waited_s": round(now - h.get("queued_at", j["queued_at"]), 1)})
            await self._announce()
            try:
                out = await self._run(jid, j)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.exception("Simulation job %s failed", jid)
                out = {"status": "failed", "error": f"{type(e).__name__}: {e}"}
                self._set(j, status="failed", error=out["error"])
            out.setdefault("job_id", jid)
            out.setdefault("cached", False)
            self._finish_job(j, out)
            await self._announce()

    async def _run(self, jid: str, j: dict[str, Any]) -> dict[str, Any]:
        d: Path = j["dir"]
        if j["cancel"]:
            self._set(j, status="canceled")
            return {"status": "canceled"}
        sc = json.loads((d / "scenario.json").read_text())
        end_db = float(sc.get("end_criteria", -40.0))
        for stale in ("result.json", "summary.json", "progress.jsonl"):
            (d / stale).unlink(missing_ok=True)
        self._set(j, status="running", progress=0.0)
        self.stats["ran"] += 1
        j["running"] = True
        cmd = docker_run_command(self.docker, j["name"], self.image, d, cpus=self.cpus, memory=self.memory, worker_dir=self.worker_dir, owner=self.owner)
        t0 = time.time()
        proc = await asyncio.create_subprocess_exec(*cmd, env=docker_environment(), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
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
            self._set(j, status="canceled", error="canceled")
            return {"status": "canceled", "elapsed_s": elapsed}
        if timed_out:
            msg = f"simulation exceeded the {self.timeout_s:.0f} s limit and was stopped"
            self._set(j, status="timeout", error=msg)
            return {"status": "timeout", "error": msg, "elapsed_s": elapsed}
        try:
            summary = postprocess.finalize(d)
        except FileNotFoundError:
            msg = "the solver produced no result: " + tail.decode("utf8", "replace")[-600:]
            self._set(j, status="failed", error=msg)
            return {"status": "failed", "error": msg, "elapsed_s": elapsed}
        summary["elapsed_s"] = elapsed
        if proc.returncode != 0 or "error" in summary:
            msg = summary.get("error") or f"worker exited with code {proc.returncode}"
            self._set(j, status="failed", error=msg, trust=json.dumps(summary["trust"]))
            return {**summary, "status": "failed", "error": msg}
        self._set(j, status="done", progress=1.0, trust=json.dumps(summary["trust"]), result=summary)
        await self._broadcast(j, {"phase": "done", "fraction": 1.0, "status": summary["trust"]["status"]})
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
            j["fraction"] = round(frac, 4)
            self._set(j, progress=round(frac, 4))
            await self._broadcast(j, {**last, "fraction": round(frac, 4)})
        return frac, pos + end + 1
