"""Mouser Search API: part-number search (exact) and keyword search, with the key in the query string as Mouser requires.

The key never appears in a message, an error or a log line: transport errors are re-raised without their chained cause (which
carries the URL) and the request logger is wrapped by `http.install_redaction`. Calls are paced (30 a minute) and counted against a
daily allowance kept in the data folder.
"""
from __future__ import annotations

import asyncio
import collections
import logging
import time
from collections.abc import Awaitable, Callable

import httpx

from . import cache
from . import http as http_module
from .errors import CREDENTIALS_REFUSED, RATE_LIMITED, REQUEST_FAILED, TRANSPORT, UNREADABLE, PartsError
from .model import Listing, clean

logger = logging.getLogger('trustpcb')

PART_URL = 'https://api.mouser.com/api/v1/search/partnumber'
KEYWORD_URL = 'https://api.mouser.com/api/v1/search/keyword'
CALLS_PER_MINUTE = 30
DAILY_LIMIT = 1000
OUTAGE_WAITS_S = (2.0,)
MAX_PARAMS = 60

_sent: collections.deque[float] = collections.deque()      # when the last calls were made (monotonic)


async def _pace(sleep, clock, per_minute: int = CALLS_PER_MINUTE) -> None:
    while True:
        now = clock()
        while _sent and now - _sent[0] >= 60.0:
            _sent.popleft()
        if len(_sent) < per_minute:
            _sent.append(now)
            return
        await sleep(60.0 - (now - _sent[0]))


class MouserClient:
    def __init__(self, api_key: str, *, http: httpx.AsyncClient | None = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep, clock: Callable[[], float] = time.monotonic):
        self._key = api_key
        self._owns = http is None
        self._http = http or http_module.new_client()
        self._sleep, self._clock = sleep, clock

    async def __aenter__(self) -> 'MouserClient':
        return self

    async def __aexit__(self, *exc) -> None:
        if self._owns:
            await self._http.aclose()

    async def by_part_number(self, mpn: str) -> list[Listing]:
        number = (mpn or '').strip()
        if not 3 <= len(number) <= 40 or '|' in number:
            return []                           # Mouser cannot be asked about it; this is not an error
        body = {'SearchByPartRequest': {'mouserPartNumber': number, 'partSearchOptions': 'Exact'}}
        return parse_parts(await self._post(PART_URL, body))

    async def by_keywords(self, keywords: str, records: int = 10) -> list[Listing]:
        words = ' '.join((keywords or '').split())
        if not words or len(words) > 250:
            raise PartsError(REQUEST_FAILED, 'The Mouser search needs 1 to 250 characters.')
        body = {'SearchByKeywordRequest': {'keyword': words, 'records': max(1, min(records, 50)), 'startingRecord': 0}}
        return parse_parts(await self._post(KEYWORD_URL, body))

    async def _post(self, url: str, body: dict) -> dict:
        if not cache.take_daily_call('mouser', DAILY_LIMIT):
            raise PartsError(RATE_LIMITED, f"Mouser's allowance of {DAILY_LIMIT} calls a day is spent, so nothing more was asked today.", retryable=True)
        waits = list(OUTAGE_WAITS_S)
        while True:
            await _pace(self._sleep, self._clock)
            try:
                response = await self._http.post(url, params={'apiKey': self._key}, json=body, headers={'Accept': 'application/json'})
            except (httpx.TimeoutException, httpx.NetworkError):
                if waits:
                    await self._sleep(waits.pop(0))
                    continue
                raise PartsError(TRANSPORT, 'Mouser could not be reached.', retryable=True) from None   # `from None`: the cause carries the URL, and so the key
            if response.status_code >= 500 and waits:
                await self._sleep(waits.pop(0))
                continue
            return _read(response)


def _read(response: httpx.Response) -> dict:
    status = response.status_code
    if status == 429:
        raise PartsError(RATE_LIMITED, 'Mouser is rate-limiting this app, so nothing was asked.', retryable=True)
    if status in {401, 403}:
        raise PartsError(CREDENTIALS_REFUSED, f'Mouser refused the API key (status {status}). Check it in Settings.')
    if status >= 400:
        raise PartsError(REQUEST_FAILED, f'Mouser refused the call with status {status}.', retryable=status >= 500)
    try:
        payload = response.json()
    except ValueError:
        raise PartsError(UNREADABLE, 'Mouser answered with no JSON.') from None
    if not isinstance(payload, dict):
        raise PartsError(UNREADABLE, 'Mouser answered with no object.')
    results = payload.get('SearchResults')
    if not (isinstance(results, dict) and isinstance(results.get('Parts'), list)):
        errors = ' '.join(f"{e.get('PropertyName', '')} {e.get('ResourceKey', '')} {e.get('Code', '')} {e.get('Message', '')}".lower()
                          for e in payload.get('Errors') or () if isinstance(e, dict))
        if 'api key' in errors or 'invalididentifier' in errors:
            raise PartsError(CREDENTIALS_REFUSED, 'Mouser does not accept the API key. Check it in Settings.')
        if any(word in errors for word in ('too many', 'toomany', 'rate limit', 'quota', 'exceeded')):
            raise PartsError(RATE_LIMITED, 'Mouser is limiting this app, so nothing was asked.', retryable=True)
        raise PartsError(UNREADABLE, 'Mouser answered outside its expected shape.')
    return payload


def _https(value: object) -> str | None:
    text = clean(value, 1024)
    if text and text.startswith('//'):
        text = 'https:' + text
    if text and text.startswith('http://'):
        text = 'https://' + text[len('http://'):]
    return text if text and text.startswith('https://') else None


def parse_parts(payload: dict) -> list[Listing]:
    found: list[Listing] = []
    for part in (payload.get('SearchResults') or {}).get('Parts') or ():
        if not isinstance(part, dict):
            continue
        mpn = clean(part.get('ManufacturerPartNumber'), 160)
        if mpn is None:
            continue
        params: dict[str, str] = {}
        for item in part.get('ProductAttributes') or ():
            if isinstance(item, dict) and len(params) < MAX_PARAMS:
                key, val = clean(item.get('AttributeName'), 120), clean(item.get('AttributeValue'), 240)
                if key and val:
                    params[key] = val
        found.append(Listing(
            source='mouser', mpn=mpn, maker=clean(part.get('Manufacturer'), 160), description=clean(part.get('Description'), 500),
            datasheet_url=_https(part.get('DataSheetUrl')), params=params, product_url=_https(part.get('ProductDetailUrl')),
            supplier_pn=clean(part.get('MouserPartNumber'), 80), category=clean(part.get('Category'), 200),
            lifecycle=clean(part.get('LifecycleStatus'), 80)))
    return found
