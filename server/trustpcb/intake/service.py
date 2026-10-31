"""Upload handling for the API: unpack one version, run its baseline, record its artifacts."""
from __future__ import annotations

import asyncio
import mimetypes
import os
from pathlib import Path
from typing import Any

from ..analyzers import run_for_version
from ..pcb.baseline import run_baseline
from ..pcb.model import parse_board
from . import detect, stackup, upload

mimetypes.add_type('model/gltf-binary', '.glb')
mimetypes.add_type('image/svg+xml', '.svg')


def _record_artifacts(store, version_id: str, artifacts: Path) -> None:
    for path in sorted(artifacts.rglob('*')):
        if path.is_file():
            name = path.relative_to(artifacts).as_posix()
            store.add_artifact('version', version_id, name, path, mimetypes.guess_type(path.name)[0] or 'application/octet-stream')


def _limit(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, '') or default)
    except ValueError:
        return default


async def ingest(*, store, version: dict, upload_path, filename: str) -> dict[str, Any]:
    """Validate the upload, then run Phase 1. A failed KiCad step is reported, not fatal.

    A board file larger than TRUSTPCB_MAX_PCB_MB (default 150) is refused before parsing, and parsing is given TRUSTPCB_PARSE_TIMEOUT_S (default 240)
    seconds, so a giant or hostile file ends in a clear message instead of an upload that never returns.
    """
    version_dir = store.version_dir(version['id'])
    result = await asyncio.to_thread(upload.ingest, upload_path, version_dir)
    if result.pcb is None:
        raise ValueError('The upload contains no .kicad_pcb file')
    max_mb = _limit('TRUSTPCB_MAX_PCB_MB', 150)
    size_mb = Path(result.pcb).stat().st_size / (1024 * 1024)
    if size_mb > max_mb:
        raise ValueError(f'The board file {Path(result.pcb).name} is {size_mb:.0f} MB, which is more than the {max_mb:g} MB TrustPCB reads. Set TRUSTPCB_MAX_PCB_MB to raise the limit.')
    seconds = _limit('TRUSTPCB_PARSE_TIMEOUT_S', 240)
    try:
        board = await asyncio.wait_for(asyncio.to_thread(parse_board, result.pcb, version['id']), timeout=seconds)
    except asyncio.TimeoutError:
        raise ValueError(f'Reading the board file took longer than {seconds:g} seconds, so it was abandoned. The file may be too large or damaged. '
                         'Set TRUSTPCB_PARSE_TIMEOUT_S to wait longer.') from None
    except (ValueError, IndexError, KeyError, TypeError, RecursionError) as exc:
        raise ValueError(f'{Path(result.pcb).name} is not a KiCad board TrustPCB can read ({type(exc).__name__}: {str(exc)[:160]}). Upload a .kicad_pcb file saved by KiCad 9 or newer.') from exc
    baseline = await run_baseline(version_dir)
    await asyncio.to_thread(run_for_version, version_dir)
    found = detect.detect(board, result.sch)
    _record_artifacts(store, version['id'], version_dir / 'artifacts')
    return {
        'status': 'ready',
        'stackup': {**stackup.confirmation(board), 'warnings': [*result.warnings, *baseline.get('warnings', [])]},
        'detection': found.to_dict(),
    }
