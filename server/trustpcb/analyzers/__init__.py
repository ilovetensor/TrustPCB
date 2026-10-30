"""Layout analyzers (spec section 9). They measure; they do not judge.

``run_all(board, ctx)`` runs every analyzer on a board. ``run_for_version(version_dir, ctx)``
loads the version's board, runs them and writes ``artifacts/analyzers.json``::

    {candidates, overlays, status, seconds, registrations, context}

Hook for the baseline step (after its own candidates are built)::

    from ..analyzers import run_for_version
    result = await asyncio.to_thread(run_for_version, version_dir, ctx)   # ctx optional
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .base import ANALYZER_NAMES, DEFAULTS, AnalyzerContext, finalize, run_all
from .loader import board_from_dict, load_board

__all__ = ["ANALYZER_NAMES", "DEFAULTS", "AnalyzerContext", "board_from_dict", "finalize", "load_board", "run_all", "run_for_version"]

OUTPUT = "analyzers.json"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def run_for_version(version_dir: str | Path, ctx: AnalyzerContext | None = None, board=None) -> dict[str, Any]:
    """Run all analyzers for one version and write ``artifacts/analyzers.json``. Returns the same dict.

    Safe to call from several processes: an advisory lock serialises runs for one version and the
    file is replaced atomically, so a reader never sees half a file.
    """
    version_dir = Path(version_dir)
    ctx = ctx or AnalyzerContext()
    if ctx.version_dir is None:
        ctx.version_dir = str(version_dir)
    art = version_dir / "artifacts"
    art.mkdir(parents=True, exist_ok=True)
    lock = None
    try:
        import fcntl

        lock = open(art / ".analyzers.lock", "w")
        fcntl.flock(lock, fcntl.LOCK_EX)
    except (ImportError, OSError):  # no locking available: atomic replace still protects readers
        lock = None
    try:
        board = board if board is not None else load_board(version_dir)
        result = run_all(board, ctx)
        result["registrations"] = {"zone_roles": board.zone_roles, "candidate_targets": board.candidate_targets}
        result["context"] = ctx.to_dict() | {"cache": None}
        result["context"].pop("cache", None)
        _atomic_write(art / OUTPUT, json.dumps(result, indent=1))
        return result
    finally:
        if lock is not None:
            lock.close()
