"""The one place an HTTP client is made, so tests can replace it and nothing else opens a connection."""
from __future__ import annotations

import logging
import re

import httpx

REQUEST_TIMEOUT_S = 20.0
_KEY_IN_URL = re.compile(r'(apikey=)[^&\s"\'<>]+', re.IGNORECASE)


def new_client(timeout: float = REQUEST_TIMEOUT_S, **kwargs) -> httpx.AsyncClient:
    """A fresh client. Tests replace this function; production code calls it for every request batch."""
    return httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10.0), **kwargs)


class _RedactKey(logging.Filter):
    """httpx logs every request URL at INFO, and Mouser takes its key in the query string. Rewrite the line before any handler sees it."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        if 'apikey=' in message.lower():
            record.msg = _KEY_IN_URL.sub(r'\1<redacted>', message)
            record.args = None
        return True


def install_redaction() -> None:
    """Idempotent. Also lifts nothing: it only rewrites records that carry a key."""
    for name in ('httpx', 'httpcore', 'trustpcb'):
        target = logging.getLogger(name)
        if not any(isinstance(f, _RedactKey) for f in target.filters):
            target.addFilter(_RedactKey())


install_redaction()
