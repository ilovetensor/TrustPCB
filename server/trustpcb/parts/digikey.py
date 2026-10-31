"""DigiKey Product Information v4: OAuth client-credentials token (cached in memory until shortly before it expires) and keyword search.

The keyword search takes a manufacturer part number or words and answers up to ten products, with DigiKey's own `ExactMatches`
listed apart. Rate limits are read from every answer (`X-RateLimit-Remaining`, `Retry-After`); a 429 with a short wait is waited out
twice, anything longer is reported. An outage (5xx, timeout) is retried with short waits out of one budget shared by the token
request and the search. No error text carries a key, a token or a response body.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import Awaitable, Callable
from urllib.parse import parse_qs, urlsplit

import httpx

from . import http as http_module
from .errors import CREDENTIALS_REFUSED, RATE_LIMITED, REQUEST_FAILED, TRANSPORT, UNREADABLE, PartsError
from .model import Listing, clean

logger = logging.getLogger('trustpcb')

TOKEN_URL = 'https://api.digikey.com/v1/oauth2/token'
SEARCH_URL = 'https://api.digikey.com/products/v4/search/keyword'
SEARCH_LIMIT = 10
TOKEN_MARGIN_S = 60.0
OUTAGE_WAITS_S = (1.5, 4.0)
RATE_LIMIT_WAITS_S = (2.0, 6.0)
LONGEST_RETRY_AFTER_S = 15.0
EXHAUSTED_FOR_S = 3600.0
MAX_PARAMS = 60

_tokens: dict[str, tuple[str, float]] = {}          # credential digest -> (token, good until)
_remaining: dict[str, tuple[int, float]] = {}       # credential digest -> (calls left today as last answered, when)


def _digest(client_id: str, secret: str) -> str:
    return hashlib.sha256(f'{client_id}\0{secret}'.encode()).hexdigest()


def forget_tokens() -> None:
    _tokens.clear()
    _remaining.clear()


class DigiKeyClient:
    def __init__(self, client_id: str, client_secret: str, *, http: httpx.AsyncClient | None = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep, clock: Callable[[], float] = time.monotonic):
        self._id, self._secret = client_id, client_secret
        self._digest = _digest(client_id, client_secret)
        self._owns = http is None
        self._http = http or http_module.new_client()
        self._sleep, self._clock = sleep, clock
        self._outages = 0

    async def __aenter__(self) -> 'DigiKeyClient':
        return self

    async def __aexit__(self, *exc) -> None:
        if self._owns:
            await self._http.aclose()

    async def search(self, keywords: str, limit: int = SEARCH_LIMIT) -> list[Listing]:
        words = ' '.join((keywords or '').split())
        if not words or len(words) > 250:
            raise PartsError(REQUEST_FAILED, 'The DigiKey search needs 1 to 250 characters.')
        answer = await self._send('POST', SEARCH_URL, json={'Keywords': words, 'Limit': limit})
        return parse_answer(answer)

    # ---- transport ----

    async def _outage(self, said: str) -> bool:
        if self._outages >= len(OUTAGE_WAITS_S):
            return False
        delay = OUTAGE_WAITS_S[self._outages]
        self._outages += 1
        logger.warning('DigiKey %s; asking again in %.1f s', said, delay)
        await self._sleep(delay)
        return True

    async def _send(self, method: str, url: str, **kwargs) -> dict:
        known = _remaining.get(self._digest)
        if known and known[0] <= 0 and self._clock() - known[1] < EXHAUSTED_FOR_S:
            raise PartsError(RATE_LIMITED, "DigiKey's allowance for today is spent, so nothing more was asked.", retryable=True)
        renewed, waits = False, 0
        while True:
            token = await self._bearer()
            try:
                response = await self._http.request(method, url, headers={
                    'X-DIGIKEY-Client-Id': self._id, 'Authorization': f'Bearer {token}', 'Accept': 'application/json'}, **kwargs)
            except (httpx.TimeoutException, httpx.NetworkError):
                if await self._outage('could not be reached'):
                    continue
                raise PartsError(TRANSPORT, 'DigiKey could not be reached.', retryable=True) from None
            self._note_limit(response.headers)
            status = response.status_code
            if status == 401 and not renewed:
                _tokens.pop(self._digest, None)
                renewed = True
                continue
            if status == 429:
                delay = _retry_delay(response.headers, waits)
                if delay is None:
                    raise PartsError(RATE_LIMITED, 'DigiKey is rate-limiting this app, so nothing was asked.', retryable=True)
                waits += 1
                await self._sleep(delay)
                continue
            if (status >= 500 or status in {408, 409}) and await self._outage(f'answered {status}'):
                continue
            if status in {401, 403}:
                raise PartsError(CREDENTIALS_REFUSED, f'DigiKey refused the call (status {status}). Check the DigiKey client ID and secret in Settings.')
            if status >= 400:
                raise PartsError(REQUEST_FAILED, f'DigiKey refused the call with status {status}.', retryable=status >= 500)
            return _object(response)

    async def _bearer(self) -> str:
        cached = _tokens.get(self._digest)
        if cached and self._clock() < cached[1]:
            return cached[0]
        asked = self._clock()
        while True:
            try:
                response = await self._http.post(TOKEN_URL, data={'client_id': self._id, 'client_secret': self._secret, 'grant_type': 'client_credentials'})
            except (httpx.TimeoutException, httpx.NetworkError):
                if await self._outage('token endpoint could not be reached'):
                    continue
                raise PartsError(TRANSPORT, 'DigiKey could not be reached.', retryable=True) from None
            if 400 <= response.status_code < 500:
                raise PartsError(CREDENTIALS_REFUSED, f'DigiKey refused the client ID and secret (status {response.status_code}). Check them in Settings.')
            if response.status_code >= 400:
                if await self._outage(f'token endpoint answered {response.status_code}'):
                    continue
                raise PartsError(REQUEST_FAILED, f'DigiKey token request failed with status {response.status_code}.', retryable=True)
            break
        payload = _object(response)
        token, lifetime = payload.get('access_token'), payload.get('expires_in')
        if not isinstance(token, str) or not token or isinstance(lifetime, bool) or not isinstance(lifetime, (int, float)) or lifetime <= 0:
            raise PartsError(UNREADABLE, "DigiKey's token answer is not in the expected shape.")
        _tokens[self._digest] = (token, asked + float(lifetime) - TOKEN_MARGIN_S)
        return token

    def _note_limit(self, headers: httpx.Headers) -> None:
        value = (headers.get('X-RateLimit-Remaining') or '').strip()
        if value.isdigit():
            _remaining[self._digest] = (int(value), self._clock())


def _retry_delay(headers: httpx.Headers, waits: int) -> float | None:
    if waits >= len(RATE_LIMIT_WAITS_S):
        return None
    stated = headers.get('Retry-After')
    if stated is None:
        return RATE_LIMIT_WAITS_S[waits]
    try:
        seconds = float(stated.strip())
    except ValueError:
        return None
    return seconds if 0 <= seconds <= LONGEST_RETRY_AFTER_S else None


def _object(response: httpx.Response) -> dict:
    try:
        payload = response.json()
    except ValueError:
        raise PartsError(UNREADABLE, 'DigiKey answered with no JSON.') from None
    if not isinstance(payload, dict):
        raise PartsError(UNREADABLE, 'DigiKey answered with no object.')
    return payload


# ---- reading an answer ----

def datasheet_link(value: object) -> str | None:
    """The datasheet URL as https. DigiKey writes `//host/path`; TI wraps its link in a supplier-info page that names the real one."""
    text = clean(value, 1024)
    if text is None:
        return None
    if text.startswith('//'):
        text = 'https:' + text
    if text.startswith('http://'):
        text = 'https://' + text[len('http://'):]
    if not text.startswith('https://'):
        return None
    parts = urlsplit(text)
    if parts.hostname == 'www.ti.com' and parts.path == '/general/docs/suppproductinfo.tsp':
        target = (parse_qs(parts.query).get('gotoUrl') or [''])[0].replace('http://', 'https://', 1)
        inner = urlsplit(target)
        return target if inner.scheme == 'https' and inner.hostname == 'www.ti.com' else None
    return text


def _name(value: object) -> str | None:
    return clean(value.get('Name'), 160) if isinstance(value, dict) else None


def parse_product(product: dict, exact: bool) -> Listing | None:
    mpn = clean(product.get('ManufacturerProductNumber'), 160)
    if mpn is None:
        return None
    described = product.get('Description') if isinstance(product.get('Description'), dict) else {}
    params: dict[str, str] = {}
    for item in product.get('Parameters') or ():
        if isinstance(item, dict) and len(params) < MAX_PARAMS:
            key, val = clean(item.get('ParameterText'), 120), clean(item.get('ValueText'), 240)
            if key and val:
                params[key] = val
    status = product.get('ProductStatus')
    variations = [v for v in product.get('ProductVariations') or () if isinstance(v, dict)]
    return Listing(
        source='digikey', mpn=mpn, maker=_name(product.get('Manufacturer')),
        description=clean(described.get('DetailedDescription'), 500) or clean(described.get('ProductDescription'), 500),
        datasheet_url=datasheet_link(product.get('DatasheetUrl')), params=params,
        product_url=clean(product.get('ProductUrl'), 500),
        supplier_pn=clean(variations[0].get('DigiKeyProductNumber'), 80) if variations else None,
        category=_name(product.get('Category')),
        lifecycle=_name(status) if isinstance(status, dict) else clean(status, 80), exact=exact)


def parse_answer(answer: dict) -> list[Listing]:
    exact, products = answer.get('ExactMatches'), answer.get('Products')
    if not isinstance(exact, list) or not isinstance(products, list):
        raise PartsError(UNREADABLE, 'The DigiKey search answered outside its expected shape.')
    seen: set[str] = set()
    found: list[Listing] = []
    for index, (product, flag) in enumerate([(p, True) for p in exact] + [(p, False) for p in products]):
        if not isinstance(product, dict):
            continue
        listing = parse_product(product, flag)
        if listing is None:
            continue
        identity = listing.product_url or f'#{index}'
        if identity in seen:
            continue
        seen.add(identity)
        found.append(listing)
    return found
