"""Part lookup across DigiKey and Mouser, merged into one answer.

    await lookup(mpn='USBLC6-2SC6')             exact number: 'found', or 'ambiguous' with candidates, or 'not_found'
    await lookup(value='100nF', footprint=...)   value and footprint alone: always candidates, never a pick

A part is only 'found' when a supplier lists exactly that manufacturer part number (letters and digits compared, case and
punctuation ignored) and every exact listing is from one maker. Anything else returns the candidates and says why. Answers are
cached in the data folder (seven days; an empty answer one day), so a repeated lookup costs no request.
"""
from __future__ import annotations

import re

from .. import settings_store
from . import cache
from . import http as http_module
from .digikey import DigiKeyClient
from .errors import NOT_CONFIGURED, PartsError
from .model import Candidate, Listing, LookupResult, PartInfo, clean, folded, normal
from .mouser import MouserClient

MAX_CANDIDATES = 10
_PACKAGE_KEYS = ('Package / Case', 'Supplier Device Package', 'Package', 'Case Code - Imperial', 'Case/Package')
_SIZE = re.compile(r'(?<![0-9])(0201|0402|0603|0805|1206|1210|1812|2010|2512)(?![0-9])')
_PACKAGE = re.compile(r'\b(SOT-?\d+(?:-\d+)?|SOIC-?\d+|TSSOP-?\d+|QFN-?\d+|DFN-?\d+|LQFP-?\d+|BGA-?\d+|SOD-?\d+|SC-?\d+|TO-?\d+\w*)\b', re.I)


def digikey_credentials() -> tuple[str, str] | None:
    ident, secret = settings_store.get_secret('DIGIKEY_CLIENT_ID'), settings_store.get_secret('DIGIKEY_CLIENT_SECRET')
    return (ident, secret) if ident and secret else None


def mouser_key() -> str | None:
    return settings_store.get_secret('MOUSER_API_KEY')


def configured() -> dict[str, bool]:
    return {'digikey': digikey_credentials() is not None, 'mouser': mouser_key() is not None}


def available() -> bool:
    """True when at least one supplier has a key. Everything else in TrustPCB works without."""
    return any(configured().values())


def footprint_words(footprint: str | None) -> str:
    """Search words from a KiCad footprint name: `Capacitor_SMD:C_0402_1005Metric` -> `0402`, `Package_TO_SOT_SMD:SOT-23-6` -> `SOT-23-6`."""
    name = (footprint or '').split(':')[-1]
    size, package = _SIZE.search(name), _PACKAGE.search(name)
    return ' '.join(part for part in (package.group(1) if package else None, size.group(1) if size else None) if part)


def _maker_key(name: str | None) -> str:
    first = re.split(r'[\s,./]+', (name or '').strip().lower())[0] if name else ''
    return re.sub(r'[^a-z0-9]', '', first)


def same_maker(a: str | None, b: str | None) -> bool:
    """Makers are written differently by each supplier ('Diodes Incorporated', 'Diodes Inc.'): the first word decides. Unknown matches anything."""
    if not a or not b:
        return True
    ka, kb = _maker_key(a), _maker_key(b)
    return bool(ka) and bool(kb) and (ka == kb or folded(a).startswith(folded(b)) or folded(b).startswith(folded(a)))


def _package(listing: Listing) -> str | None:
    for key in _PACKAGE_KEYS:
        if listing.params.get(key):
            return listing.params[key]
    return None


def _candidate(listing: Listing, relation: str) -> Candidate:
    return Candidate(mpn=listing.mpn, maker=listing.maker, description=listing.description, datasheet_url=listing.datasheet_url,
                     source=listing.source, relation=relation, package=_package(listing), product_url=listing.product_url)


async def _cached(supplier: str, kind: str, query: str, fetch, result: LookupResult) -> list[Listing]:
    """Listings from the cache, else from the supplier. A failure is recorded on the result, never raised."""
    stored = cache.get(supplier, kind, query)
    if stored is not None:
        result.suppliers[supplier] = 'cached'
        return [Listing.from_dict(item) for item in stored]
    try:
        listings = await fetch()
    except PartsError as error:
        result.suppliers[supplier] = f'{error.code}: {error.message}'
        return []
    cache.put(supplier, kind, query, [item.to_dict() for item in listings])
    result.suppliers[supplier] = 'ok'
    return listings


def merge(listings: list[Listing]) -> PartInfo:
    """One part from the exact listings of one maker. DigiKey leads where both list the field; a disagreement is recorded, not hidden."""
    ordered = sorted(listings, key=lambda item: item.source != 'digikey')
    conflicts: list[str] = []

    def first(field: str):
        for item in ordered:
            value = getattr(item, field)
            if value:
                return value, item.source
        return None, None

    maker, maker_from = first('maker')
    description, description_from = first('description')
    datasheet, datasheet_from = first('datasheet_url')
    lifecycle, _ = first('lifecycle')
    for field in ('maker', 'datasheet_url'):
        values = {item.source: getattr(item, field) for item in ordered if getattr(item, field)}
        if field == 'maker' and len({folded(v) for v in values.values()}) > 1 and not all(same_maker(a, b) for a in values.values() for b in values.values()):
            conflicts.append(f'maker differs: {values}')
        if field == 'datasheet_url' and len(set(values.values())) > 1:
            conflicts.append(f'datasheet link differs by supplier: {values}')
    params: dict[str, dict] = {}
    by_name: dict[str, str] = {}
    for item in ordered:
        for key, value in item.params.items():
            slot = folded(key)
            if slot in by_name:
                if folded(params[by_name[slot]]['value']) != folded(value):
                    conflicts.append(f'{key}: {params[by_name[slot]]["source"]} says {params[by_name[slot]]["value"]!r}, {item.source} says {value!r}')
                continue
            by_name[slot] = key
            params[key] = {'value': value, 'source': item.source}
    return PartInfo(
        mpn=ordered[0].mpn, maker=maker, description=description, datasheet_url=datasheet, params=params,
        source=sorted({item.source for item in ordered}),
        sources={'maker': maker_from, 'description': description_from, 'datasheet_url': datasheet_from, 'params': 'per parameter'},
        product_urls={item.source: item.product_url for item in ordered if item.product_url}, lifecycle=lifecycle, conflicts=conflicts)


def _dedupe(candidates: list[Candidate]) -> list[Candidate]:
    seen, out = set(), []
    for item in candidates:
        key = (item.source, normal(item.mpn), folded(item.maker))
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


async def lookup(mpn: str | None = None, *, value: str | None = None, footprint: str | None = None, maker: str | None = None) -> LookupResult:
    query = {'mpn': mpn, 'value': value, 'footprint': footprint, 'maker': maker}
    result = LookupResult(status='error', query=query)
    digikey, mouser = digikey_credentials(), mouser_key()
    if not (digikey or mouser):
        result.status = NOT_CONFIGURED
        result.reason = 'No DigiKey or Mouser key is set, so parts cannot be looked up. Add one in Settings. Reviews and simulations run without it.'
        return result
    result.suppliers = {'digikey': 'not configured' if not digikey else 'unused', 'mouser': 'not configured' if not mouser else 'unused'}
    number = clean(mpn, 120) if mpn else None
    if number and len(normal(number)) < 3:
        result.reason = 'That part number is too short to search.'
        return result
    if not number and not (clean(value, 120) or footprint):
        result.reason = 'Give a manufacturer part number, or a value and a footprint.'
        return result
    async with http_module.new_client() as http:
        if number:
            await _by_number(result, number, maker, digikey, mouser, http)
        else:
            await _by_value(result, clean(value, 120) or '', footprint, maker, digikey, mouser, http)
    return result


async def _by_number(result: LookupResult, number: str, maker: str | None, digikey, mouser, http) -> None:
    wanted = normal(number)
    listings: list[Listing] = []
    if digikey:
        async with DigiKeyClient(*digikey, http=http) as client:
            listings += await _cached('digikey', 'search', number.upper(), lambda: client.search(number), result)
    if mouser:
        async with MouserClient(mouser, http=http) as client:
            listings += await _cached('mouser', 'mpn', number.upper(), lambda: client.by_part_number(number), result)
    tried = [text for text in result.suppliers.values() if text not in {'not configured', 'unused'}]
    if tried and all(text not in {'ok', 'cached'} for text in tried):
        result.status = 'error'
        result.reason = 'No supplier could be reached: ' + '; '.join(f'{k}: {v}' for k, v in result.suppliers.items() if v not in {'not configured', 'unused'})
        return
    if mouser and not digikey and not any(normal(item.mpn) == wanted for item in listings):
        async with MouserClient(mouser, http=http) as client:        # Mouser only: its keyword search supplies the near matches
            listings += await _cached('mouser', 'kw', number.upper(), lambda: client.by_keywords(number), result)
    exact = [item for item in listings if normal(item.mpn) == wanted and (maker is None or same_maker(item.maker, maker))]
    if exact:
        clusters: list[list[Listing]] = []
        for item in exact:
            for cluster in clusters:
                if same_maker(cluster[0].maker, item.maker):
                    cluster.append(item)
                    break
            else:
                clusters.append([item])
        if len(clusters) == 1:
            result.status, result.part = 'found', merge(clusters[0])
            if len(result.part.source) == 1:
                missing = 'mouser' if result.part.source == ['digikey'] else 'digikey'
                if result.suppliers.get(missing) in {'ok', 'cached'}:
                    result.notes.append(f'Only {result.part.source[0]} lists this exact number.')
            return
        result.status = 'ambiguous'
        result.reason = f'{len(clusters)} makers list the number {number}. Say which maker is meant.'
        result.candidates = _dedupe([_candidate(cluster[0], 'exact') for cluster in clusters])
        return
    near = []
    for item in listings:
        other = normal(item.mpn)
        relation = 'orderable_variant' if other.startswith(wanted) and other != wanted else 'near'
        near.append(_candidate(item, relation))
    near.sort(key=lambda c: c.relation != 'orderable_variant')
    result.candidates = _dedupe(near)[:MAX_CANDIDATES]
    if not result.candidates:
        result.status = 'not_found'
        result.reason = f'No listed part has the number {number}. Check the spelling, or search by value and footprint to see candidates.'
        return
    result.status = 'ambiguous'
    result.reason = (f'No supplier lists exactly {number}. The closest listings are below; pick one by its full number. '
                     'Nothing was chosen for you.')
    variants = [c for c in result.candidates if c.relation == 'orderable_variant']
    links = {c.datasheet_url for c in variants}
    if variants and len(links) == 1 and None not in links and len({_maker_key(c.maker) for c in variants}) <= 1:
        result.family_datasheet_url = links.pop()
        result.notes.append(f'Every listed variant of {number} ({len(variants)}) shares one datasheet.')


async def _by_value(result: LookupResult, value: str, footprint: str | None, maker: str | None, digikey, mouser, http) -> None:
    words = ' '.join(part for part in (value, footprint_words(footprint), maker or '') if part)
    if not words:
        result.reason = 'The value and footprint gave nothing to search for.'
        return
    listings: list[Listing] = []
    if digikey:
        async with DigiKeyClient(*digikey, http=http) as client:
            listings += await _cached('digikey', 'search', words.upper(), lambda: client.search(words), result)
    if mouser and not digikey:                                   # one search is enough; Mouser answers only when DigiKey cannot
        async with MouserClient(mouser, http=http) as client:
            listings += await _cached('mouser', 'kw', words.upper(), lambda: client.by_keywords(words), result)
    result.candidates = _dedupe([_candidate(item, 'keyword') for item in listings])[:MAX_CANDIDATES]
    if not result.candidates:
        failed = [f'{k}: {v}' for k, v in result.suppliers.items() if v not in {'ok', 'cached', 'not configured', 'unused'}]
        result.status, result.reason = ('error', 'The search failed. ' + '; '.join(failed)) if failed else ('not_found', f'Nothing is listed for "{words}".')
        return
    result.status = 'ambiguous'
    result.reason = (f'A value and footprint alone ("{words}") match many parts. These are candidates, not a match: '
                     'the manufacturer part number is needed to name one part.')
