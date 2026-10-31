"""The hook behind GET /api/versions/{id}/extent: what a review of this version can be asked to check."""
from __future__ import annotations

from . import extent as ext
from .service import get_lab


async def extent_options(*, store, version_id: str) -> dict:
    options = await ext.options_for(get_lab(store), version_id, {})
    return {'options': options, 'default': ext.default_extent(options)}


_SIMS: dict[str, dict] = {}


async def sims_available(*, store, version_id: str) -> dict:
    """What each board simulation says about this version: {power: {available, reason}, thermal: ..., signal: ...}. Kept per version (a version never changes). {} when the board cannot be read."""
    if version_id in _SIMS:
        return _SIMS[version_id]
    try:
        status = await ext.sim_status(get_lab(store), version_id, {})
    except Exception:
        return {}
    out = {kind: {'available': bool(info.get('available')), 'reason': info.get('reason') or ''} for kind, info in status.items()}
    if out:
        _SIMS[version_id] = out
    return out
