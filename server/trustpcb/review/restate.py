"""The investigation story follows the confirm result.

Suspect conclusions and the investigation outcome are written at the screen phase, before any confirm run exists. A screen run is only for
comparing, so when the confirm run then disagrees ("met the target" on the screen, -3.86 dB at confirm) the final report may not keep the screen text
as its conclusion. `restate_entry` takes one report entry for an investigation and, when every confirm result is short of the target:

  * keeps each screen-phase text as history (`screen_summary`, `screen_reason`), labelled as such;
  * makes the confirm outcome the conclusion, written by code from the stored comparison with its evidence codes;
  * rewrites a suspect whose reason still says the target was met, so "not tested because the target was met" no longer stands.

It is a pure function over the report entry, so it fixes both the live investigation (called after each confirm) and older stored reviews when
a report is built; nothing stored is edited by it. It does nothing when a confirm run met the target or when there is no confirm result.
"""
from __future__ import annotations

import re

from . import verdict
from .number_check import extract, sentence_spans

_SIMULATED = re.compile(r'simulat|in the model|modelled|modeled', re.I)
_S11_WORDS = re.compile(r'\bS11\b|return loss|accepted power|reflect', re.I)
SCREEN_HISTORY = 'Screen phase (history, written before the confirm run)'
# a point "was not run because matching already reached the target on the screen run": a skip justified by a screen result the confirm run did not hold
_SKIP_FOR_TARGET = re.compile(r'\b(?:not|never|no|n\'t|skipped|omitted)\b[^.;]*?\b(?:because|since|as|so|given|due\s+to)\b[^.;]*?\b(?:reach(?:ed|es)?|met|meets|hit|achiev(?:ed|es)?|satisf(?:ied|ies))\b[^.;]*?\b(?:target|goal|-?\d+\s*dB)\b'
                              r"|\b(?:already|had|has)\s+(?:reach(?:ed)?|met|hit|achiev(?:ed)?)\s+(?:the\s+|its\s+)?(?:[-−]?\d+\s*dB\s+)?(?:target|goal)\b", re.I)
_BECAUSE_TAIL = re.compile(r'\s*(?:[,;]\s*)?\b(?:because|since|as|so|given|due\s+to)\b[^.]*$', re.I)


def simulated_problem(texts: dict[str, str]) -> str | None:
    """A paragraph that quotes an S11 or accepted-power number must say the number is simulated. Returns the message, or None."""
    for name, text in texts.items():
        for paragraph in re.split(r'\n\s*\n', text or ''):
            if _S11_WORDS.search(paragraph) and any(q.dimension in ('dB', 'percent') for q in extract(paragraph)) and not _SIMULATED.search(paragraph):
                quote = paragraph.strip()
                quote = quote if len(quote) <= 120 else quote[:117] + '...'
                return (f'In {name}: the paragraph "{quote}" quotes an S11 or accepted-power number without saying it is simulated. '
                        'Every such number comes from a simulation with an assumed stackup and ideal parts; write "simulated" (or "in simulation") in that paragraph.')
    return None


def _outcome_phrase(conf: dict) -> str:
    hold = conf.get('hold') or conf.get('outcome')
    return {
        'held': 'the improvement held', 'held_smaller': 'the improvement held but the screen overstated it',
        'held_after_retune': "the screen's own parts did not hold, re-tuning at confirm quality still improves it", 'not_held': 'the screen improvement did not hold',
        'unconfirmed': 'the base was not confirmed',
    }.get(hold, str(hold or conf.get('outcome')).replace('_', ' '))


def confirm_text(conf: dict, target_db: float | None = None, *, label: str = '') -> str:
    """One confirm comparison as a sentence for the conclusion, with the evidence codes of its numbers. Always says the target is not met (callers use it only then)."""
    base, best = conf.get('base') or {}, conf.get('best') or {}
    target = f'the {target_db:g} dB target' if target_db is not None else 'the target'
    head = f'{label}Confirm run (simulated): {_outcome_phrase(conf)}.'
    if conf.get('headline_text'):          # a board simulation (power, thermal, signal): its own numbers, written by code from the stored comparison
        return f'{head} {conf["headline_text"]}'
    if base.get('band_worst_s11_db') is not None and best.get('band_worst_s11_db') is not None:
        gain = conf.get('best_gain_db')
        return (f'{head} Worst in-band S11 goes from {base["band_worst_s11_db"]} dB ({conf.get("base_evidence") or "base"}) to {best["band_worst_s11_db"]} dB ({best.get("evidence") or "best candidate"})'
                + (f', a gain of {gain} dB' if gain is not None else '') + f', and {target} is not met.')
    return f'{head} {target[0].upper() + target[1:]} is not met at confirm quality.'


def confirmations_of(entry: dict) -> list[dict]:
    rounds = entry.get('refine_rounds') or []
    return [c for c in [entry.get('confirmation'), *[(r or {}).get('confirmation') for r in rounds]] if isinstance(c, dict) and c.get('outcome')]


def _has_target_claim(text: str | None) -> bool:
    return any(verdict.claims_target_met(text[a:b]) or _SKIP_FOR_TARGET.search(text[a:b]) for a, b in sentence_spans(text or ''))


def _tested_points(suspect: dict) -> int:
    return len({e.get('hash') or e.get('ref') for e in suspect.get('experiments') or [] if e.get('kind') in ('whatif', 'matching') and e.get('ok')})


def _without_stale_reason(text: str) -> str:
    """`text` with the screen-phase justification cut off ("..., a second point was not run because matching already reached the target on the screen run" keeps
    "a second point was not run"). A sentence that is only the stale claim is dropped."""
    kept = []
    for a, b in sentence_spans(text):
        sentence = text[a:b].strip()
        if verdict.claims_target_met(sentence) or _SKIP_FOR_TARGET.search(sentence):
            bare = sentence.rstrip(' .')
            cut = _BECAUSE_TAIL.sub('', bare).rstrip(' ,;')
            if cut and cut != bare and not verdict.claims_target_met(cut) and not _SKIP_FOR_TARGET.search(cut) and re.search(r'\bnot\s+(?:run|tested|tried|done|needed)\b', cut, re.I):
                kept.append(cut + '.')
            continue
        kept.append(sentence if sentence.endswith(('.', '!', '?')) else sentence + '.')
    return ' '.join(kept)


def restate_entry(entry: dict, target_db: float | None = None, confirmations: list[dict] | None = None) -> dict:
    """The report entry of one investigation with its conclusions following the confirm result. Returns `entry` itself when nothing changes."""
    confs = confirmations if confirmations is not None else confirmations_of(entry)
    confs = [c for c in confs if isinstance(c, dict) and c.get('outcome')]
    if not confs or any(verdict.meets_target(c) for c in confs):
        return entry
    outcome = entry.get('outcome') if isinstance(entry.get('outcome'), dict) else None
    best = min((c for c in confs if (c.get('best') or {}).get('band_worst_s11_db') is not None), key=lambda c: c['best']['band_worst_s11_db'], default=confs[-1])
    rounds = [confirm_text(c, target_db, label=('Refine round: ' if i and len(confs) > 1 else ('First attempt. ' if len(confs) > 1 else ''))) for i, c in enumerate(confs)]
    conclusion = ' '.join(rounds)
    out = dict(entry)
    if outcome is not None:
        screen = outcome.get('screen_summary') or outcome.get('summary')
        new_outcome = {**outcome, 'confirm_outcome': best.get('outcome'), 'confirm_label': verdict.OUTCOME_LABEL.get(best.get('outcome'), str(best.get('outcome')).replace('_', ' ')),
                       'summary': conclusion}
        if screen and screen != conclusion:
            new_outcome['screen_summary'] = screen
        out['outcome'] = new_outcome
    winner = (outcome or {}).get('winner')
    first = confs[0]
    suspects = []
    for suspect in entry.get('suspects') or []:
        suspect = dict(suspect)
        original = suspect.get('screen_reason') or suspect.get('reason')
        refs = {e.get('ref') for e in suspect.get('experiments') or []}
        owner = bool(winner) and winner in refs
        stale = _has_target_claim(original) or bool(re.search(r'target\s+was\s+met|further\s+optimi[sz]ing\s+is\s+not\s+allowed', original or '', re.I))
        if owner:
            suspect['reason'] = confirm_text(first, target_db)
            if first.get('hold') == 'not_held' and suspect.get('status') == 'confirmed':
                suspect['screen_status'] = suspect.get('screen_status') or 'confirmed'
                suspect['status'] = 'inconclusive'
        elif stale and _tested_points(suspect):
            # tested at some points: keep what was tested, drop the screen-phase reason for skipping the rest, and say the target is not met at confirm quality
            kept = _without_stale_reason(original)
            miss = (f'it is simulated short of {"the " + format(target_db, "g") + " dB" if target_db is not None else "the"} target'
                    + (f' (worst in-band S11 {best["best"]["band_worst_s11_db"]} dB, {best["best"].get("evidence")})' if (best.get('best') or {}).get('band_worst_s11_db') is not None else ''))
            suspect['reason'] = (f'{kept} ' if kept else '') + f'The reason given at the screen phase for not testing further (the target was reached on the screen run) did not survive the confirm run: {miss}. The untested points stay open.'
        elif stale:
            suspect['reason'] = (f'Not settled. The reason given at the screen phase (the target was met) did not survive the confirm run: it is simulated short of {"the " + format(target_db, "g") + " dB" if target_db is not None else "the"} target'
                                 + (f' (worst in-band S11 {best["best"]["band_worst_s11_db"]} dB, {best["best"].get("evidence")})' if (best.get('best') or {}).get('band_worst_s11_db') is not None else '')
                                 + '. This suspect was not tested and is not ruled out.')
        else:
            suspects.append(suspect)
            continue
        if original and original != suspect['reason']:
            suspect['screen_reason'] = original
        suspects.append(suspect)
    out['suspects'] = suspects
    out['restated'] = True
    return out


def entry_from_rows(store, inv: dict, index: int) -> dict:
    """The report entry of a stored investigation: its suspects with their experiments, the outcome and the confirm comparisons."""
    suspects = []
    for s in store.list_suspects(inv['id']):
        data = s.get('data') or {}
        conclusion = s.get('conclusion') or {}
        suspects.append({'code': data.get('code') or f'S{s["rank"]}', 'title': s['title'], 'status': s['status'], 'reason': conclusion.get('reason'),
                         'experiments': data.get('experiments') or [],
                         **({'screen_reason': conclusion['screen_reason']} if conclusion.get('screen_reason') else {}),
                         **({'screen_status': conclusion['screen_status']} if conclusion.get('screen_status') else {})})
    outcome = inv.get('outcome')
    return {'code': f'I{index}', 'question': inv['question'], 'status': inv['status'], 'outcome': outcome, 'suspects': suspects,
            'confirmation': outcome.get('confirmation') if isinstance(outcome, dict) else None,
            'refine_rounds': (outcome.get('refine_rounds') or []) if isinstance(outcome, dict) else []}


def add_caveats(entry: dict) -> dict:
    """A suspect ruled out from a single test point says so in the report (new reviews cannot do this: conclude_suspect wants two points). Nothing stored changes."""
    out = None
    for suspect in entry.get('suspects') or []:
        points = {e.get('hash') or e.get('ref') for e in suspect.get('experiments') or [] if e.get('kind') in ('whatif', 'matching') and e.get('ok')}
        if suspect.get('status') == 'ruled_out' and len(points) < 2:
            refs = ', '.join(str(e.get('ref')) for e in suspect.get('experiments') or [] if e.get('kind') in ('whatif', 'matching') and e.get('ok')) or 'none'
            suspect['caveat'] = f'Ruled out from {len(points)} test point ({refs}). One point does not show a trend, so read this as "not shown to matter", not as excluded.'
            out = entry
    return entry if out is None else out


def entries(store, review_id: str, target_db: float | None = None) -> list[dict]:
    """The investigations of a review as the report shows them: screen text kept as history, the confirm result as the conclusion."""
    out = []
    for index, inv in enumerate(store.list_investigations(review_id), start=1):
        entry = entry_from_rows(store, inv, index)
        sim_kind = (entry['outcome'] or {}).get('sim_kind') if isinstance(entry.get('outcome'), dict) else None
        out.append(add_caveats(restate_entry(entry, None if sim_kind else target_db)))
    return out
