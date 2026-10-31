"""Cheap wording checks the evidence cannot decide, refused once and never twice.

A fact-check of finished reviews found sentences no analysis supported or that the file contradicts:

  * a cause with nothing behind it ("VBUS_RAW also feeds the long decoupling loops already found");
  * a do-not-populate statement that names one of several parts, or gives a purpose ("C19 is marked DNP as a tuning slot" when C20 is too);
  * advice that contradicts the rest of the review ("keep the feed width constant" beside "size the feed for 50 ohm");
  * a severity that moved away from the analyzers' own hint with no reason.

Each is a `Problem` with a clear message. The server refuses the text ONCE (`Once`); the same sentence sent again is kept, so the agent is never
jammed, and a causal sentence that stays is listed under "not tested" on the finding so the report does not present it as shown.
These checks are wording heuristics: they look for a verb and a missing citation, not for meaning.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

from .number_check import Problem, sentence_spans  # noqa: F401  (Problem is re-exported for the callers of this module)

CAUSAL_CODE, DNP_ALL_CODE, DNP_INTENT_CODE, WIDTH_CODE, SEVERITY_CODE = (
    'causal_claim_unsupported', 'dnp_incomplete', 'dnp_intent_claim', 'width_advice_conflict', 'severity_without_reason')
SOFT_CODES = frozenset({CAUSAL_CODE, DNP_ALL_CODE, DNP_INTENT_CODE, WIDTH_CODE, SEVERITY_CODE})

_CODE = re.compile(r'\bE-\d{3,}\b')
_CAUSAL = re.compile(r'\b(?:feeds|feeding|causes|causing|caused\s+by|affects|leads\s+to|leading\s+to|degrades|contributes\s+to|results\s+in)\b', re.I)     # indicative verbs: "the antenna feed" is a noun
# "the LDO feeds an LED" names what a supply is wired to, which the schematic shows: not a claim about an effect
_WIRED_TO = re.compile(r'^\s+(?:only\s+|just\s+|also\s+)?(?:(?:an?|the|each|all|both|its|their|two|three)\s+)?(?:\w+\s+){0,2}?(?:leds?|loads?|rails?|regulators?|circuits?|supply|supplies|nets?|pins?|inputs?|outputs?|devices?|sensors?|radio|mcu|board|parts?|connectors?|antenna|capacitors?|resistors?|[A-Z]{1,2}\d{1,3}\b)', re.I)
_HEDGE = re.compile(r"\b(?:may|might|could|can|would|should|possibly|probably|likely|perhaps|if|whether|unless|suspect(?:ed)?|unclear|unknown|untested|not\s+tested|"
                    r"not|no|never|nothing|n't|cannot|question|open\s+question|opinion)\b", re.I)
_REF = re.compile(r'\b([A-Z]{1,2})(\d{1,3})\b')
_DNP = re.compile(r'\b(?:dnp|dnf|dni|do[\s_-]?not[\s_-]?(?:populate|fit|place|install)|not[\s_-]?(?:fitted|populated|placed|installed)|unpopulated)\b', re.I)
_INTENT = re.compile(r'\b(?:tuning|tune|tuned|trim|trimming|placeholder|reserved|spare|optional|provision|slot|footprint\s+for|in\s+case|for\s+future|for\s+matching|tweak\w*)\b', re.I)
_SAYS_ALL = re.compile(r'\b(?:all|both|every|each|other|others|also|remaining|etc)\b', re.I)
_KEEP_WIDTH = re.compile(r'\bkeep\b[^.;]*\bwidth\b[^.;]*\b(?:constant|uniform|unchanged|the\s+same|as\s+it\s+is)\b|\bconstant\s+(?:feed\s+)?width\b|\bdo\s+not\s+change\s+the\s+(?:feed\s+)?width\b', re.I)


def _quote(sentence: str) -> str:
    sentence = sentence.strip()
    return sentence if len(sentence) <= 140 else sentence[:137] + '...'


def _sentences(texts: dict[str, str]) -> Iterable[tuple[str, str]]:
    for name, text in texts.items():
        for a, b in sentence_spans(text or ''):
            yield name, text[a:b].strip()


def _clause(sentence: str, match: re.Match) -> str:
    """The part of the sentence the verb belongs to: between the nearest comma, semicolon or joining word before and after it ("likely to radiate, and X affects Y" hedges only the first half)."""
    start = max([m.end() for m in re.finditer(r'[,;:]|\s+(?:and|but|while|so|whereas)\s+', sentence[:match.start()])] or [0])
    after = re.search(r'[,;:]|\s+(?:and|but|while|so|whereas)\s+', sentence[match.end():])
    return sentence[start:match.end() + (after.start() if after else len(sentence))]


def causal_problems(texts: dict[str, str], codes: list[str] | None = None) -> list[Problem]:
    """A sentence with a causal verb, no hedge and no evidence code. `codes` are the evidence codes of the finding, named in the message."""
    out = []
    have = f' This finding\'s evidence: {", ".join(codes[:6])}.' if codes else ''
    for name, sentence in _sentences(texts):
        if name == 'title' or name.startswith(('marks', 'assumptions', 'not_tested')):
            continue
        match = _CAUSAL.search(sentence)
        if not match or _CODE.search(sentence) or _HEDGE.search(_clause(sentence, match)):
            continue
        if match.group(0).lower() in ('feeds', 'feeding') and _WIRED_TO.match(sentence[match.end():]):
            continue
        out.append(Problem(CAUSAL_CODE, name, sentence, f'In {name}: "{_quote(sentence)}" says one thing {match.group(0).lower()} another, but no analysis or evidence code in the sentence shows the link.{have} '
                           'Cite the evidence that shows it (put its code in the sentence), say what would test it ("not tested"), or write only what was measured. '
                           'The server refuses this once: if you send the same sentence again it stays, listed as not tested.', {'verb': match.group(0).lower()}))
    return out


def dnp_refs(board: Any) -> set[str]:
    """Every part the board or the schematic marks do-not-populate (also by a "DNP" value)."""
    if board is None or not hasattr(board, 'footprints'):
        return set()
    from ..analyzers.classify import is_dnp
    from ..analyzers.schematic import schematic_info
    found = {f.ref for f in board.footprints if is_dnp(f)}
    info = schematic_info(board)
    return found | {ref for ref, value in info.dnp.items() if value}


def dnp_problems(texts: dict[str, str], board: Any) -> list[Problem]:
    """A sentence about do-not-populate parts that names some of them but not all of that kind, or gives a purpose for them."""
    refs = dnp_refs(board)
    if not refs:
        return []
    out = []
    for name, sentence in _sentences(texts):
        if not _DNP.search(sentence):
            continue
        written = {m.group(0) for m in _REF.finditer(sentence)}
        named = written & refs
        if not named:
            continue
        prefixes = {re.match(r'[A-Z]+', r).group(0) for r in named}
        missing = sorted((r for r in refs - written if re.match(r'[A-Z]+', r).group(0) in prefixes), key=lambda r: (re.match(r'[A-Z]+', r).group(0), int(re.sub(r'\D', '', r) or 0)))
        everything = sorted(named | set(missing), key=lambda r: (re.match(r'[A-Z]+', r).group(0), int(re.sub(r'\D', '', r) or 0)))
        listing = everything[0] if len(everything) == 1 else ', '.join(everything[:-1]) + ' and ' + everything[-1]
        if missing and not _SAYS_ALL.search(sentence):
            out.append(Problem(DNP_ALL_CODE, name, sentence, f'In {name}: "{_quote(sentence)}" names {", ".join(sorted(named))} as do-not-populate, but the file also marks {", ".join(missing)}. '
                               f'Quote every affected part ({listing}). The server refuses this once.', {'named': sorted(named), 'missing': missing}))
        if _INTENT.search(sentence) and not _CODE.search(sentence):
            out.append(Problem(DNP_INTENT_CODE, name, sentence, f'In {name}: "{_quote(sentence)}" gives a purpose for the do-not-populate parts ({listing}). The file says only that they are not fitted, not why. '
                               'Write what the file says (they are marked do-not-populate, so the pads are open) and, if you think it is for tuning, call it your reading and say what would settle it. '
                               'The server refuses this once.', {'refs': everything}))
    return out


def width_conflict_problems(texts: dict[str, str], rows: list[dict]) -> list[Problem]:
    """"Keep the feed width constant" while the review's evidence has a width for the target impedance (`width_for_target`)."""
    need = None
    for row in rows:
        item = ((row.get('values') or {}).get('measurements') or {}).get('width_for_target')
        if isinstance(item, dict) and isinstance(item.get('value'), (int, float)):
            need = (item['value'], row.get('code'))
            break
    if need is None:
        return []
    out = []
    for name, sentence in _sentences(texts):
        if _KEEP_WIDTH.search(sentence):
            out.append(Problem(WIDTH_CODE, name, sentence, f'In {name}: "{_quote(sentence)}" tells the engineer to keep the width, but the impedance evidence ({need[1]}) gives {need[0]:g} mm for the target, '
                               'and the impedance finding says to size the feed for it. Say "keep the width uniform at the width chosen for the target impedance" (no steps in width), or explain why the existing width stays. '
                               'The server refuses this once.', {'width_for_target': need[0]}))
    return out


def problems(texts: dict[str, str], *, board: Any = None, rows: list[dict] | None = None, codes: list[str] | None = None) -> list[Problem]:
    found = causal_problems(texts, codes) + dnp_problems(texts, board) + width_conflict_problems(texts, rows or [])
    return found


class Once:
    """Remember which wording problems were already refused, so each is refused once and the same sentence is then allowed."""

    def __init__(self) -> None:
        self.seen: set[str] = set()

    def split(self, found: list[Problem]) -> tuple[list[Problem], list[Problem]]:
        """(problems to refuse now, problems already refused before). Marks the first group as seen."""
        fresh, kept = [], []
        for p in found:
            key = f'{p.code}|{" ".join(p.sentence.split())}'
            if key in self.seen:
                kept.append(p)
            else:
                self.seen.add(key)
                fresh.append(p)
        return fresh, kept
