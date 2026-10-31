"""Optional parts data: DigiKey and Mouser lookups and datasheet reading.

Everything here is optional. With no key set, `available()` is False, `lookup` answers `not_configured`, and the rest of TrustPCB
runs as before. All functions are async (the review tools are) and none returns, logs or emits a key.

    available()                                   True when a supplier key is set
    await lookup(mpn=... | value=..., footprint=...)    -> LookupResult (see catalog.py); `.to_dict()` for a tool answer
    await datasheet(mpn, url=None)                 -> DatasheetResult: the PDF cached in the data folder
    await read_datasheet(mpn, question, url=None)  -> dict of cited passages (page and URL), marked as untrusted text
    await datasheet_inputs(mpn, url=None)          -> dict of typical inputs the text states, each with page and quote

Datasheet text is third-party data. Every model-facing result carries `data_notice` and `untrusted_text`.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from . import datasheets
from .errors import PartsError
from .pdftext import SheetText, extract
from .catalog import available, configured, lookup
from .model import DATA_NOTICE, Candidate, Listing, LookupResult, PartInfo
from .readsheet import find_passages, tool_result, typical_inputs

__all__ = ['available', 'configured', 'lookup', 'datasheet', 'read_datasheet', 'datasheet_inputs', 'DatasheetResult', 'LookupResult',
           'PartInfo', 'Candidate', 'Listing', 'PartsError', 'DATA_NOTICE']


@dataclass
class DatasheetResult:
    status: str                      # 'ok' | 'ambiguous' | 'not_found' | 'not_configured' | 'failed'
    mpn: str | None
    url: str | None = None
    path: str | None = None
    sha256: str | None = None
    pages: int | None = None
    cached: bool | None = None
    reason: str | None = None
    note: str | None = None
    candidates: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


async def _link(mpn: str | None, url: str | None, maker: str | None) -> tuple[str | None, str | None, DatasheetResult | None]:
    """The datasheet link for a part: the one given, else from the lookup. Returns (url, note, failure)."""
    if url:
        return url, None, None
    if not mpn:
        return None, None, DatasheetResult('failed', mpn, reason='Give a manufacturer part number or a datasheet link.')
    found = await lookup(mpn, maker=maker)
    if found.status == 'found':
        if found.part and found.part.datasheet_url:
            return found.part.datasheet_url, None, None
        return None, None, DatasheetResult('failed', mpn, reason='The suppliers list this part but give no datasheet link.')
    if found.status == 'ambiguous' and found.family_datasheet_url:
        return found.family_datasheet_url, ' '.join(found.notes) + ' The link comes from those listings, not from an exact listing of the number asked.', None
    status = found.status if found.status in {'ambiguous', 'not_found', 'not_configured'} else 'failed'
    return None, None, DatasheetResult(status, mpn, reason=found.reason, candidates=[c.to_dict() for c in found.candidates])


async def datasheet(mpn: str | None = None, *, url: str | None = None, maker: str | None = None) -> DatasheetResult:
    link, note, failure = await _link(mpn, url, maker)
    if failure:
        return failure
    try:
        stored = await datasheets.fetch(link)
        text = await extract(stored)
    except PartsError as error:
        return DatasheetResult('failed', mpn, url=link, reason=error.message, note=note)
    return DatasheetResult('ok', mpn, url=link, path=str(stored.path), sha256=stored.sha256, pages=text.page_count, cached=stored.cached, note=note)


async def _open(mpn: str | None, url: str | None, maker: str | None) -> tuple[str | None, SheetText | None, str | None, dict | None]:
    link, note, failure = await _link(mpn, url, maker)
    if failure:
        return None, None, None, {**failure.to_dict(), 'path': None}
    try:
        text = await extract(await datasheets.fetch(link))
    except PartsError as error:
        return link, None, note, {'status': 'failed', 'mpn': mpn, 'url': link, 'reason': error.message, 'note': note, 'candidates': []}
    return link, text, note, None


async def read_datasheet(mpn: str | None, question: str, *, url: str | None = None, maker: str | None = None) -> dict:
    """Passages from the part's datasheet that bear on the question, each with its page and the document URL."""
    link, text, note, failure = await _open(mpn, url, maker)
    if failure:
        return failure
    return tool_result(mpn=mpn, url=link, question=question, passages=find_passages(text, question), sheet=text, note=note)


async def datasheet_inputs(mpn: str | None, *, url: str | None = None, maker: str | None = None) -> dict:
    """Typical inputs (quiescent current, maximum current, thermal resistance, voltage range, rise time) where the text states them."""
    link, text, note, failure = await _open(mpn, url, maker)
    if failure:
        return failure
    return {'status': 'ok', 'mpn': mpn, 'url': link, 'pages': text.page_count, 'inputs': typical_inputs(text, link), 'note': note,
            'untrusted_text': True, 'data_notice': DATA_NOTICE}
