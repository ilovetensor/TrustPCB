"""Supplier answers cached in the data folder, so a repeated lookup costs no request, and a small daily counter per supplier.

Only reduced listings are stored (fields TrustPCB uses), never a request, a header, a token or a key.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .. import settings_store

FOUND_TTL_S = 7 * 24 * 3600
EMPTY_TTL_S = 24 * 3600
_lock = threading.Lock()


def root() -> Path:
    return settings_store.data_dir() / 'parts'


def _atomic_write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle, temp = tempfile.mkstemp(prefix='.part-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(handle, 'w', encoding='utf-8') as out:
            json.dump(payload, out)
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def _entry(supplier: str, kind: str, query: str) -> Path:
    digest = hashlib.sha256(f'{supplier}\n{kind}\n{query}'.encode()).hexdigest()[:32]
    return root() / 'lookups' / f'{digest}.json'


def get(supplier: str, kind: str, query: str, *, now: float | None = None):
    """The cached listings (a list of dicts, possibly empty) or None when there is no fresh entry."""
    try:
        data = json.loads(_entry(supplier, kind, query).read_text(encoding='utf-8'))
        stamp, listings = float(data['at']), data['listings']
    except (OSError, ValueError, KeyError, TypeError):
        return None
    age = (time.time() if now is None else now) - stamp
    limit = FOUND_TTL_S if listings else EMPTY_TTL_S
    return listings if 0 <= age <= limit and isinstance(listings, list) else None


def put(supplier: str, kind: str, query: str, listings: list[dict], *, now: float | None = None) -> None:
    try:
        _atomic_write(_entry(supplier, kind, query), {'at': time.time() if now is None else now, 'listings': listings})
    except OSError:
        pass                                # a cache that cannot be written only costs a request next time


def take_daily_call(supplier: str, limit: int) -> bool:
    """Count one call against today's allowance (UTC day). False when the allowance is spent."""
    path = root() / 'usage.json'
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    with _lock:
        try:
            usage = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            usage = {}
        if not isinstance(usage, dict) or usage.get('day') != today:
            usage = {'day': today}
        used = int(usage.get(supplier, 0)) if isinstance(usage.get(supplier, 0), int) else 0
        if used >= limit:
            return False
        usage[supplier] = used + 1
        try:
            _atomic_write(path, usage)
        except OSError:
            pass
        return True
