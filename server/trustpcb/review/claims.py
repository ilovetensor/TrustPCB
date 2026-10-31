"""Checks that tie what a text says to the evidence it cites.

`number_check` asks whether a number exists somewhere in the evidence. This module asks the sharper questions an independent fact-check found missing:

  * a number in a sentence that cites evidence codes must come from one of those rows (a number from another run is refused, and the message says
    which run it is from);
  * "A to B unit" must match the smallest and largest value of that measurement over the cited set ("10 to 16 mm" over distances that run from
    5.1 to 16.3 mm is refused);
  * "N signals", "N vias" and similar counts must not exceed the distinct nets, vias, tracks, parts or pads of the cited rows (41 gap crossings on
    9 nets are not 41 signals);
  * "with matching parts" next to a number that only the geometry run with the board's own parts produced is refused, and the message names the
    rows that do carry the matching parts.

A sentence that cites no code is judged against the rows of the finding (or the whole review, for a summary). A text cites rows as `E-175`,
`E-060 to E-100` or `E-060..E-100`.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

from .evidence import statistic_counts as ev_statistic_counts
from .fingerprint import TargetIndex
from .number_check import _UNIT_RE, Problem, Quantity, _explains, extract, sentence_spans

CODE_RE = re.compile(r'\bE-(\d{3,})(?:\s*(?:to|–|—|-|\.\.\.?|…)\s*E-(\d{3,}))?')
MAX_SPAN = 400                                   # a citation range wider than this is not expanded
RANGE_DIMENSIONS = ('length', 'area', 'resistance', 'capacitance', 'inductance', 'voltage', 'current', 'time')   # not S11, accepted power or frequency: those are compared run by run
MIN_GROUP = 3                                    # a spread needs at least this many rows of one measurement before "A to B" is judged against it
_IGNORED_NAMES = ('title', 'detail', 'whatif_op')

_RANGE_GAP = re.compile(rf'(?:\s?(?:{_UNIT_RE}))?\s*(?:to|–|—|-)\s*', re.I)
_BETWEEN_GAP = re.compile(rf'(?:\s?(?:{_UNIT_RE}))?\s+and\s+', re.I)
_STOP = {'of', 'to', 'and', 'or', 'than', 'from', 'by', 'in', 'on', 'at', 'the', 'a', 'an', 'is', 'are', 'with', 'for', 'as', 'that', 'which', 'out', 'up', 'only',
         'places', 'place', 'where', 'between', 'crossings', 'crossing', 'items', 'item', 'pairs', 'pair', 'results', 'result', 'records', 'events', 'violations', 'instances',
         'cases', 'spots', 'points', 'clusters', 'groups', 'gaps', 'shorts', 'errors', 'issues'}
_RECORD_AFTER = re.compile(r'\s+(?:crossings?|violations?|clearances?|errors?|items?|results?|records?|events?|issues?|gaps?|bridges|stubs?|detours?|shorts?|overlaps?|pairs?|intersections?|lengths?|widths?)\b', re.I)
_SUBSET_WORDS = re.compile(r'\b(?:some|several|most|worst|largest|longest|shortest|biggest|smallest|top|few|among|examples?|e\.g\.|including|such as|mainly|mostly|typically|about|around|roughly|of them)\b', re.I)
_REF_WORD = re.compile(r'\b[A-Z]{1,2}\d{1,3}\b')
_NET_WORD = re.compile(r'\b[A-Z][A-Z0-9]*[_+-][A-Z0-9_+-]+\b')
_INPUT_NAME = re.compile(r'^[A-Z]{1,3}\d+_')
_COUNT = re.compile(r'(?<![\w.#/-])(?P<n>\d{1,4})\s+(?P<mid>(?:[A-Za-z][A-Za-z-]*\s+){0,2}?)(?P<noun>signals?|nets?|tracks?|traces?|segments?|vias?|parts?|components?|capacitors?|caps?|resistors?|pads?|footprints?)\b')
_KIND = {'signal': 'nets', 'net': 'nets', 'track': 'tracks', 'trace': 'tracks', 'segment': 'tracks', 'via': 'vias', 'part': 'refs', 'component': 'refs', 'footprint': 'refs',
         'capacitor': 'caps', 'cap': 'caps', 'resistor': 'resistors', 'pad': 'pads'}
_KIND_WORD = {'nets': 'nets', 'tracks': 'tracks', 'vias': 'vias', 'refs': 'parts', 'caps': 'capacitors', 'resistors': 'resistors', 'pads': 'pads'}
_BRACKET_NET = re.compile(r'\[([^\]]+)\]')
_MATCHING_CLAIM = re.compile(r'\bwith\s+(?:(?:the|these|those|new|retuned|re-tuned|matching|best|refined|different)\s+)*(?:parts|values|network|components)\b|\bmatching\s+parts\s+(?:applied|fitted|added)\b|\bre-?tuned\b', re.I)
_OWN_PARTS = re.compile(r"(?:own|board's|original|existing|fitted|as-built|as built|current|unchanged|same)\s+(?:matching\s+)?(?:parts|network|values)\s*$", re.I)
_BARE_OWN = re.compile(r"\bwith\s+(?:the\s+)?(?:board's|original|existing|fitted|as-built|as built|current|unchanged|same)\b", re.I)


def _is_input(a: Quantity) -> bool:
    """A value the test was given (a geometry change, a part value) and not a result: it needs no citation of its own."""
    return a.name == 'whatif_op' or bool(_INPUT_NAME.match(a.name or ''))


def cited_codes(text: str) -> list[str]:
    """The evidence codes a text names, ranges expanded, in order of first use."""
    out: list[str] = []
    for match in CODE_RE.finditer(text or ''):
        first, last = match.group(1), match.group(2)
        width = len(first)
        if last and 0 <= int(last) - int(first) <= MAX_SPAN:
            out.extend(f'E-{n:0{width}d}' for n in range(int(first), int(last) + 1))
        else:
            out.append(f'E-{first}')
            if last:
                out.append(f'E-{last}')
    return list(dict.fromkeys(out))


def _span(codes: list[str]) -> str:
    return codes[0] if len(codes) == 1 else f'{codes[0]} to {codes[-1]}'


def _fmt(value: float) -> str:
    return f'{value:.4g}'


def _is_geometry_only(row: dict) -> bool:
    """A confirm or screen run of changed geometry that carries the board's own parts: no matching parts were applied to it."""
    values = row.get('values') or {}
    return (row.get('type') in ('simulation', 'confirm') and values.get('kind') == 'simulated' and 'parts' not in values
            and values.get('experiment') != 'matching' and not values.get('calculation'))


def _row_label(row: dict) -> str:
    values = row.get('values') or {}
    return f'{row.get("code")} ({values.get("ref") or "run"}, "{values.get("label") or row.get("type")}")'


def _entity_sets(rows: Iterable[dict], index: TargetIndex) -> dict[str, set[str]]:
    sets: dict[str, set[str]] = {'nets': set(), 'tracks': set(), 'vias': set(), 'refs': set(), 'pads': set(), 'caps': set(), 'resistors': set()}

    def add_ref(ref: str) -> None:
        sets['refs'].add(ref)
        if ref[:1] == 'C':
            sets['caps'].add(ref)
        if ref[:1] == 'R':
            sets['resistors'].add(ref)

    for row in rows:
        values = row.get('values') or {}
        for target in values.get('targets') or []:
            if target.startswith('track:'):
                sets['tracks'].add(target)
            elif target.startswith('via:'):
                sets['vias'].add(target)
            elif target.startswith('pad:'):
                sets['pads'].add(target)
                add_ref(index.ref_of.get(target, 'ref:' + target.split(':')[1]).removeprefix('ref:'))
            elif target.startswith('ref:'):
                add_ref(target.removeprefix('ref:'))
            elif target.startswith('net:'):
                sets['nets'].add(target.removeprefix('net:'))
            if target in index.net_of:
                sets['nets'].add(index.net_of[target])
        for detail in values.get('details') or []:
            sets['nets'].update(m for m in _BRACKET_NET.findall(str(detail)) if not m.startswith('<'))
    return sets


def _count_measurements(rows: Iterable[dict]) -> set[int]:
    found: set[int] = set()
    for row in rows:
        for item in ((row.get('values') or {}).get('measurements') or {}).values():
            if isinstance(item, dict) and item.get('unit') == 'count' and isinstance(item.get('value'), (int, float)):
                found.add(int(item['value']))
        found |= ev_statistic_counts((row.get('values') or {}).get('statistics'))      # gap_events, nets_with_gaps, distinct_vias ...: the analyzers' own counts
    return found


def _groups(allowed: Iterable[Quantity], codes: set[str]) -> dict[tuple[str, str], list[Quantity]]:
    groups: dict[tuple[str, str], list[Quantity]] = {}
    for a in allowed:
        if a.source in codes and a.name and a.name not in _IGNORED_NAMES and not a.name.startswith(('delta_', 'fixed_dip', 's11_dip', 'stat_')) and a.dimension in RANGE_DIMENSIONS:
            groups.setdefault((a.name, a.dimension), []).append(a)
    return {k: v for k, v in groups.items() if len({a.source for a in v}) >= MIN_GROUP}


def _ranges(sentence: str, quantities: list[Quantity]) -> list[tuple[Quantity, Quantity]]:
    out = []
    for first, second in zip(quantities, quantities[1:]):
        if first.dimension != second.dimension or first.dimension not in RANGE_DIMENSIONS:
            continue
        gap = sentence[first.end:second.start]
        before = sentence[:first.start]
        if re.search(r'\bfrom\s*$', before, re.I):
            continue                     # "from 3 mm to 5 mm" is a change, not a spread
        if _RANGE_GAP.fullmatch(gap) or (_BETWEEN_GAP.fullmatch(gap) and re.search(r'\bbetween\s*$', before, re.I)):
            out.append((first, second))
    return out


def check(texts: dict[str, str], *, scope_rows: list[dict], universe: list[dict], allowed: list[Quantity], board: Any = None, require_attached: bool = True) -> list[Problem]:
    """Every problem found, in text order. `scope_rows` are the rows the texts stand on (a finding's evidence, or all of the review for a summary);
    `universe` resolves every code of the review; `allowed` are the quantities of the scope rows plus the setup facts."""
    by_code = {r.get('code'): r for r in universe}
    scope_codes = {r.get('code') for r in scope_rows}
    index = TargetIndex(board)
    problems: list[Problem] = []
    for name, text in texts.items():
        spans = sentence_spans(text or '')
        previous_codes: list[str] = []
        previous_claim = False
        for start, end in spans:
            sentence = text[start:end]
            quote = sentence.strip() if len(sentence.strip()) <= 140 else sentence.strip()[:137] + '...'
            cited = cited_codes(sentence)
            unknown = [c for c in cited if c not in by_code]
            if unknown:
                problems.append(Problem('unknown_evidence_citation', name, sentence.strip(), f'In {name}: "{quote}" cites {", ".join(unknown[:4])}, which {"does" if len(unknown) == 1 else "do"} not exist in this review. Cite evidence codes the tools returned.'))
                previous_codes, previous_claim = [], False
                continue
            unattached = [c for c in cited if c not in scope_codes] if require_attached else []
            if unattached:
                problems.append(Problem('citation_not_attached', name, sentence.strip(),
                                        f'In {name}: "{quote}" cites {", ".join(unattached[:4])}, which {"is" if len(unattached) == 1 else "are"} not in the evidence of this finding. Add {"it" if len(unattached) == 1 else "them"} to evidence_ids, or remove the citation.',
                                        {'codes': unattached[:8]}))
                continue
            quantities = extract(sentence)
            chosen = set(cited) if cited else scope_codes
            # 1. each number comes from a row this sentence cites ------------------------------------------------------
            if cited:
                for q in quantities:
                    matches = [a for a in allowed if _explains(q, a)]
                    if not matches or any(a.source in cited or not a.source or _is_input(a) for a in matches):
                        continue
                    where = sorted({a.source for a in matches if a.source})
                    near = [a for a in allowed if a.source in cited and a.dimension == q.dimension and a.name not in _IGNORED_NAMES][:4]
                    hint = (' The cited rows have ' + ', '.join(f'{a.name} {_fmt(a.value)} {a.unit} ({a.source})' for a in near) + '.') if near else ''
                    labels = ', '.join(_row_label(by_code[c]) for c in where[:3] if c in by_code)
                    problems.append(Problem('number_not_in_cited_evidence', name, sentence.strip(),
                                            f'In {name}: "{q.raw}" is not a value of {_span(cited)}, which this sentence cites. It comes from {labels}.{hint} '
                                            f'Cite {_span(where)} for that number, or quote a value of the row you cite.', {'number': q.raw, 'found_in': where, 'cited': cited}))
            # 2. "A to B unit" against the spread of the cited set ---------------------------------------------------------
            subset = bool(_SUBSET_WORDS.search(sentence)) or len(set(_REF_WORD.findall(sentence))) >= 2 or len(set(_NET_WORD.findall(sentence))) >= 2
            for low, high in ([] if subset else _ranges(sentence, quantities)):
                groups = _groups(allowed, chosen)
                lo, hi = sorted((low, high), key=lambda q: q.base)
                member_groups = []
                ok = any(abs(a.base - lo.base) <= lo.tolerance and abs(b.base - hi.base) <= hi.tolerance
                         for a in allowed if a.name == 'stat_min_mm' and a.source in chosen for b in allowed if b.name == 'stat_max_mm' and b.source == a.source)   # the analyzer's own class range
                for (gname, _), members in groups.items():
                    near_lo = any(abs(m.base - lo.base) <= lo.tolerance for m in members)
                    near_hi = any(abs(m.base - hi.base) <= hi.tolerance for m in members)
                    if not (near_lo and near_hi):
                        continue
                    gmin, gmax = min(members, key=lambda m: m.base), max(members, key=lambda m: m.base)
                    if abs(gmin.base - lo.base) <= lo.tolerance and abs(gmax.base - hi.base) <= hi.tolerance:
                        ok = True
                        break
                    member_groups.append((gname, members, gmin, gmax))
                if ok or not member_groups:
                    continue
                gname, members, gmin, gmax = max(member_groups, key=lambda g: len(g[1]))
                codes = sorted({m.source for m in members})
                problems.append(Problem('range_mismatch', name, sentence.strip(),
                                        f'In {name}: "{low.value:.{low.decimals}f} to {high.value:.{high.decimals}f} {high.unit}" does not match the evidence: {gname.replace("_", " ")} runs from '
                                        f'{_fmt(gmin.value)} {gmin.unit} ({gmin.source}) to {_fmt(gmax.value)} {gmax.unit} ({gmax.source}) over {len(codes)} rows ({_span(codes)}). '
                                        f'Write the range as {_fmt(gmin.value)} to {_fmt(gmax.value)} {gmax.unit}, or, if you mean only some of the rows, cite just those rows by code in the same sentence.',
                                        {'measurement': gname, 'min': gmin.value, 'max': gmax.value, 'unit': gmin.unit, 'rows': len(codes)}))
            # 3. counts of things against the distinct entities of the cited set ---------------------------------------------
            rows_here = [by_code[c] for c in chosen if c in by_code]
            sets = None
            for match in _COUNT.finditer(sentence):
                words = match.group('mid').split()
                if any(w.lower() in _STOP or w in _unit_words() for w in words) or _RECORD_AFTER.match(sentence, match.end()):
                    continue
                n = int(match.group('n'))
                noun = match.group('noun').lower()
                kind = _KIND[noun[:-1] if noun[:-1] in _KIND else noun]
                sets = sets or _entity_sets(rows_here, index)
                distinct = sets[kind]
                if not distinct or n <= len(distinct) or n in _count_measurements(rows_here):
                    continue
                shown = sorted(distinct)[:6]
                word = _KIND_WORD[kind]
                problems.append(Problem('count_mismatch', name, sentence.strip(),
                                        f'In {name}: "{match.group(0).strip()}" counts records, not {word}. The cited evidence ({_span(sorted(c for c in chosen if c))}, {len(rows_here)} rows) covers {len(distinct)} distinct {word} '
                                        f'({", ".join(shown)}{", ..." if len(distinct) > len(shown) else ""}). Write "{len(distinct)} {word}" for the things, or name what the {n} are (for example "{n} gap crossings on {len(distinct)} {word}").',
                                        {'written': n, 'distinct': len(distinct), 'kind': word, 'rows': len(rows_here)}))
            # 4. "with matching parts" and a number only the geometry run produced -------------------------------------------
            claim = bool(_MATCHING_CLAIM.search(sentence)) and not _BARE_OWN.search(sentence)
            for q in quantities:
                if q.dimension not in ('dB', 'percent') or not (claim or (previous_claim and previous_codes)):
                    continue
                matches = [a for a in allowed if _explains(q, a) and a.source]
                geometry = [a for a in matches if a.source in by_code and _is_geometry_only(by_code[a.source])]
                if not matches or len(geometry) != len(matches):
                    continue
                if not claim and not any(a.source in previous_codes for a in geometry):
                    continue
                row = by_code[geometry[0].source]
                ref = (row.get('values') or {}).get('ref')
                siblings = [r for r in universe if (r.get('values') or {}).get('applied_to') == ref and ref]
                have = '; '.join(f'{r["code"]} ({(r.get("values") or {}).get("label")}) ' + ', '.join(f'{_fmt(a.value)} {a.unit}' for a in allowed if a.source == r['code'] and a.name == 's11_band_worst') for r in siblings)
                problems.append(Problem('number_from_other_run', name, sentence.strip(),
                                        f'In {name}: "{q.raw}" is {_row_label(row)}, the changed geometry run with the board\'s own parts, but the text says matching parts were applied. '
                                        + (f'The runs with matching parts are: {have}. ' if have else '')
                                        + 'Quote the matching-parts result with its own evidence code, or say the number is for the geometry change with the original parts.',
                                        {'number': q.raw, 'run': row.get('code'), 'matching_runs': [r['code'] for r in siblings]}))
            previous_codes, previous_claim = cited, claim
    return problems


def _unit_words() -> set[str]:
    from .number_check import _UNIT_TABLE
    return set(_UNIT_TABLE)


def drop_sentences(text: str, sentences: Iterable[str]) -> str:
    """`text` without the given sentences (used when a refusal has been repeated and the bounded retries are used up)."""
    drop = {s.strip() for s in sentences}
    kept = [text[a:b] for a, b in sentence_spans(text) if text[a:b].strip() not in drop]
    return ' '.join(k.strip() for k in kept).strip()
