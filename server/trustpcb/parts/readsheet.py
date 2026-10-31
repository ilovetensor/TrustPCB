"""Find what a datasheet says: passages for a question, and typical inputs where they are stated plainly.

Everything returned from here is text copied out of a third-party PDF. It is data. Each passage carries its page and the
document's URL, and every result for a model carries `DATA_NOTICE`.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

from .pdftext import SheetText
from .model import DATA_NOTICE

PASSAGE_CHARS = 700
MAX_PASSAGES = 5
_STOP = frozenset('a an and are as at be by can do does for from has have how in is it its of on or the this that to what when which with value values typical typically'.split())
SYNONYMS = {
    'quiescent': ['quiescent', 'iq', 'ground pin current', 'no-load', 'supply current', 'standby'],
    'thermal': ['thermal resistance', 'junction-to-ambient', 'rθja', 'θja', 'rthja', 'theta'],
    'resistance': ['thermal resistance', 'θja', 'rθja'],
    'dropout': ['dropout', 'vdo'],
    'rise': ['rise time', 'tr'],
    'current': ['current'],
    'voltage': ['voltage', 'vin', 'supply'],
}


def _terms(question: str) -> list[str]:
    words = [w for w in re.findall(r"[a-zA-Zθ0-9°µ.+-]+", question.lower()) if len(w) > 1 and w not in _STOP]
    extra = [alt for w in words for alt in SYNONYMS.get(w, ()) if alt not in words]
    return list(dict.fromkeys(words + extra))


@dataclass
class Passage:
    page: int
    text: str
    score: float

    def to_dict(self, url: str) -> dict:
        return {'page': self.page, 'quote': self.text, 'url': url, 'score': round(self.score, 2)}


def _chunks(page_text: str) -> list[str]:
    lines = [ln.strip() for ln in page_text.splitlines() if ln.strip()]
    out, step, size = [], 3, 6
    for start in range(0, max(len(lines), 1), step):
        chunk = ' '.join(lines[start:start + size])
        if chunk:
            out.append(chunk[:PASSAGE_CHARS])
        if start + size >= len(lines):
            break
    return out


def find_passages(sheet: SheetText, question: str, limit: int = MAX_PASSAGES) -> list[Passage]:
    terms = _terms(question)
    if not terms:
        return []
    chunks = [(number, chunk, chunk.lower()) for number, text in enumerate(sheet.pages, start=1) for chunk in _chunks(text)]
    document_frequency = {t: sum(1 for _, _, low in chunks if t in low) for t in terms}
    scored: list[Passage] = []
    for number, chunk, low in chunks:
        score = sum(math.log(1 + len(chunks) / (1 + document_frequency[t])) for t in terms if document_frequency[t] and t in low)
        if not score:
            continue
        if re.search(r'\d', chunk):
            score *= 1.15                                           # a passage with a number is more useful than prose
        if re.search(r'\.{6,}\s*\d+\s*$', chunk):
            score *= 0.2                                            # a table of contents line
        scored.append(Passage(number, chunk, score))
    scored.sort(key=lambda p: -p.score)
    picked: list[Passage] = []
    per_page: dict[int, int] = {}
    for passage in scored:
        if per_page.get(passage.page, 0) >= 2:
            continue
        per_page[passage.page] = per_page.get(passage.page, 0) + 1
        picked.append(passage)
        if len(picked) >= limit:
            break
    return picked


# ---- typical inputs ----

_NUM = r'[-+±]?\d+(?:[.,]\d+)?'
_DASH = r'(?<!\S)[—–-](?!\S)'
_CELL = rf'(?:{_NUM}|{_DASH})'
_UNITS = {
    'current': r'(?:[mnuµμp]\s?)?A(?![A-Za-z/])',
    'resistance': r'(?:°\s?C|˚\s?C|K)\s?/\s?W',
    'voltage': r'(?:[mµμ]\s?)?V(?![A-Za-z])',
    'time': r'(?:[mnuµμp]\s?)?s(?![A-Za-z])',
}
_CONDITION = re.compile(rf'[A-Za-zθ_()]{{1,12}}\s*=\s*{_NUM}\s*[A-Za-zµμΩ°]{{0,3}}(?:\s*(?:to|~|–)\s*{_NUM}\s*[A-Za-zµμΩ°]{{0,3}})?')
INPUTS = {
    'quiescent_current': {'label': 'Quiescent current', 'kind': 'current',
                          'labels': r'quiescent current|\bI\s?Q\b|ground (?:pin )?current|no[- ]load (?:supply|input|ground) current|supply current'},
    'max_output_current': {'label': 'Maximum output current', 'kind': 'current',
                           'labels': r'(?:maximum|max\.?|rated|continuous)\s+(?:dc\s+)?output current|output current\s*\(?max\.?\)?|\bI\s?OUT\s?\(\s?MAX\s?\)'},
    'thermal_resistance_ja': {'label': 'Thermal resistance, junction to ambient', 'kind': 'resistance', 'window': 4,
                              'labels': r'junction[- ]to[- ]ambient|\bR?\s?θ\s?JA\b|\bR\s?TH\s?JA\b'},
    'input_voltage_range': {'label': 'Input or supply voltage range', 'kind': 'voltage', 'ranges_only': True,
                            'labels': r'input voltage(?: range)?|supply voltage(?: range)?|operating (?:input |supply )?voltage|\bV\s?IN\b|\bV\s?DD\b'},
    'rise_time': {'label': 'Rise time', 'kind': 'time', 'labels': r'rise time|\bt\s?R\b'},
}


def _num(text: str) -> float:
    return float(text.replace(',', '.').replace('±', '').replace('+', ''))


def _cells(text: str) -> list[float | None]:
    return [None if re.fullmatch(_DASH, token) or token in '—–-' else _num(token) for token in re.findall(rf'{_NUM}|{_DASH}|[—–-]', text)]


def _clean_unit(unit: str) -> str:
    return re.sub(r'\s', '', unit).replace('μ', 'µ').replace('˚', '°')


def _parse_line(spec: dict, line: str, following: list[str]) -> dict | None:
    label = re.search(spec['labels'], line, re.IGNORECASE)
    if not label:
        return None
    rest = _CONDITION.sub(' ', line[label.end():])                  # 'VIN = 2.5V, IOUT = 0mA' is a test condition, not the value
    unit = _UNITS[spec['kind']]
    row = re.search(rf'((?:{_CELL}\s+){{0,2}}{_CELL})\s*({unit})', rest)
    if row:
        cells, name = _cells(row.group(1)), _clean_unit(row.group(2))
        tag = re.search(r'\b(typ|max|min)\b', rest[row.end():row.end() + 12], re.IGNORECASE)
        if len(cells) == 1 and cells[0] is not None and not spec.get('ranges_only'):
            return {'form': 'value', 'value': cells[0], 'unit': name, 'column': tag.group(1).lower() if tag else None, 'confident': True}
        if len(cells) == 2 and spec['kind'] == 'voltage' and None not in cells and cells[0] < cells[1]:
            return {'form': 'range', 'min': cells[0], 'max': cells[1], 'unit': name, 'confident': True}
        if len(cells) == 3 and any(c is not None for c in cells) and not spec.get('ranges_only'):
            return {'form': 'min_typ_max', 'min': cells[0], 'typ': cells[1], 'max': cells[2], 'unit': name, 'confident': True}
        return {'form': 'table_row', 'values': [c for c in cells if c is not None], 'unit': name, 'confident': False}
    if spec['kind'] == 'voltage':
        rng = re.search(rf'({_NUM})\s*V?\s*(?:to|–|—|~)\s*({_NUM})\s*({unit})', rest)
        if rng and _num(rng.group(1)) < _num(rng.group(2)):
            return {'form': 'range', 'min': _num(rng.group(1)), 'max': _num(rng.group(2)), 'unit': _clean_unit(rng.group(3)), 'confident': True}
    window = spec.get('window')
    if window:                                                    # the value is on the next lines (a table cell the reader split)
        text = ' '.join([line, *following[:window]])
        if re.search(unit, text):
            return {'form': 'text', 'quote_window': text[:300], 'confident': False}
    return None


def _fields(hit: dict) -> dict:
    """The numbers a confident hit states, by column."""
    if hit['form'] == 'value':
        return {hit['column'] or 'value': hit['value']}
    return {key: hit[key] for key in ('min', 'typ', 'max') if hit.get(key) is not None}


def _merge(hits: list[dict]) -> dict | None:
    """One set of columns that every hit agrees with, or None when two hits disagree."""
    merged: dict = {}
    for hit in hits:
        for key, value in _fields(hit).items():
            if key == 'value':
                if merged.get('value', value) != value and value not in merged.values():
                    return None
                if not any(v == value for v in merged.values()):
                    merged['value'] = value
            elif key in merged and merged[key] != value:
                return None
            else:
                merged[key] = value
    if 'value' in merged and any(k != 'value' and v == merged['value'] for k, v in merged.items()):
        del merged['value']
    return merged


def _distinct(hits: list[dict]) -> list[dict]:
    seen, out = set(), []
    for hit in hits:
        key = (tuple(sorted(_fields(hit).items())), hit.get('unit')) if hit['confident'] else hit['quote']
        if key not in seen:
            seen.add(key)
            out.append(hit)
    return out


def typical_inputs(sheet: SheetText, url: str) -> dict:
    """Inputs the text states plainly, each with its page and quote. A value is `stated` only when a line shows one number, a
    min/typ/max row or a voltage range with its unit, and every such line in the document agrees. Anything else (a row whose columns are
    unclear, a table the reader split, two different values) is listed as candidates for the reader to settle from the quote."""
    out: dict[str, dict] = {}
    for name, spec in INPUTS.items():
        hits = []
        for number, text in enumerate(sheet.pages, start=1):
            lines = [ln.strip() for ln in text.splitlines()]
            for index, line in enumerate(lines):
                if not line or len(line) > 400:
                    continue
                parsed = _parse_line(spec, line, lines[index + 1:index + 6])
                if parsed:
                    window = parsed.pop('quote_window', None)
                    hits.append({**parsed, 'page': number, 'quote': (window or line)[:300], 'url': url})
        if not hits:
            continue
        sure = [h for h in hits if h['confident']]
        merged = _merge(sure) if sure else None
        if merged:
            lead = max(sure, key=lambda h: len(_fields(h)))
            out[name] = {'label': spec['label'], 'status': 'stated', **merged, 'unit': lead['unit'], 'page': lead['page'], 'quote': lead['quote'], 'url': url,
                         'also_on_pages': sorted({h['page'] for h in sure} - {lead['page']})[:6]}
        else:
            out[name] = {'label': spec['label'], 'status': 'conflicting' if sure else 'needs_reading',
                         'note': ('The document gives different values; read the quotes.' if sure else 'Nothing was stated plainly enough to take as a value; read the quote.'),
                         'candidates': _distinct(sure + [h for h in hits if not h['confident']])[:5]}
    return out


def tool_result(*, mpn: str | None, url: str, question: str, passages: list[Passage], sheet: SheetText, note: str | None = None) -> dict:
    return {
        'status': 'ok' if passages else 'no_match', 'mpn': mpn, 'url': url, 'question': question,
        'pages': sheet.page_count, 'passages': [p.to_dict(url) for p in passages],
        'note': note, 'truncated': sheet.truncated or None, 'untrusted_text': True, 'data_notice': DATA_NOTICE,
        'hint': None if passages else 'Nothing in the datasheet text matched that question. Try the name the document uses (for example the symbol).'}
