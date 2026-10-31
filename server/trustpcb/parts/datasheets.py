"""Download a datasheet PDF once and keep it in the data folder.

A datasheet is untrusted input from the internet. The fetch refuses anything that is not an https address on a public host (every
redirect hop is checked again), caps the size and the total time, accepts only a body that starts like a PDF, and stores it under
the digest of its bytes with mode 0600. It is never opened here: text is read by `pdftext.py` in a separate, resource-limited
process.
"""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import os
import socket
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from . import cache
from . import http as http_module
from .errors import REQUEST_FAILED, TRANSPORT, UNREADABLE, PartsError

logger = logging.getLogger('trustpcb')

MAX_BYTES = 25 * 1024 * 1024
MAX_REDIRECTS = 5
TOTAL_TIMEOUT_S = 60.0
KEEP_S = 30 * 24 * 3600
PDF_TYPES = ('application/pdf', 'application/x-pdf', 'application/octet-stream', 'binary/octet-stream', 'application/download', 'application/force-download')

BROWSER_HEADERS = {
    'user-agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36',
    'accept': 'application/pdf,application/octet-stream;q=0.9,*/*;q=0.8',
    'accept-language': 'en-US,en;q=0.9',
}
PLAIN_HEADERS = {'user-agent': 'TrustPCB datasheet download', 'accept': 'application/pdf,application/octet-stream;q=0.9'}

_index_lock = threading.Lock()


@dataclass
class DatasheetFile:
    url: str
    sha256: str
    path: Path
    size: int
    cached: bool

    def to_dict(self) -> dict:
        out = asdict(self)
        out['path'] = str(self.path)
        return out


def folder() -> Path:
    return cache.root() / 'datasheets'


async def _resolve(host: str) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return sorted({info[4][0] for info in infos})


async def check_url(url: str) -> str:
    """The URL as https on a public host, or a PartsError. A datasheet link must never reach the local network."""
    parts = urlsplit(url.strip())
    if parts.scheme == 'http':
        parts = parts._replace(scheme='https')
    try:
        port = parts.port
    except ValueError:
        port = -1
    if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password or port not in (None, 443):
        raise PartsError(REQUEST_FAILED, 'The datasheet link is not a plain https address, so it was not fetched.')
    host = parts.hostname
    try:
        addresses = [str(ipaddress.ip_address(host))]
    except ValueError:
        try:
            addresses = await _resolve(host)
        except OSError:
            raise PartsError(TRANSPORT, f'The datasheet host {host} could not be found.', retryable=True) from None
    if not addresses or not all(ipaddress.ip_address(a).is_global for a in addresses):
        raise PartsError(REQUEST_FAILED, f'The datasheet host {host} is not a public internet address, so it was not fetched.')
    return parts.geturl()


def _index_path() -> Path:
    return folder() / 'index.json'


def _read_index() -> dict:
    try:
        data = json.loads(_index_path().read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def known(url: str) -> DatasheetFile | None:
    entry = _read_index().get(url)
    if not isinstance(entry, dict):
        return None
    path = folder() / f"{entry.get('sha256', '')}.pdf"
    if not path.is_file() or time.time() - float(entry.get('at', 0)) > KEEP_S:
        return None
    return DatasheetFile(url=url, sha256=entry['sha256'], path=path, size=int(entry.get('size', path.stat().st_size)), cached=True)


def _store(url: str, body: bytes) -> DatasheetFile:
    digest = hashlib.sha256(body).hexdigest()
    root = folder()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / f'{digest}.pdf'
    if not path.exists():
        handle, temp = tempfile.mkstemp(prefix='.dl-', suffix='.tmp', dir=root)
        try:
            with os.fdopen(handle, 'wb') as out:
                out.write(body)
            os.replace(temp, path)
        except BaseException:
            Path(temp).unlink(missing_ok=True)
            raise
    with _index_lock:
        index = _read_index()
        index[url] = {'sha256': digest, 'at': time.time(), 'size': len(body)}
        cache._atomic_write(_index_path(), index)
    return DatasheetFile(url=url, sha256=digest, path=path, size=len(body), cached=False)


async def _download(client: httpx.AsyncClient, url: str) -> bytes:
    headers = BROWSER_HEADERS
    hops = 0
    while True:
        url = await check_url(url)
        try:
            async with client.stream('GET', url, headers=headers, follow_redirects=False) as response:
                status = response.status_code
                if status in (301, 302, 303, 307, 308):
                    hops += 1
                    target = response.headers.get('location')
                    if hops > MAX_REDIRECTS or not target:
                        raise PartsError(REQUEST_FAILED, 'The datasheet link redirects too many times.')
                    url = urljoin(url, target)
                    continue
                if status in (401, 403) and headers is BROWSER_HEADERS:
                    headers = PLAIN_HEADERS                      # some bot filters turn away a browser-like request and serve a plain one
                    continue
                host = urlsplit(url).hostname
                if status != 200:
                    raise PartsError(REQUEST_FAILED, f'{host} answered the datasheet link with status {status}'
                                     + (', turning away automated downloads.' if status in (401, 403, 429) else '.'), retryable=status >= 500 or status == 429)
                kind = response.headers.get('content-type', '').split(';')[0].strip().lower()
                if kind and kind not in PDF_TYPES:
                    raise PartsError(UNREADABLE, f'{host} answered with {kind}, not a PDF (often a web page in front of the file).')
                declared = response.headers.get('content-length', '')
                if declared.isdigit() and int(declared) > MAX_BYTES:
                    raise PartsError(UNREADABLE, f'The datasheet is larger than {MAX_BYTES // (1024 * 1024)} MB, so it was not downloaded.')
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_BYTES:
                        raise PartsError(UNREADABLE, f'The datasheet is larger than {MAX_BYTES // (1024 * 1024)} MB, so the download was stopped.')
                if b'%PDF-' not in bytes(body[:1024]):
                    raise PartsError(UNREADABLE, f'{host} sent a file that is not a PDF.')
                return bytes(body)
        except httpx.HTTPError:
            raise PartsError(TRANSPORT, 'The datasheet host could not be reached.', retryable=True) from None


async def fetch(url: str) -> DatasheetFile:
    """The PDF for this link: from the data folder when it was fetched in the last 30 days, else downloaded once."""
    start = (url or '').strip()
    if not start:
        raise PartsError(REQUEST_FAILED, 'There is no datasheet link to fetch.')
    have = known(start)
    if have is not None:
        return have
    try:
        async with asyncio.timeout(TOTAL_TIMEOUT_S):
            async with http_module.new_client(timeout=30.0) as client:
                body = await _download(client, start)
    except TimeoutError:
        raise PartsError(TRANSPORT, 'The datasheet download took too long and was stopped.', retryable=True) from None
    return _store(start, body)
