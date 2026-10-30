"""Upload handling for the API: unpack one version, run its baseline, record its artifacts."""
from __future__ import annotations

import asyncio
import mimetypes
from pathlib import Path
from typing import Any

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


async def ingest(*, store, version: dict, upload_path, filename: str) -> dict[str, Any]:
    """Validate the upload, then run Phase 1. A failed KiCad step is reported, not fatal."""
    version_dir = store.version_dir(version['id'])
    result = await asyncio.to_thread(upload.ingest, upload_path, version_dir)
    if result.pcb is None:
        raise ValueError('The upload contains no .kicad_pcb file')
    board = await asyncio.to_thread(parse_board, result.pcb, version['id'])
    baseline = await run_baseline(version_dir)
    found = detect.detect(board, result.sch)
    _record_artifacts(store, version['id'], version_dir / 'artifacts')
    return {
        'status': 'ready',
        'stackup': {**stackup.confirmation(board), 'warnings': [*result.warnings, *baseline.get('warnings', [])]},
        'detection': found.to_dict(),
    }
