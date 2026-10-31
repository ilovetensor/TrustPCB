#!/usr/bin/env python3
"""A stand-in for the docker CLI in queue tests: ``run`` writes worker-like output, ``kill`` stops it."""
import json, os, sys, time
from pathlib import Path

args = sys.argv[1:]
state = Path(os.environ["FAKE_DOCKER_STATE"])
if args[0] == "ps":          # containers an earlier process left behind: names listed in state/orphans
    (state / "ps_args.json").write_text(json.dumps(args))
    orphans = state / "orphans"
    print(orphans.read_text() if orphans.exists() else "", end="")
    sys.exit(0)
if args[0] == "kill":
    (state / f"kill-{args[1]}").write_text("1")
    sys.exit(0)
name = args[args.index("--name") + 1]
vol = next(a for a in args if a.endswith(":/job")).split(":/job")[0]
job = Path(vol)
mode = os.environ.get("FAKE_DOCKER_MODE", "ok")
(state / "last_cmd.json").write_text(json.dumps(args))
(state / "running").write_text(name)
prog = job / "progress.jsonl"
prog.write_text(json.dumps({"t": 0, "phase": "setup"}) + "\n")
for i in range(1, 6):
    if (state / f"kill-{name}").exists():
        sys.exit(137)
    with open(prog, "a") as f:
        f.write(json.dumps({"t": i, "phase": "solve", "timestep": i * 1000, "max_timesteps": 10000, "energy_db": -8.0 * i}) + "\n")
    time.sleep(0.4 if mode != "hang" else 30)
if mode == "fail":
    (job / "result.json").write_text(json.dumps({"error": "boom", "trust": {"status": "fail", "reasons": ["worker error: boom"]}}))
    sys.exit(1)
n = 60
f = [1.6e9 + i * 1.7e9 / (n - 1) for i in range(n)]
(job / "s11.json").write_text(json.dumps({"freq_hz": f, "s11_db": [-10.0] * n, "s11_re": [0.3] * n, "s11_im": [0.0] * n}))
(job / "result.json").write_text(json.dumps({
    "f_res": 2.4e9, "s11_min_db": -10.5, "bandwidth": {}, "zin_at_res": {"re": 50, "im": 0}, "dmax_dbi": 2.0, "efficiency_est": 0.6,
    "trust": {"energy_decay_db": -41.0, "passive": True, "cells_per_wavelength": 22.0, "status": "ok", "reasons": []},
    "cells": 1000, "timesteps": 5000, "solve_s": 1.0, "nf2ff_s": 0.1}))
with open(prog, "a") as fh:
    fh.write(json.dumps({"t": 9, "phase": "done", "status": "ok"}) + "\n")
