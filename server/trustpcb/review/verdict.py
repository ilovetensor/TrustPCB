"""The confirm verdict, and the prose that has to follow it.

A confirm run decides what the screen runs only suggested. `rounds_from_rows` reads the confirm comparisons out of the evidence
(`confirm comparison` for the first attempt, `refine confirm comparison` for each refine round) and `problems` refuses text that
contradicts them: a target called met when no confirm run met it, an improvement called "did not hold" when every comparison says it held
(smaller or not), or the other way round. A claim that the target was met is allowed only when its sentence says it is a screen result.
"""
from __future__ import annotations

import re

from .number_check import Problem, sentence_spans

HELD = ('held', 'held_smaller')
OUTCOME_LABEL = {
    'held': 'held at confirm quality', 'held_smaller': 'held, smaller than the screen showed', 'held_after_retune': 'did not hold with the screen parts, held after re-tuning',
    'not_held': 'did not hold at confirm quality', 'improved_short_of_target': 'improved, target not met', 'unconfirmed': 'not confirmed (no confirmed base)',
}

_MET = r'(?:met|meets|reached|reaches|hit|hits|achieved|achieves|satisfied|satisfies|cleared)'
_TARGET_CLAIMS = [
    re.compile(rf'\b{_MET}\s+(?:the\s+|its\s+|that\s+)?(?:[-−]?\d+(?:\.\d+)?\s*dB\s+)?(?:tuning\s+|s11\s+|return[- ]loss\s+)?(?:target|goal)\b', re.I),
    re.compile(rf'\b(?:target|goal)\b[^.;:]{{0,25}}?\b(?:was|is|has\s+been|had\s+been|got|were)\s+(?:also\s+|now\s+|fully\s+|already\s+|easily\s+)?{_MET}\b', re.I),
]
_NEGATION = re.compile(r"\b(?:not|never|no|n't|unless|until|if|cannot|nor|neither|whether|would|could|should|may|might|without|before|only\s+if)\b", re.I)
_DID_NOT_HOLD = re.compile(r"\b(?:did|does|do|has|have|had)\s*(?:not|n't)\s+hold\b|\bfail(?:ed|s)?\s+to\s+hold\b|\b(?:not|never)\s+held\b|\bwas\s+not\s+confirmed\b|\bdid\s+not\s+survive\b", re.I)
_DID_HOLD = re.compile(r'\b(?:improvement|change|fix|gain|result|match(?:ing)?)\s+(?:also\s+)?held\b|\bheld\s+(?:at|in|under)\s+confirm', re.I)
_SCREEN_LABEL = re.compile(r'\bscreen\b', re.I)
_ANTENNA_WORDS = re.compile(r'S11|\bdB\b|return loss|\bmatch|antenna|\bband\b|resonan|confirm|improv|simulat|\bgain\b', re.I)     # a layout finding that "meets the target distance" is not about the tuning target


def claims_target_met(sentence: str) -> bool:
    """Does this sentence say the tuning target was met (and not "not met", "if it is met", "never reached")?"""
    for pattern in _TARGET_CLAIMS:
        for match in pattern.finditer(sentence):
            if not _NEGATION.search(sentence[max(0, match.start() - 45):match.start()]) and not re.match(r'\s*(?:only\s+if|if|unless)\b', sentence[match.end():match.end() + 12], re.I):
                return True
    return False


def meets_target(confirmation: dict) -> bool:
    free = (confirmation.get('matching_at_confirm') or {}).get('free') or {}
    return bool(confirmation.get('meets_target') or free.get('meets_target') or (confirmation.get('best') or {}).get('meets_target'))


def rounds_from_rows(rows: list[dict]) -> list[dict]:
    """The confirm comparisons of a review, oldest first: [{evidence, refine, outcome, hold, held, meets_target, ...}]."""
    out = []
    for row in rows or []:
        values = row.get('values') or {}
        confirmation = values.get('confirmation')
        if not isinstance(confirmation, dict) or not confirmation.get('outcome') or (values.get('trust') or {}).get('status') != 'ok':
            continue
        best = confirmation.get('best') or {}
        base = confirmation.get('base') or {}
        hold = confirmation.get('hold') or confirmation.get('outcome')
        out.append({
            'evidence': row.get('code'), 'refine': 'refine' in str(values.get('label') or ''), 'outcome': confirmation['outcome'], 'hold': hold,
            'held': hold in HELD, 'meets_target': meets_target(confirmation), 'statement': confirmation.get('statement'),
            'base_evidence': confirmation.get('base_evidence'), 'best_evidence': best.get('evidence'), 'base_worst_s11_db': base.get('band_worst_s11_db'),
            'best_worst_s11_db': best.get('band_worst_s11_db'), 'gain_db': confirmation.get('best_gain_db'),
            'locked': (confirmation.get('matching_at_confirm') or {}).get('locked'), 'free': (confirmation.get('matching_at_confirm') or {}).get('free'),
        })
    return out


def short_of_target(rounds: list[dict]) -> bool:
    return bool(rounds) and not any(r['meets_target'] for r in rounds)


def _code_of_ref(rows: list[dict] | None) -> dict[str, str]:
    return {(row.get('values') or {}).get('ref'): row.get('code') for row in rows or [] if (row.get('values') or {}).get('ref')}


def facts(rounds: list[dict], target_db: float | None = None, rows: list[dict] | None = None) -> str:
    """The verdict in plain words, with evidence codes, for a refusal message or a tool result."""
    refs = _code_of_ref(rows)
    parts = []
    for r in rounds:
        label = 'Refine round' if r['refine'] else 'First attempt'
        bits = [f'{label} ({r["evidence"]}): outcome {r["outcome"]}, {OUTCOME_LABEL.get(r["hold"], r["hold"])}']
        if r.get('base_worst_s11_db') is not None and r.get('best_worst_s11_db') is not None:
            bits.append(f'simulated worst in-band S11 {r["base_worst_s11_db"]} dB ({r.get("base_evidence")}) to {r["best_worst_s11_db"]} dB ({r.get("best_evidence")})'
                        + (f', gain {r["gain_db"]} dB' if r.get('gain_db') is not None else ''))
        for mode in ('locked', 'free'):
            m = r.get(mode)
            if m and m.get('band_worst_s11_db') is not None:
                bits.append(f'{mode} parts {m["band_worst_s11_db"]} dB' + (f' ({refs[m["ref"]]})' if m.get('ref') in refs else ''))
        bits.append('target ' + ('met' if r['meets_target'] else 'not met') + (f' (target {target_db:g} dB)' if target_db is not None else ''))
        parts.append('; '.join(bits))
    return ' | '.join(parts)


def verdict_object(rounds: list[dict], target_db: float | None = None) -> dict | None:
    """Compact form for the agent, so it writes finding prose from the verdict and not from memory of a screen run."""
    if not rounds:
        return None
    last = rounds[-1]
    return {
        'outcome': last['outcome'], 'hold': last['hold'], 'held': any(r['held'] for r in rounds), 'meets_target': any(r['meets_target'] for r in rounds), 'target_db': target_db,
        'rounds': [{k: r[k] for k in ('evidence', 'refine', 'outcome', 'hold', 'held', 'meets_target', 'base_evidence', 'best_evidence', 'base_worst_s11_db', 'best_worst_s11_db', 'gain_db')} for r in rounds],
        'rule': 'Write what you say about the match from this object. Do not call the target met unless meets_target is true, do not say the improvement did not hold when held is true, '
                'and quote each number with its evidence code and the word simulated.',
    }


def problems(texts: dict[str, str], rounds: list[dict], *, target_db: float | None = None, rows: list[dict] | None = None) -> list[Problem]:
    """Sentences that contradict the confirm verdict, or call a screen result the outcome."""
    out: list[Problem] = []
    short = short_of_target(rounds)
    all_held = bool(rounds) and all(r['held'] for r in rounds)
    none_held = bool(rounds) and all(r['hold'] == 'not_held' for r in rounds)
    for name, text in texts.items():
        for start, end in sentence_spans(text or ''):
            sentence = text[start:end].strip()
            if not _ANTENNA_WORDS.search(sentence):
                continue
            quote = sentence if len(sentence) <= 140 else sentence[:137] + '...'
            if claims_target_met(sentence) and not _SCREEN_LABEL.search(sentence) and (short or not rounds):
                if rounds:
                    message = (f'In {name}: "{quote}" says the target was met, but no confirm run met it. {facts(rounds, target_db, rows)}. '
                               'Say the target is not met, and if you mean the screen run, write "on the screen run" and give the confirm result beside it.')
                else:
                    message = (f'In {name}: "{quote}" calls the target met before any confirm run. A screen run is only for comparing, so write what the screen run reached, '
                               'for example "the screen run reached X dB (comparison only; a confirm run is needed)".')
                out.append(Problem('contradicts_confirm' if rounds else 'screen_result_called_final', name, sentence, message, {'verdict': verdict_object(rounds, target_db)} if rounds else None))
            elif all_held and _DID_NOT_HOLD.search(sentence) and not _SCREEN_LABEL.search(sentence):
                out.append(Problem('contradicts_confirm', name, sentence,
                                   f'In {name}: "{quote}" says the improvement did not hold, but every confirm comparison says it held. {facts(rounds, target_db, rows)}. '
                                   'Describe the confirm result as it is: it held (smaller than the screen showed where the outcome is held_smaller) and the target is not met. '
                                   'If you mean a run that is not one of these comparisons, cite its evidence code in the sentence and say which run it is.',
                                   {'verdict': verdict_object(rounds, target_db)}))
            elif none_held and _DID_HOLD.search(sentence) and not _SCREEN_LABEL.search(sentence):
                out.append(Problem('contradicts_confirm', name, sentence,
                                   f'In {name}: "{quote}" says the improvement held, but the confirm comparison says it did not. {facts(rounds, target_db, rows)}.',
                                   {'verdict': verdict_object(rounds, target_db)}))
    return out
