"""Board simulations: DC power delivery today, with room for thermal and signal.

``SIMULATORS`` maps a kind to its simulator. A module that is not installed yet is left out, so one
simulator can ship before the others.
"""

from __future__ import annotations

import importlib

from .base import SIM_VERSION, Assumption, Overlay, SimResult, Simulator, load_cached, run_cached, run_dir, run_hash

SIMULATORS: dict[str, Simulator] = {}

_MODULES = ("power", "thermal", "signal")


def register(sim: Simulator) -> Simulator:
    SIMULATORS[sim.kind] = sim
    return sim


def _load() -> None:
    for name in _MODULES:
        qualified = f"{__name__}.{name}"
        try:
            mod = importlib.import_module(qualified)
        except ModuleNotFoundError as exc:
            if exc.name != qualified:  # a missing dependency inside a module is a real error
                raise
            continue
        sim = getattr(mod, "SIMULATOR", None)
        if sim is not None:
            register(sim)


_load()

__all__ = [
    "SIMULATORS", "SIM_VERSION", "Assumption", "Overlay", "SimResult", "Simulator",
    "load_cached", "register", "run_cached", "run_dir", "run_hash",
]
