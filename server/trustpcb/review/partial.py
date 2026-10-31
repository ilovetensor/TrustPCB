"""The partial win: a confirm-quality improvement of at least 3 dB in the band that still misses the target.

A confirmation (see `investigate._compare_with_screen`) carries `best`: the best confirm-quality candidate, which includes the
free re-tune of the matching. When that candidate gains `PARTIAL_WIN_DB` or more over the confirmed base and the target is still
missed, the outcome is `improved_short_of_target`. It is not a plain failure: the finding must name the best parts as a concrete
recommendation, give the accepted power before and after, say the target is not met, and cite the confirm evidence rows. The report
leads with it. All numbers here come from stored evidence; nothing is computed from a model.
"""
from __future__ import annotations

import re

from .budgets import PARTIAL_WIN_DB
from .number_check import extract, make_quantity

OUTCOME = 'improved_short_of_target'
NOT_MET = re.compile(r"\b(?:not\s+met|not\s+meet\w*|does\s+not\s+(?:meet|reach|hit)|doesn't\s+(?:meet|reach|hit)|do\s+not\s+meet|short\s+of\s+(?:the\s+)?target|"
                     r"(?:miss|misses|missed)\s+(?:the\s+)?target|falls?\s+short|fell\s+short|target\s+(?:is\s+)?(?:still\s+)?(?:not|missed)|below\s+the\s+target|"
                     r"(?:target|goal)\s+is\s+not|still\s+(?:short|misses|below))", re.I)
MODE_TEXT = {'free_retune': 'matching re-tuned freely at confirm quality', 'screen_parts_locked': "the screen's matching parts, checked at confirm quality",
             'geometry_only': "the geometry change with the board's own matching parts"}


def best_candidate(rows: list[tuple]) -> dict | None:
    """The best of several confirm-quality candidates, judged by the worst S11 in the band, then the mean.

    Each row is (mode, ref, evidence code, metrics, parts, ops, meets_target).
    """
    scored = []
    for mode, ref, code, metrics, parts, ops, meets in rows:
        if not metrics or metrics.get('band_worst_s11_db') is None:
            continue
        mean = metrics.get('band_mean_s11_db')
        scored.append((round(metrics['band_worst_s11_db'], 2), mean if mean is not None else 0.0, mode, ref, code, metrics, parts, ops, meets))
    if not scored:
        return None
    worst, _, mode, ref, code, metrics, parts, ops, meets = min(scored, key=lambda r: (r[0], r[1]))
    return {
        'mode': mode, 'ref': ref, 'evidence': code, 'ops': list(ops or []),
        'parts': [{k: p.get(k) for k in ('ref', 'position', 'kind', 'value', 'was', 'now')} for p in parts or []],
        'band_worst_s11_db': worst, 'band_mean_s11_db': None if metrics.get('band_mean_s11_db') is None else round(metrics['band_mean_s11_db'], 2),
        'accepted_power_worst_pct': metrics.get('band_worst_accepted_power_pct'), 'accepted_power_mean_pct': metrics.get('band_mean_accepted_power_pct'),
        'meets_target': bool(meets),
    }


def parts_text(best: dict) -> str:
    """The best candidate as one concrete instruction for the engineer, to apply in their own tool."""
    bits = []
    for op in best.get('ops') or []:
        if op.get('op') == 'adjust_antenna_length' and isinstance(op.get('delta_mm'), (int, float)):
            bits.append(f'{"shorten" if op["delta_mm"] < 0 else "lengthen"} the antenna by {abs(op["delta_mm"]):g} mm')
        elif op.get('op'):
            bits.append(str(op['op']).replace('_', ' '))
    for p in best.get('parts') or []:
        was = str(p.get('was') or '').replace(' (fitted)', '')
        bits.append(f'{p.get("ref")}: {was} to {p.get("now")}' + (f' ({p["position"]})' if p.get('position') else ''))
    return '; '.join(bits) or 'no change to the board'


def pick(confirmations: list[dict]) -> dict | None:
    """From the confirmations of one investigation (first attempt, then refine rounds), the partial win to report, or None.

    It is the best candidate over all of them, provided it gains `PARTIAL_WIN_DB` or more over the confirmed base and no confirmation met the target.
    """
    confirmations = [c for c in confirmations if c]
    if not confirmations or any(c.get('meets_target') or ((c.get('matching_at_confirm') or {}).get('free') or {}).get('meets_target') for c in confirmations):
        return None
    bests = [c for c in confirmations if c.get('best') and c.get('base')]
    if not bests:
        return None
    top = min(bests, key=lambda c: (round(c['best']['band_worst_s11_db'], 2), c['best'].get('band_mean_s11_db') or 0.0))
    gain = top.get('best_gain_db')
    if gain is None or gain < PARTIAL_WIN_DB or top['best'].get('meets_target'):
        return None
    return {'outcome': OUTCOME, 'best': top['best'], 'base': top['base'], 'gain_db': gain, 'statement': top.get('statement'),
            'base_evidence': top.get('base_evidence'), 'comparison_evidence': top.get('comparison_evidence'), 'winner': top.get('winner')}


def facts_text(win: dict, target_db: float | None = None) -> str:
    """The numbers of a partial win in plain sentences, taken from the stored comparison."""
    best, base = win['best'], win['base']
    out = (f'At confirm quality the best candidate ({MODE_TEXT.get(best.get("mode"), best.get("mode"))}) improves the worst S11 in the band from {base["band_worst_s11_db"]} dB to '
           f'{best["band_worst_s11_db"]} dB, a gain of {win["gain_db"]} dB.')
    if base.get('accepted_power_worst_pct') is not None and best.get('accepted_power_worst_pct') is not None:
        out += (f' Accepted power (a mismatch figure only, not radiated power or efficiency) at the worst point of the band goes from {base["accepted_power_worst_pct"]} % to '
                f'{best["accepted_power_worst_pct"]} %')
        if base.get('accepted_power_mean_pct') is not None and best.get('accepted_power_mean_pct') is not None:
            out += f', and its mean across the band from {base["accepted_power_mean_pct"]} % to {best["accepted_power_mean_pct"]} %'
        out += '.'
    out += f' Parts to try, in your own tool: {parts_text(best)}.'
    out += f' The tuning target{f" of {target_db:g} dB" if target_db is not None else ""} is not met.'
    return out


def problems(finding: dict, win: dict, cited_codes: set[str]) -> list[str]:
    """What a finding about a partial win still lacks. Empty when it is complete."""
    text = ' '.join(str(finding.get(k) or '') for k in ('title', 'what', 'why_it_matters', 'recommendation'))
    best, base = win['best'], win['base']
    missing = []
    wanted = [c for c in (win.get('base_evidence'), best.get('evidence'), win.get('comparison_evidence')) if c]
    absent = [c for c in dict.fromkeys(wanted) if c not in cited_codes]
    if absent:
        missing.append('cite the confirm evidence rows ' + ', '.join(absent))
    parts = [p['ref'] for p in best.get('parts') or [] if p.get('ref')]
    unnamed = [r for r in parts if not re.search(rf'\b{re.escape(r)}\b', text)]
    if unnamed:
        missing.append('name the best confirm-quality parts as the recommendation (' + parts_text(best) + '), including ' + ', '.join(unnamed))
    quantities = [q for q in extract(text) if q.dimension == 'percent']
    for label, value in (('before', base.get('accepted_power_worst_pct')), ('after', best.get('accepted_power_worst_pct'))):
        if value is None:
            continue
        wanted_q = make_quantity(value, '%')
        if not any(abs(q.base - wanted_q.base) <= max(q.tolerance, 0.05) for q in quantities):
            missing.append(f'state the accepted power {label} ({value} %, a mismatch figure only)')
    if not NOT_MET.search(text):
        missing.append('say plainly that the target is not met')
    return missing


def headline(investigations: list[dict], findings: list[dict] | None = None, target_db: float | None = None) -> dict | None:
    """What a report leads with when a partial win exists. `investigations` are the report's investigation entries (confirmation and refine rounds).

    Returns the win with its finding code and the facts as one paragraph, or None.
    """
    for inv in investigations or []:
        outcome = inv.get('outcome') if isinstance(inv.get('outcome'), dict) else {}
        confirmations = [inv.get('confirmation'), *[(r or {}).get('confirmation') for r in inv.get('refine_rounds') or []]]
        win = pick(confirmations)
        if win is None:
            continue
        code = outcome.get('finding')
        finding = next((f for f in findings or [] if f.get('code') == code), None)
        return {**win, 'investigation': inv.get('code'), 'finding': code, 'finding_title': finding and finding.get('title'),
                'parts_text': parts_text(win['best']), 'text': facts_text(win, target_db), 'target_db': target_db}
    return None
