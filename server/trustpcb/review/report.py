"""Reports: assembled by code from stored data, numbered revisions, frozen snapshots.

`create_revision` freezes everything a report shows into one JSON snapshot. Earlier snapshots are
never edited, so exported files and older revisions stay exactly as they were. The HTML is
self-contained (inline styles, inline SVG), the JSON is the snapshot itself, and the PDF is the HTML
printed by a headless browser when one is installed (see report_pdf.py for how that browser is locked down).
"""
from __future__ import annotations

import asyncio
import base64
import html
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from . import change
from . import evidence as ev
from . import partial, restate
from .compare import classify_findings, previous_context
from .report_css import CSS
from .report_pdf import html_to_pdf, pdf_slot

logger = logging.getLogger('trustpcb')

SEVERITY_ORDER = ('critical', 'high', 'medium', 'low', 'info')
CLASS_LABEL = {'measured': 'Measured', 'simulated': 'Simulated', 'opinion': 'Engineering opinion', 'not_tested': 'Not tested'}
END_STATE_TEXT = {
    'complete': 'The review ran to the end within its budgets.',
    'complete_with_limits': 'The review ended with a budget or limit reached; the sections below say which.',
    'agent_stopped': 'The agent stopped before finishing; code assembled this report from what was recorded.',
    'canceled': 'The review was stopped by the user; code assembled this report from what was recorded.',
    'pipeline_failed': 'The checks before the agent could not finish; this report shows what was available.',
}
EVIDENCE_SHOWN = 8
SIM_NOTE = 'Simulated. Not a substitute for a real antenna measurement.'


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


# ---- building the snapshot ---------------------------------------------------------------------

def _evidence_public(row: dict) -> dict:
    values = dict(row.get('values') or {})
    keep_curve = values.get('quality') == 'confirm' or values.get('kind') != 'simulated'
    if not keep_curve:
        values.pop('curve', None)
    values.pop('run', None)
    return {'id': row['id'], 'code': row['code'], 'type': row['type'], 'source_tool': row['source_tool'], 'values': values, 'created_at': row['created_at']}


def _finding_public(store, finding: dict, codes: dict[str, str]) -> dict:
    history = store.finding_history(finding['id'])
    out = {k: finding.get(k) for k in ('id', 'code', 'fingerprint', 'title', 'severity', 'class', 'status', 'what', 'why_it_matters', 'recommendation',
                                      'marks', 'assumptions', 'not_tested', 'needs_recheck', 'source', 'thread_id', 'dismissed_reason', 'superseded_by')}
    out['evidence'] = [codes.get(i, i) for i in finding.get('evidence_ids') or []]
    out['history'] = [{'source': h['source'], 'reason': h['reason'], 'at': h['created_at'],
                       'before': {k: (h['before'] or {}).get(k) for k in ('title', 'severity', 'what', 'recommendation', 'status')} if h['before'] else None}
                      for h in history if h['before']]
    return out


def _investigations(store, review_id: str) -> list[dict]:
    """The investigation story. Conclusions written at the screen phase are kept as history; when the confirm result is short of the target it is the conclusion
    (restate.py), so the report never says "met the target" over a confirm run that missed it."""
    review = store.get_review(review_id) or {}
    return restate.entries(store, review_id, ev.target_db(review.get('goal') or {}, (review.get('summary') or {}).get('rf') or {}))


def best_confirmed(*rows: dict | None) -> dict | None:
    """The confirm-quality row with the lowest worst in-band S11, so the report's best change is the one the headline names, not only the screen's parts."""
    candidates = [r for r in rows if r]
    worst = lambda r: ((r['values'].get('metrics') or {}).get('band_worst_s11_db'))
    scored = [r for r in candidates if isinstance(worst(r), (int, float))]
    return min(scored, key=worst) if scored else (candidates[0] if candidates else None)


def _rf_section(store, review_id: str, summary: dict, evidence_rows: list[dict]) -> dict:
    rf = summary.get('rf') or {}
    sims = [r for r in evidence_rows if r['type'] in (ev.SIMULATION, ev.CONFIRM)]
    section: dict[str, Any] = {'status': 'tested' if sims else 'not_tested', 'reason': None if sims else (rf.get('reason') or 'No simulation was run.'),
                               'kind': rf.get('kind'), 'target_db': rf.get('target_s11_db'), 'band_hz': rf.get('band_hz'), 'note': SIM_NOTE}
    if not sims:
        return section
    confirm = [r for r in sims if r['type'] == ev.CONFIRM and (r['values'].get('trust') or {}).get('status') == 'ok']
    base_confirm = next((r for r in confirm if r['values'].get('label') == 'confirm base'), None)
    winner_confirm = next((r for r in confirm if r['values'].get('label') == 'confirm winner'), None)
    shown = lambda r: r and {'code': r['code'], 'label': r['values'].get('label'), 'quality': r['values'].get('quality'), 'trust': r['values'].get('trust'),
                             'measurements': r['values'].get('measurements'), 'scenario_hash': r['values'].get('scenario_hash'), 'curve': r['values'].get('curve')}
    calc = [r for r in evidence_rows if r['type'] == ev.CALCULATION and (r['values'].get('trust') or {}).get('status') == 'ok' and r['values'].get('quality') == 'confirm']
    locked = next((r for r in calc if r['values'].get('label') == 'confirm matching, parts locked'), None)
    free = next((r for r in calc if r['values'].get('label') == 'confirm matching, free tuning'), None)
    section['confirm_base'], section['confirm_winner'] = shown(base_confirm), shown(best_confirmed(winner_confirm, free if free and free['values'].get('curve') else None))
    section['confirm_matching'] = {'locked': shown(locked), 'free': shown(free)} if (locked or free) else None
    best = locked or winner_confirm or base_confirm
    if best:
        metrics = best['values'].get('metrics') or {}
        goal = (store.get_review(review_id) or {}).get('goal') or {}
        section['meets_target'] = ev.meets_target(metrics, goal, rf)
    else:
        section['meets_target'] = None
        section['note'] = SIM_NOTE + ' Only screen runs exist, so no absolute result is shown.'
        section['screen_runs'] = [shown(r) | {'curve': None} for r in sims[:12]]
    return section


def _not_tested(summary: dict, review: dict, rf_section: dict, candidates_new: int) -> list[str]:
    items = list(summary.get('not_tested') or [])
    if rf_section['status'] == 'not_tested':
        items.append(f'RF simulation: {rf_section["reason"]}')
    for phase in summary.get('phases') or []:
        for skipped in phase.get('skipped') or []:
            items.append(f'Phase {skipped["phase"]} ({skipped["name"]}) was skipped: {skipped["reason"]}')
    if candidates_new:
        items.append(f'{candidates_new} analyzer or KiCad result(s) were not reviewed by the agent and are listed as Unreviewed.')
    groups = summary.get('unreviewed_groups') or []
    if groups:
        items.append('Unreviewed by rule: ' + ', '.join(f'{g["rule"]} ({g["count"]})' for g in groups[:10]) + '.')
    waived = summary.get('coverage_waived')
    if waived:
        items.append(f'Phase 3 coverage was cut short: {waived.get("reason", "a budget ended")}.')
    for group in summary.get('auto_grouped') or []:
        items.append(f'{group["count"]} info-level {group["rule"]} result(s) were grouped by code and not reviewed one by one.')
    for limit in summary.get('limits') or []:
        if limit['rule'] in ('budget_wall', 'hard_deadline', 'model_turns', 'silence_watchdog', 'loop'):
            items.append(f'The review stopped early: {limit["detail"]}.')
    return list(dict.fromkeys(items))


def _method(store, review: dict, evidence_rows: list[dict]) -> dict:
    baseline = {}
    path = store.version_dir(review['version_id']) / 'artifacts' / 'baseline.json'
    if path.is_file():
        try:
            baseline = json.loads(path.read_text())
        except ValueError:
            baseline = {}
    scenarios = []
    for r in evidence_rows:
        v = r['values'] or {}
        if r['type'] in (ev.SIMULATION, ev.CONFIRM) and v.get('scenario_hash'):
            scenarios.append({'ref': v.get('ref'), 'hash': v['scenario_hash'], 'quality': v.get('quality'), 'ops': v.get('whatif_ops'), 'material': v.get('material'),
                              'solve_s': v.get('solve_s'), 'trust': (v.get('trust') or {}).get('status')})
    profile = review.get('profile') or {}
    return {'kicad_version': baseline.get('kicad_version'), 'default_rules': baseline.get('default_rules'), 'runner': {k: profile.get(k) for k in ('name', 'runner', 'model')},
            'depth': review.get('depth'), 'scenarios': scenarios, 'note': 'Screen runs compare a change with the base at the same mesh. Confirm runs use a finer mesh and longer time.'}


def build_snapshot(store, review_id: str, *, cause: str, incomplete: bool = False, previous: dict | None = None, number: int = 1) -> dict:
    review = store.get_review(review_id)
    version = store.get_version(review['version_id'])
    project = store.get_project(review['project_id'])
    summary = review.get('summary') or {}
    rows = store.list_evidence(review_id)
    codes = {r['id']: r['code'] for r in rows}
    findings = [_finding_public(store, f, codes) for f in store.list_findings(review_id)]
    live = [f for f in findings if f['status'] in ('open', 'needs_recheck')]
    counts = store.severity_counts(review_id)
    rf = _rf_section(store, review_id, summary, rows)
    candidate_rows = store.list_candidates(review_id)
    unreviewed = sum(1 for c in candidate_rows if c['review_state'] in ('new', 'unreviewed'))
    snapshot = {
        'revision': number, 'cause': cause, 'incomplete': incomplete, 'created_at': now(),
        'cover': {'project': project['name'], 'version': {'id': version['id'], 'number': version['number'], 'label': version.get('label')},
                  'review_id': review_id, 'depth': review.get('depth'), 'end_state': review.get('end_state'), 'end_reason': (summary.get('end') or {}).get('reason'),
                  'end_detail': (summary.get('end') or {}).get('detail'), 'profile': {k: (review.get('profile') or {}).get(k) for k in ('name', 'runner', 'model')},
                  'goal': review.get('goal') or {}},
        'summary': summary.get('summary'),
        'overview': {'board': summary.get('board'), 'counts': counts, 'brief': summary.get('brief'),
                     'top_risks': [{'code': f['code'], 'severity': f['severity'], 'title': f['title']} for f in sorted(live, key=lambda f: SEVERITY_ORDER.index(f['severity']))[:5]]},
        'rf': rf,
        'investigations': _investigations(store, review_id),
        'findings': findings, 'evidence': [_evidence_public(r) for r in rows],
        'candidates': {'total': len(candidate_rows), 'unreviewed': unreviewed,
                       'by_state': {s: sum(1 for c in candidate_rows if c['review_state'] == s) for s in {c['review_state'] for c in candidate_rows}}},
        'version_changes': summary.get('version_changes'),
        'review_work': {'dismissals': summary.get('dismissals') or [], 'auto_grouped': summary.get('auto_grouped') or [],
                        'region_views': len(summary.get('region_views') or []), 'unreviewed_groups': summary.get('unreviewed_groups') or []},
        'assumptions': [{'name': a['name'], 'value': a['value'], 'source': a['source'], 'note': a.get('note')} for a in store.list_assumptions(review_id)],
        'not_tested': [], 'limits': summary.get('limits') or [], 'budget': review.get('budget') or {},
        'method': _method(store, review, rows),
    }
    win = partial.headline(snapshot['investigations'], findings, ev.target_db(review.get('goal') or {}, summary.get('rf') or {}))
    snapshot['version_change'] = recheck_change(store, review_id)
    snapshot['headline'] = ({**snapshot['version_change'], 'kind': 'version_change', 'partial_win': win} if snapshot['version_change']
                            else ({**win, 'kind': 'partial_win'} if win else None))
    snapshot['not_tested'] = _not_tested(summary, review, rf, unreviewed)
    snapshot['changes_since_last'] = diff_snapshots(previous, snapshot) if previous else None
    return snapshot


def diff_snapshots(old: dict, new: dict) -> dict:
    """What changed between two revisions, finding by finding."""
    before = {f['code']: f for f in old.get('findings', [])}
    out: dict[str, Any] = {'from_revision': old.get('revision'), 'new_findings': [], 'updated': [], 'superseded': [], 'dismissed': [], 'needs_recheck': [],
                           'reopened': [], 'assumptions': [], 'not_tested_added': []}
    for f in new.get('findings', []):
        prior = before.get(f['code'])
        brief = {'code': f['code'], 'title': f['title'], 'severity': f['severity']}
        if prior is None:
            out['new_findings'].append({**brief, 'found_in_followup': f.get('source') == 'followup'})
            continue
        if f['status'] != prior['status']:
            bucket = {'superseded': 'superseded', 'dismissed': 'dismissed', 'needs_recheck': 'needs_recheck', 'open': 'reopened'}.get(f['status'])
            if bucket:
                out[bucket].append({**brief, 'superseded_by': f.get('superseded_by'), 'reason': f.get('dismissed_reason')})
        changed = [k for k in ('title', 'severity', 'what', 'why_it_matters', 'recommendation', 'class') if f.get(k) != prior.get(k)]
        if changed and f['status'] == prior['status']:
            out['updated'].append({**brief, 'fields': changed, 'was': {k: prior.get(k) for k in changed}})
    old_assumptions = {(a['name'], str(a['value'])) for a in old.get('assumptions', [])}
    for a in new.get('assumptions', []):
        if (a['name'], str(a['value'])) not in old_assumptions:
            out['assumptions'].append(a)
    out['not_tested_added'] = [t for t in new.get('not_tested', []) if t not in old.get('not_tested', [])]
    out['empty'] = not any(out[k] for k in ('new_findings', 'updated', 'superseded', 'dismissed', 'needs_recheck', 'reopened', 'assumptions', 'not_tested_added'))
    return out


def create_revision(store, review_id: str, *, cause: str, incomplete: bool = False) -> dict:
    """Freeze the current state of a review as the next report revision."""
    latest = store.latest_report_revision(review_id)
    number = (latest['number'] + 1) if latest else 1
    snapshot = build_snapshot(store, review_id, cause=cause, incomplete=incomplete, previous=latest['snapshot'] if latest else None, number=number)
    return store.add_report_revision(review_id, cause, snapshot)


def recheck_change(store, review_id: str) -> dict | None:
    """The as-built result against the previous version's review, with the prediction it made; None for a first version or when either has no confirmed base."""
    review = store.get_review(review_id)
    previous = previous_context(store, review['version_id'])
    return change.version_change(store, review_id, previous['review']['id']) if previous else None


def version_changes(store, review_id: str, new_coverage: set[str] | None = None) -> dict | None:
    """Every finding against the previous version's review (fixed, improved, still present, worse, new), or None for a first version."""
    review = store.get_review(review_id)
    previous = previous_context(store, review['version_id'])
    if not previous:
        return None
    out = classify_findings(store, previous['findings'], store.list_findings(review_id), new_coverage=new_coverage, version_change=recheck_change(store, review_id),
                            old_review_id=previous['review']['id'], new_review_id=review_id)
    out['against'] = {'version_number': previous['version_number'], 'review_id': previous['review']['id']}
    return out


# ---- HTML ------------------------------------------------------------------------------------------



def esc(value: Any) -> str:
    return html.escape('' if value is None else str(value), quote=True)


def pill(kind: str, label: str | None = None) -> str:
    return f'<span class="pill {esc(kind)}">{esc(label or kind)}</span>'


def sev_pill(sev: str) -> str:
    return pill(f'sev-{sev}', sev.capitalize())


def class_pill(cls: str) -> str:
    return pill(f'cls-{cls}', CLASS_LABEL.get(cls, cls))


METRIC_NAMES = {
    'resonance': 'Deepest dip', 's11_min': 'Lowest S11', 's11_band_worst': 'Worst S11 in the band', 's11_band_mean': 'Mean S11 in the band',
    'band_worst_accepted_power_pct': 'Accepted power, worst point of the band', 'band_mean_accepted_power_pct': 'Accepted power, mean across the band',
    'antenna_only_band_worst_accepted_power_pct': 'Accepted power, worst point of the band, antenna alone',
    'antenna_only_band_mean_accepted_power_pct': 'Accepted power, mean across the band, antenna alone',
    'as_built_band_worst_accepted_power_pct': 'Accepted power, worst point of the band, as built',
    'as_built_band_mean_accepted_power_pct': 'Accepted power, mean across the band, as built',
    'antenna_only_band_worst_s11': 'Worst S11 in the band, antenna alone', 'antenna_only_band_mean_s11': 'Mean S11 in the band, antenna alone',
    'as_built_band_worst_s11': 'Worst S11 in the band, as built', 'as_built_band_mean_s11': 'Mean S11 in the band, as built',
    'band_centre_resistance': 'Resistance at the band centre', 'band_centre_reactance': 'Reactance at the band centre', 'band_centre_frequency': 'Band centre frequency',
    'bandwidth': 'Bandwidth below -10 dB', 'peak_directivity': 'Peak directivity', 'pattern_frequency': 'Frequency of the pattern and efficiency', 'radiation_efficiency': 'Radiation efficiency',
    'input_resistance': 'Input resistance', 'input_reactance': 'Input reactance', 'first_series_resonance': 'First series resonance',
    'delta_s11': 'Change in lowest S11', 'delta_band_mean': 'Change in mean S11 in the band',
    'delta_band_worst_accepted_power': 'Change in accepted power, worst point of the band', 'delta_band_mean_accepted_power': 'Change in accepted power, mean across the band',
    'delta_first_series_resonance': 'Change in first series resonance', 'delta_resonance': 'Change in deepest dip', 'delta_bandwidth': 'Change in bandwidth',
    'delta_peak_directivity': 'Change in peak directivity',
}


def metric_name(key: str) -> str:
    """The name a reader sees for a stored measurement key. Only the display changes; an unknown key is shown as plain words."""
    key = str(key)
    if key in METRIC_NAMES:
        return METRIC_NAMES[key]
    dip = re.fullmatch(r's11_dip_(\d+)(_depth)?', key)
    if dip:
        return f'S11 dip {dip.group(1)} {"depth" if dip.group(2) else "frequency"}'
    words = key.replace('_', ' ').strip()
    words = re.sub(r'\bs11\b', 'S11', words, flags=re.I)
    return (words[:1].upper() + words[1:]) if words else key


def measurement_text(item: dict) -> str:
    return f'{item.get("value")} {item.get("unit", "")}'.strip()


def curve_svg(curve: dict, band: list | None, target: float | None) -> str:
    freqs, s11 = curve.get('freq_hz') or [], curve.get('s11_db') or []
    if len(freqs) < 2 or len(freqs) != len(s11):
        return ''
    w, h, left, bottom = 560, 220, 46, 28
    lo, hi = min(freqs), max(freqs)
    floor = min(-30.0, min(s11) - 2)
    x = lambda f: left + (f - lo) / (hi - lo or 1) * (w - left - 8)
    y = lambda d: 8 + (0 - d) / (0 - floor) * (h - bottom - 8)
    points = ' '.join(f'{x(f):.1f},{y(min(d, 0)):.1f}' for f, d in zip(freqs, s11))
    parts = [f'<svg class="board" viewBox="0 0 {w} {h}" role="img" aria-label="S11 against frequency">']
    if band:
        parts.append(f'<rect x="{x(max(band[0], lo)):.1f}" y="8" width="{max(0, x(min(band[1], hi)) - x(max(band[0], lo))):.1f}" height="{h - bottom - 8}" fill="#22a3d6" opacity=".12"/>')
    if target is not None:
        parts.append(f'<line x1="{left}" x2="{w - 8}" y1="{y(target):.1f}" y2="{y(target):.1f}" stroke="#ff9f1a" stroke-dasharray="4 3"/>'
                     f'<text x="{w - 10}" y="{y(target) - 3:.1f}" text-anchor="end" font-size="10" fill="#7a4a00">target {target:g} dB</text>')
    parts.append(f'<polyline points="{points}" fill="none" stroke="#0e5a78" stroke-width="1.8"/>')
    parts.append(f'<line x1="{left}" x2="{left}" y1="8" y2="{h - bottom}" stroke="#8a929b"/><line x1="{left}" x2="{w - 8}" y1="{h - bottom}" y2="{h - bottom}" stroke="#8a929b"/>')
    parts.append(f'<text x="{left}" y="{h - 10}" font-size="10" fill="#5b6570">{lo / 1e9:.3g} GHz</text><text x="{w - 8}" y="{h - 10}" font-size="10" text-anchor="end" fill="#5b6570">{hi / 1e9:.3g} GHz</text>')
    parts.append(f'<text x="4" y="14" font-size="10" fill="#5b6570">0 dB</text><text x="4" y="{h - bottom}" font-size="10" fill="#5b6570">{floor:.0f}</text></svg>')
    return ''.join(parts)


def experiment_line(e: dict) -> str:
    """One experiment of a suspect: what it was, what kind, and how the in-band result moved against the base (screen: for comparing only)."""
    text = f'{e.get("ref")} {e.get("label")} ({e.get("kind")}, {e.get("quality")}){" - failed trust" if not e.get("ok") else ""}'
    if e.get('kind') == 'matching' and e.get('quality') == 'screen':
        text += ' - no solver time'
    delta = (e.get('delta') or {}).get('s11_db')
    if e.get('kind') in ('whatif', 'matching') and e.get('quality') == 'screen' and delta is not None:
        text += f': worst in-band S11 {abs(delta):.1f} dB {"better" if delta < 0 else "worse"} than the base (screen, for comparing only)'
    return esc(text)


def _cell(row: dict | None, key: str, unit: str) -> str:
    if not row or row.get(key) is None:
        return '<td class="muted">not run</td>' if row is None else '<td class="muted">-</td>'
    return f'<td class="n">{esc(row[key])} {esc(unit)}</td>'


def power_bars(caption: str, rows: list[tuple[str, Any]]) -> str:
    """Accepted power as bars, one per (label, percent). Empty when a value is missing."""
    if any(pct is None for _, pct in rows):
        return ''
    # The first row is the earlier value (hatched); the rest are the improved value (solid, with a marker at the end of the fill).
    bars = ''.join(f'<div><span>{esc(label)}</span><i><b class="{"before" if i == 0 else "now"}" style="width:{min(max(float(pct), 0), 100):g}%"></b></i><strong>{float(pct):g} %</strong></div>' for i, (label, pct) in enumerate(rows))
    return f'<div class="power"><p class="small muted">{esc(caption)}</p>{bars}</div>'


def render_headline(h: dict) -> str:
    """The partial win, first on the page: improved at confirm quality, target not met, with the best parts and the accepted power before and after."""
    best, base = h['best'], h['base']
    bars = power_bars('Accepted power, worst point of the band', [('Confirmed base', base.get('accepted_power_worst_pct')), ('Best confirm-quality parts', best.get('accepted_power_worst_pct'))])
    rows = [('Worst S11 in the band', f'{base.get("band_worst_s11_db")} dB', f'{best.get("band_worst_s11_db")} dB')]
    if base.get('accepted_power_worst_pct') is not None and best.get('accepted_power_worst_pct') is not None:
        rows.append(('Accepted power, worst point of the band', f'{base["accepted_power_worst_pct"]} %', f'{best["accepted_power_worst_pct"]} %'))
    if base.get('accepted_power_mean_pct') is not None and best.get('accepted_power_mean_pct') is not None:
        rows.append(('Accepted power, mean across the band', f'{base["accepted_power_mean_pct"]} %', f'{best["accepted_power_mean_pct"]} %'))
    table = '<table><tr><th></th><th>Confirmed base</th><th>Best confirm-quality parts</th></tr>' + ''.join(f'<tr><td>{esc(a)}</td><td class="n">{esc(b)}</td><td class="n">{esc(c)}</td></tr>' for a, b, c in rows) + '</table>'
    return ('<div class="card keep"><h2 style="margin-top:0">Improved, target not met</h2>'
            f'<p>{pill("cls-simulated", "partial win")} Confirm improves the worst S11 in the band by {esc(h.get("gain_db"))} dB, and the tuning target'
            f'{" of " + esc(format(h["target_db"], "g")) + " dB" if h.get("target_db") is not None else ""} is not met.</p>'
            f'<p><strong>Parts to try, in your own tool:</strong> {esc(h["parts_text"])}.</p>{bars}{table}'
            '<p class="small muted">Accepted power is 100 x (1 - |S11|^2) per frequency: a mismatch figure only. It is not radiated power or efficiency. '
            f'Simulated; not a substitute for a real antenna measurement.{(" See finding " + esc(h["finding"]) + ".") if h.get("finding") else ""}</p></div>')


def render_version_change(vc: dict) -> str:
    """The lead of a recheck: the as-built result against the previous version, whether it is consistent with the earlier prediction (same solver run) or the prediction held (separate run), and whether the target is met."""
    a, b, frm, to = vc['from_version'], vc['to_version'], vc['from'], vc['to']
    tone = {'improved': 'cls-measured', 'worse': 'sev-high', 'unchanged': 'cls-not_tested'}[vc['direction']]
    rows = [('Worst S11 across the band, as built', f'{frm["band_worst_s11_db"]:g} dB', f'{to["band_worst_s11_db"]:g} dB', f'{vc["gain_db"]:+g} dB')]
    if frm.get('band_mean_s11_db') is not None and to.get('band_mean_s11_db') is not None:
        rows.append(('Mean S11 across the band, as built', f'{frm["band_mean_s11_db"]:g} dB', f'{to["band_mean_s11_db"]:g} dB', f'{round(frm["band_mean_s11_db"] - to["band_mean_s11_db"], 2):+g} dB'))
    if frm.get('accepted_power_worst_pct') is not None and to.get('accepted_power_worst_pct') is not None:
        rows.append(('Accepted power, worst point of the band', f'{frm["accepted_power_worst_pct"]:g} %', f'{to["accepted_power_worst_pct"]:g} %', f'{vc["accepted_power_gain_worst_pct"]:+g} points'))
    if frm.get('accepted_power_mean_pct') is not None and to.get('accepted_power_mean_pct') is not None:
        rows.append(('Accepted power, mean across the band', f'{frm["accepted_power_mean_pct"]:g} %', f'{to["accepted_power_mean_pct"]:g} %', f'{vc["accepted_power_gain_mean_pct"]:+g} points'))
    prediction = vc.get('prediction_from_previous')
    if prediction:
        rows.append((f'Predicted by version {a}: {prediction["parts_text"]}', '', f'{prediction["band_worst_s11_db"]:g} dB predicted', f'{"separate confirm run" if vc.get("independent_check") else "same solver run"}: {to["band_worst_s11_db"]:g} dB'))
    table = (f'<table class="vc"><tr><th></th><th>Version {esc(a)}</th><th>Version {esc(b)}</th><th>Change</th></tr>'
             + ''.join(f'<tr><td>{esc(x)}</td><td class="n">{esc(y)}</td><td class="n">{esc(z)}</td><td class="n">{esc(w)}</td></tr>' for x, y, z, w in rows) + '</table>')
    badges = pill(tone, {'improved': 'improved', 'worse': 'worse', 'unchanged': 'unchanged'}[vc['direction']])
    if prediction:
        if vc.get('applied') is False:
            badges += ' ' + pill('cls-not_tested', 'Prediction not tested')
        elif vc.get('independent_check'):
            badges += ' ' + (pill('cls-simulated', 'Prediction held') if vc.get('held') else pill('sev-high', 'Prediction did not hold'))
        else:
            badges += ' ' + (pill('cls-simulated', 'Same solver run, new parts') if vc.get('consistent') else pill('sev-high', 'Same solver run, not consistent'))
    badges += ' ' + (pill('cls-measured', 'Target met') if vc['target_met'] else pill('sev-medium', 'Target not met'))
    bars = power_bars('Accepted power as built, worst point of the band', [(f'Version {a}', frm.get('accepted_power_worst_pct')), (f'Version {b}', to.get('accepted_power_worst_pct'))])
    return (f'<div class="card keep"><h2 style="margin-top:0">Compared with version {esc(a)}</h2><p>{badges}</p><p>{esc(vc["text"])}</p>{bars}{table}'
            '<p class="small muted">Accepted power is 100 x (1 - |S11|^2) per frequency: a mismatch figure only, not radiated power or efficiency. '
            f'Both results are confirm-quality simulations of the board as built (evidence {esc(frm.get("evidence"))} and {esc(to.get("evidence"))}). '
            + ('Version ' + esc(b) + ' was finalised from the same solver run as version ' + esc(a) + ', with its own matching parts: it is consistent with the prediction, not an independent result. ' if prediction and not vc.get('independent_check') and vc.get('applied') is not False else '')
            + 'Simulated, not a substitute for a real antenna measurement.</p></div>')


def render_confirmation(c: dict, title: str = 'Screen against confirm') -> str:
    """Screen against confirm: the honest comparison, with the matching re-tuned at confirm quality."""
    rows = c.get('rows') or {}
    tone = {'held': 'cls-measured', 'held_smaller': 'sev-medium', 'held_after_retune': 'sev-medium', 'not_held': 'sev-high', 'unconfirmed': 'cls-not_tested',
            'improved_short_of_target': 'cls-simulated'}.get(c.get('outcome'), '')
    winner = c.get('winner') or {}
    change = []
    if winner.get('ops'):
        change.append('what-if: ' + ', '.join(esc(json.dumps(o)) for o in winner['ops']))
    if winner.get('parts'):
        change.append('matching: ' + ', '.join(esc(f'{p.get("ref")} {p.get("was")} to {p.get("now")}') for p in winner['parts']))
    out = [f'<h3>{esc(title)}</h3><p>{pill(tone, str(c.get("outcome", "")).replace("_", " "))} {esc(c.get("statement"))}</p>',
           '<p class="small">Candidate change, tested in simulation (apply it in your own tool and re-measure): ' + ('; '.join(change) or 'none') + '. Screen columns are for comparing only; confirm columns are the result.</p>',
           '<table><tr><th></th><th>Base, screen</th><th>Base, confirm</th><th>Change, screen</th><th>Change, confirm</th></tr>']
    cols = [(rows.get('base') or {}).get('screen'), (rows.get('base') or {}).get('confirm'), (rows.get('winner') or {}).get('screen'), (rows.get('winner') or {}).get('confirm')]
    for label, key, unit in (('Worst S11 in the band', 'band_worst_s11_db', 'dB'), ('Mean S11 in the band', 'band_mean_s11_db', 'dB'), ('Deepest dip (descriptive)', 'deepest_dip_mhz', 'MHz'),
                             ('Depth of that dip', 'deepest_dip_db', 'dB'), ('Resistance at the band centre', 'centre_resistance_ohm', 'ohm'),
                             ('Reactance at the band centre', 'centre_reactance_ohm', 'ohm'), ('First series resonance', 'first_series_resonance_mhz', 'MHz')):
        out.append(f'<tr><td>{esc(label)}</td>' + ''.join(_cell(col, key, unit) for col in cols) + '</tr>')
    out.append('</table>')
    matching = c.get('matching_at_confirm') or {}
    if matching:
        out.append('<p class="small"><strong>Matching re-tuned at confirm quality</strong> (no solver time)</p><table><tr><th></th><th>Parts</th><th>Worst S11 in the band</th><th>Mean S11 in the band</th><th>Meets target</th></tr>')
        for key, label in (('locked', 'Screen\'s parts locked'), ('free', 'Free tuning')):
            m = matching.get(key)
            if m:
                parts = ', '.join(esc(f'{p.get("ref")} {p.get("was")} to {p.get("now")}') for p in m.get('parts') or []) or 'as built'
                out.append(f'<tr><td>{label}</td><td class="small">{parts}</td>{_cell(m, "band_worst_s11_db", "dB")}{_cell(m, "band_mean_s11_db", "dB")}<td>{"yes" if m.get("meets_target") else "no"}</td></tr>')
        out.append('</table>')
    return ''.join(out)


def render_html(snap: dict, images: dict | None = None) -> str:
    images = images or {}
    cover, overview, rf = snap['cover'], snap['overview'], snap['rf']
    out = [f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
           f'<title>{esc(cover["project"])} v{esc(cover["version"]["number"])} report r{esc(snap["revision"])}</title><style>{CSS}</style></head><body><main>']
    version = cover['version']
    out.append(f'<h1>{esc(cover["project"])}</h1><p class="meta">Version {esc(version["number"])}{" - " + esc(version["label"]) if version.get("label") else ""} - '
               f'report revision r{esc(snap["revision"])} - {esc(snap["created_at"][:10])} - {esc(cover["depth"])} review - runner {esc((cover["profile"] or {}).get("name") or "unknown")}'
               f'{" (" + esc(cover["profile"]["model"]) + ")" if (cover["profile"] or {}).get("model") else ""}</p>')
    out.append(f'<p>{pill("", (cover.get("end_state") or "unknown").replace("_", " "))} {esc(END_STATE_TEXT.get(cover.get("end_state"), ""))}'
               f'{(" " + esc(cover["end_detail"])) if cover.get("end_detail") else ""}</p>')
    if snap.get('incomplete'):
        out.append('<div class="notice">This revision ended before its run finished. Verified changes up to that point are included; the run is marked incomplete.</div>')
    if snap.get('changes_since_last') and not snap['changes_since_last'].get('empty'):
        out.append(render_changes(snap['changes_since_last'], snap['cause']))
    headline = snap.get('headline')
    if headline and headline.get('kind') == 'version_change':
        out.append(render_version_change(headline))
        if headline.get('partial_win'):
            out.append(render_headline(headline['partial_win']))
    elif headline:
        out.append(render_headline(headline))
    # 2 summary
    out.append('<h2>Summary</h2>')
    summary = snap.get('summary')
    if summary:
        out.append(f'<p>{esc(summary["text"])}</p>')
        if summary.get('source') == 'code':
            out.append('<p class="muted small">Written by code from the stored findings because the agent did not submit a summary.</p>')
        if summary.get('top_actions'):
            out.append('<h3>Top actions</h3><ul>' + ''.join(f'<li>{esc(a)}</li>' for a in summary['top_actions']) + '</ul>')
    else:
        out.append('<p class="muted">No summary is available.</p>')
    # 3 overview
    out.append('<h2>Board overview</h2>')
    board = overview.get('board') or {}
    if board:
        out.append(f'<p>{esc(board.get("title"))}: {esc(board.get("size_mm", ["?", "?"])[0])} x {esc(board.get("size_mm", ["?", "?"])[1])} mm, {esc(len(board.get("layers", [])))} copper layers, '
                   f'{esc(board.get("footprints"))} parts, {esc(board.get("nets"))} nets.</p>')
    brief = overview.get('brief') or {}
    if brief.get('purpose'):
        out.append(f'<p>{esc(brief["purpose"])}</p>')
    counts = overview['counts']
    out.append('<p class="counts">' + ''.join(f'<span>{sev_pill(s)} {esc(counts.get(s, 0))}</span>' for s in SEVERITY_ORDER) + '</p>')
    out.append('<p class="muted small">There is no overall score: it would look more precise than it is.</p>')
    if overview.get('top_risks'):
        out.append('<h3>Top risks</h3><ul>' + ''.join(f'<li>{sev_pill(r["severity"])} {esc(r["code"])} {esc(r["title"])}</li>' for r in overview['top_risks']) + '</ul>')
    if images.get('overview'):
        out.append(f'<div class="card">{images["overview"]}<p class="muted small">Board with numbered issue pins (top layer).</p></div>')
    # 4 rf
    out.append('<h2>RF result</h2>')
    out.append(render_rf(rf, images))
    # 5 investigation story
    out.append('<h2>Investigation story</h2>')
    if snap['investigations']:
        for inv in snap['investigations']:
            outcome = inv.get('outcome') or {}
            confirm_label = f' - confirm: {esc(outcome["confirm_label"])}' if isinstance(outcome, dict) and outcome.get('confirm_label') else ''
            out.append(f'<div class="card"><h3>{esc(inv["code"])}: {esc(inv["question"])}</h3><p>{pill("", inv["status"])} {esc(outcome.get("outcome", "") if isinstance(outcome, dict) else outcome)}{confirm_label}</p>')
            if isinstance(outcome, dict) and outcome.get('summary'):
                out.append(f'<p>{esc(outcome["summary"])}</p>')
            if isinstance(outcome, dict) and outcome.get('screen_summary'):
                out.append(f'<p class="muted small"><strong>{esc(restate.SCREEN_HISTORY)}.</strong> {esc(outcome["screen_summary"])}</p>')
            out.append('<table><tr><th>Suspect</th><th>Result</th><th>Tests</th></tr>')
            for s in inv['suspects']:
                tests = '<br>'.join(experiment_line(e) for e in s['experiments']) or '<span class="muted">none</span>'
                history = f'<br><span class="muted small"><strong>{esc(restate.SCREEN_HISTORY)}</strong>{" (" + esc(s["screen_status"].replace("_", " ")) + ")" if s.get("screen_status") else ""}: {esc(s["screen_reason"])}</span>' if s.get('screen_reason') else ''
                caveat = f'<br><span class="small"><strong>Caveat.</strong> {esc(s["caveat"])}</span>' if s.get('caveat') else ''
                out.append(f'<tr><td>{esc(s["code"])} {esc(s["title"])}</td><td>{pill("", s["status"].replace("_", " "))}<br><span class="small">{esc(s.get("reason") or "")}</span>{history}{caveat}</td><td class="small">{tests}</td></tr>')
            out.append('</table>')
            if inv.get('confirmation'):
                out.append(render_confirmation(inv['confirmation']))
            for round_ in inv.get('refine_rounds') or []:
                if round_.get('declined'):
                    out.append(f'<p><strong>Refine round declined.</strong> {esc(round_["declined"])}</p>')
                elif round_.get('skipped'):
                    out.append(f'<p><strong>Refine round not possible.</strong> {esc(round_["skipped"])}</p>')
                else:
                    shots = '; '.join(f'{esc(x.get("label"))} (reason: {esc(x.get("reason"))})' for x in round_.get('screens') or []) or 'no screen what-if'
                    out.append(f'<p><strong>Refine round {esc(round_.get("round"))}</strong>, a second attempt after the confirm missed the target: {shots}. Result: {esc(str(round_.get("result", "")).replace("_", " "))}.</p>')
                    if round_.get('confirmation'):
                        out.append(render_confirmation(round_['confirmation'], f'Refine round {round_.get("round")}: screen against confirm'))
            out.append('</div>')
    else:
        out.append('<p class="muted">No investigation was run.</p>')
    # 6 findings
    out.append('<h2>Findings</h2>')
    findings = sorted(snap['findings'], key=lambda f: (SEVERITY_ORDER.index(f['severity']), f['code']))
    if not findings:
        out.append('<p class="muted">No findings were recorded.</p>')
    for f in findings:
        out.append(render_finding(f, snap, images.get(f['code'])))
    out.append(render_review_work(snap.get('review_work') or {}))
    # 7 version changes
    if snap.get('version_changes'):
        out.append('<h2>Changes since the previous version</h2>')
        out.append(render_version_changes(snap['version_changes']))
    # 8 not tested, assumptions, limits
    out.append('<h2>Not tested, assumptions and limits</h2>')
    out.append('<h3>Not tested</h3>' + ('<ul>' + ''.join(f'<li>{esc(t)}</li>' for t in snap['not_tested']) + '</ul>' if snap['not_tested'] else '<p class="muted">Nothing is listed as not tested.</p>'))
    out.append('<h3>Assumptions</h3>' + ('<table><tr><th>Name</th><th>Value</th><th>Source</th></tr>' + ''.join(
        f'<tr><td>{esc(a["name"].replace("_", " "))}</td><td>{esc(assumption_text(a["value"]))}</td><td>{esc(a["source"].replace("_", " "))}{" - " + esc(a["note"]) if a.get("note") else ""}</td></tr>' for a in snap['assumptions']) + '</table>'
        if snap['assumptions'] else '<p class="muted">No assumptions were recorded.</p>'))
    if snap['limits']:
        out.append('<h3>Limits reached</h3><ul>' + ''.join(f'<li><code>{esc(l["rule"])}</code> {esc(l["detail"])} (phase {esc(l.get("phase"))})</li>' for l in snap['limits']) + '</ul>')
    if snap['candidates']['unreviewed']:
        out.append(f'<p>{esc(snap["candidates"]["unreviewed"])} analyzer or KiCad result(s) were <strong>Unreviewed</strong> by the agent.</p>')
    # 9 method
    method = snap['method']
    out.append('<h2>Method</h2>')
    out.append(f'<p class="small">KiCad {esc(method.get("kicad_version") or "not recorded")}{"; default design rules were used" if method.get("default_rules") else ""}. '
               f'{esc(method.get("note"))}</p>')
    if method['scenarios']:
        out.append('<table><tr><th>Run</th><th>Quality</th><th>Scenario hash</th><th>Changes</th><th>Solve s</th><th>Trust</th></tr>' + ''.join(
            f'<tr><td>{esc(s.get("ref"))}</td><td>{esc(s.get("quality"))}</td><td class="mono small">{esc(s["hash"])}</td><td class="small">{esc(json.dumps(s.get("ops") or "none"))}</td>'
            f'<td class="n">{esc(s.get("solve_s"))}</td><td>{esc(s.get("trust"))}</td></tr>' for s in method['scenarios']) + '</table>')
    out.append('<p class="muted small">TrustPCB is an independent inspection layer. It never edits a design: every recommendation is a step for the engineer to apply in their own tool.</p>')
    out.append('</main></body></html>')
    return ''.join(out)


def assumption_text(value: Any) -> str:
    if isinstance(value, dict):
        if 'value' in value and 'unit' in value:
            return f'{value["value"]} {value["unit"]}'.strip()
        return ', '.join(f'{k.replace("_", " ")} {v}' for k, v in value.items() if v is not None)
    return json.dumps(value) if isinstance(value, list) else str(value)


def render_changes(changes: dict, cause: str) -> str:
    rows = []
    for key, label in (('new_findings', 'New'), ('updated', 'Updated'), ('superseded', 'Superseded'), ('dismissed', 'Dismissed'), ('needs_recheck', 'Needs recheck'), ('reopened', 'Reopened')):
        for item in changes.get(key) or []:
            tag = ' (found in follow-up)' if item.get('found_in_followup') else ''
            rows.append(f'<li>{esc(label)}: {esc(item["code"])} {esc(item["title"])}{esc(tag)}</li>')
    for a in changes.get('assumptions') or []:
        rows.append(f'<li>Assumption: {esc(a["name"])} = {esc(a["value"])} ({esc(str(a["source"]).replace("_", " "))})</li>')
    for t in changes.get('not_tested_added') or []:
        rows.append(f'<li>Not tested: {esc(t)}</li>')
    return f'<div class="changes"><strong>Changes since revision r{esc(changes.get("from_revision"))}</strong> <span class="muted small">({esc(cause)})</span><ul>{"".join(rows)}</ul></div>'


NOT_TESTED_RF = ('S11 curve and match to the target band', 'electric-field picture of the antenna', 'radiation pattern')


def render_rf(rf: dict, images: dict | None = None) -> str:
    images = images or {}
    if rf['status'] == 'not_tested':
        shown = ', '.join(NOT_TESTED_RF)
        return (f'<div class="notice"><p><strong>{pill("cls-not_tested", "Not tested")} RF behaviour.</strong> {esc(rf["reason"])}</p>'
                f'<p>Because no simulation ran, this report has no {esc(shown)}. Statements about antenna tuning, matching or radiated power are '
                f'engineering opinion or layout measurements, not simulated results. Treat the antenna as unverified until it is measured on a real board.</p></div>')
    parts = []
    target = rf.get('target_db')
    for key, title in (('confirm_base', 'Confirmed base'), ('confirm_winner', 'Confirmed best change')):
        item = rf.get(key)
        if not item:
            continue
        parts.append(f'<div class="card"><h3>{esc(title)} ({esc(item["code"])})</h3>')
        if item.get('curve'):
            parts.append(curve_svg(item['curve'], rf.get('band_hz'), target))
        parts.append('<table class="metrics">' + ''.join(f'<tr><td>{esc(metric_name(k))}</td><td class="n">{esc(measurement_text(v))}</td></tr>' for k, v in (item.get('measurements') or {}).items()) + '</table></div>')
    if rf.get('meets_target') is not None:
        parts.append(f'<p>Target ({esc(target)} dB across the band): {pill("cls-measured" if rf["meets_target"] else "sev-high", "met" if rf["meets_target"] else "not met")}</p>')
    if rf.get('screen_runs'):
        parts.append('<p class="muted">Only screen runs exist. They compare changes with the base and give no absolute result.</p>')
    for key, title, caption in (('rf_field', 'Field still', 'Electric field in the board plane at the design frequency, from the confirm run.'),
                                ('rf_pattern', 'Radiation pattern', pattern_caption(images.get('rf_pattern_freq_hz'), rf.get('band_hz')))):
        if images.get(key):
            parts.append(f'<div class="card"><h3>{esc(title)}</h3>{images[key]}<p class="muted small">{esc(caption)}{" " + esc(images[key + "_note"]) if images.get(key + "_note") else ""}</p></div>')
    missing = images.get('rf_missing') or {}
    if rf.get('confirm_base') or rf.get('confirm_winner'):
        for key, title in (('field', 'Field still'), ('pattern', 'Radiation-pattern still')):
            if not images.get('rf_' + key):
                parts.append(f'<p class="muted small">{pill("cls-not_tested", "Not tested")} {esc(title)}: {esc(missing.get(key) or "no image was produced for this run")}.</p>')
    else:
        parts.append(f'<p class="muted small">{pill("cls-not_tested", "Not tested")} Field and radiation-pattern stills are shown only for confirm runs, and there is none.</p>')
    parts.append(f'<p class="muted small">{esc(rf["note"])}</p>')
    return ''.join(parts)


def render_review_work(work: dict) -> str:
    """What the agent cleared as harmless, what code grouped, and how closely the board was inspected."""
    out = []
    dismissals = work.get('dismissals') or []
    if dismissals:
        out.append('<h2>Checked and cleared</h2><p class="muted small">These analyzer results were judged harmless on this board. The reason is the agent\'s own; check it against your design intent.</p><table>'
                   '<tr><th>Check</th><th>Results</th><th>Why harmless</th></tr>')
        for d in dismissals:
            out.append(f'<tr><td>{esc(", ".join(d.get("rules") or []))}</td><td class="n">{esc(d.get("count"))}</td><td class="small">{esc(d.get("reason"))}</td></tr>')
        out.append('</table>')
    grouped = work.get('auto_grouped') or []
    if grouped:
        out.append('<p class="small muted">Grouped by code, not judged one by one (info level): ' + esc(', '.join(f'{g["rule"]} ({g["count"]})' for g in grouped)) + '.</p>')
    if work.get('region_views'):
        out.append(f'<p class="small muted">The agent made {esc(work["region_views"])} close-up board view(s) in its open review.</p>')
    return ''.join(out)


def render_finding(f: dict, snap: dict, image: str | None) -> str:
    evidence_by_code = {e['code']: e for e in snap['evidence']}
    out = [f'<div class="card" id="{esc(f["code"])}"><h3>{esc(f["code"])} {esc(f["title"])}</h3>',
           f'<p>{sev_pill(f["severity"])} {class_pill(f["class"])} {pill("", f["status"].replace("_", " "))}'
           f'{pill("", "Found in follow-up") if f.get("source") == "followup" else ""}{pill("sev-medium", "Needs recheck") if f.get("needs_recheck") else ""}</p>']
    if f['status'] == 'dismissed':
        out.append(f'<p class="notice">Dismissed: {esc(f.get("dismissed_reason") or "no reason recorded")}</p>')
    if f.get('superseded_by'):
        out.append('<p class="notice">Superseded by a newer finding.</p>')
    if image:
        out.append(image)
    out.append(f'<p><strong>What.</strong> {esc(f["what"])}</p><p><strong>Why it matters.</strong> {esc(f["why_it_matters"])}</p>'
               f'<p><strong>Recommendation for the engineer.</strong> {esc(f["recommendation"])}</p>')
    if f['evidence']:
        out.append('<p class="small"><strong>Evidence.</strong></p><ul class="small">')
        for code in f['evidence'][:EVIDENCE_SHOWN]:
            e = evidence_by_code.get(code)
            if not e:
                continue
            values = e['values']
            label = values.get('title') or values.get('label') or e['type']
            numbers = '; '.join(f'{metric_name(k)}: {measurement_text(v)}' for k, v in (values.get('measurements') or {}).items() if isinstance(v, dict))
            tag = f' [{values.get("quality")}, trust {(values.get("trust") or {}).get("status")}]' if values.get('kind') == 'simulated' else ''
            out.append(f'<li><code>{esc(code)}</code> {esc(label)}{esc(tag)}{": " + esc(numbers) if numbers else ""}</li>')
        if len(f['evidence']) > EVIDENCE_SHOWN:
            out.append(f'<li class="muted">and {len(f["evidence"]) - EVIDENCE_SHOWN} more results grouped under this finding (all kept in the JSON export)</li>')
        out.append('</ul>')
    if f['assumptions']:
        out.append('<p class="small"><strong>Assumptions.</strong> ' + esc('; '.join(f['assumptions'])) + '</p>')
    if f['not_tested']:
        out.append('<p class="small"><strong>Not tested.</strong> ' + esc('; '.join(f['not_tested'])) + '</p>')
    if f.get('history'):
        out.append(f'<p class="muted small">{len(f["history"])} earlier version(s) of this finding are kept in its history.</p>')
    out.append('</div>')
    return ''.join(out)


CHANGE_LABEL = {'fixed': 'Fixed', 'improved': 'Improved', 'still_present': 'Still present', 'worse': 'Worse', 'new': 'New'}


def render_version_changes(changes: dict) -> str:
    counts = changes.get('counts') or {}
    summary = ', '.join(f'{counts.get(k, 0)} {CHANGE_LABEL[k].lower()}' for k in CHANGE_LABEL)
    out = [f'<p class="muted small">Compared with version {esc(changes["against"]["version_number"])}: {esc(summary)}.</p>']
    for key in ('fixed', 'improved', 'still_present', 'worse', 'new'):
        items = [i for bucket in ('fixed', 'still_present', 'changed', 'new') for i in changes.get(bucket) or [] if (i.get('change') or {'fixed': 'fixed', 'new': 'new'}.get(bucket, 'still_present')) == key]
        items.sort(key=lambda i: (SEVERITY_ORDER.index(i['severity']), i.get('code') or ''))
        if items:
            out.append(f'<h3>{esc(CHANGE_LABEL[key])} ({len(items)})</h3><ul>' + ''.join(
                f'<li>{sev_pill(i["severity"])} {esc(i["title"])}{" - " + esc("; ".join(i["changes"])) if i.get("changes") else ""}'
                f'{" <span class=muted>(" + esc(i["note"]) + ")</span>" if i.get("note") else ""}</li>' for i in items) + '</ul>')
    for key, label in (('dismissed_carried', 'Dismissed earlier and carried forward'), ('not_rechecked', 'Not rechecked')):
        items = changes.get(key) or []
        if items:
            out.append(f'<h3>{esc(label)} ({len(items)})</h3><ul>' + ''.join(f'<li>{sev_pill(i["severity"])} {esc(i["title"])}</li>' for i in items) + '</ul>')
    return ''.join(out)


# ---- images ---------------------------------------------------------------------------------------

def _bbox_of(marks: list[dict]) -> list[float] | None:
    boxes = [g['bbox'] for m in marks for g in m.get('resolved') or [] if g.get('bbox')]
    if not boxes:
        return None
    return [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]


def _clamp_region(region: list[float], bbox) -> list[float]:
    """The crop window moved and trimmed to lie inside the board outline's box, so a crop never shows white margin or runs off the board."""
    out = list(region)
    for lo, hi, size_lo, size_hi in ((0, 2, bbox[0], bbox[2]), (1, 3, bbox[1], bbox[3])):
        span = min(region[hi] - region[lo], size_hi - size_lo)
        start = min(max(region[lo], size_lo), size_hi - span)
        out[lo], out[hi] = start, start + span
    return out


async def board_images(store, review: dict, snap: dict) -> dict:
    """Marked-up board images as inline SVG: an overview and a crop per finding. Empty when the board cannot be drawn."""
    from ..pcb import render_svg
    from . import service
    try:
        board = await service.get_lab(store).load_board(version_id=review['version_id'])
    except Exception:
        logger.warning('Report for review %s has no board images: the board could not be loaded', review.get('id'), exc_info=True)
        return {}

    def draw() -> dict:
        images: dict[str, str] = {}
        live = [f for f in snap['findings'] if f['marks'] and f['status'] in ('open', 'needs_recheck')]
        number = lambda f: int(f['code'].split('-')[-1])
        marks = [{**m, 'number': number(f), 'severity': f['severity']} for f in live for m in f['marks'] if m['type'] == 'pin']
        opts = render_svg.RenderOptions(theme='light', px_per_mm=8)
        base = render_svg.layer_svg(board, 'top', opts)
        images['overview'] = render_svg.annotate(base, marks, board, labels=False, spread=True).replace('<svg ', '<svg class="board" ', 1)
        for f in live[:12]:
            box = _bbox_of(f['marks'])
            if not box:
                continue
            pad = max(4.0, 0.6 * max(box[2] - box[0], box[3] - box[1]))
            region = _clamp_region([box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad], board.bbox)
            crop = render_svg.layer_svg(board, 'top', render_svg.RenderOptions(theme='light', px_per_mm=14, region=region))
            fm = [{**m, 'number': number(f), 'severity': f['severity']} for m in f['marks']]
            images[f['code']] = render_svg.annotate(crop, fm, board, labels=False, spread=True).replace('<svg ', '<svg class="board" ', 1)
        return images

    try:
        return await asyncio.to_thread(draw)
    except Exception:
        logger.warning('Report for review %s has no board images: drawing the board failed', review.get('id'), exc_info=True)
        return {}


# ---- the hook the API calls ------------------------------------------------------------------------

def _still_sources(rows: list[dict]) -> list[tuple[dict, dict | None]]:
    """Confirm results whose solver output may hold the stills, best first, as (row whose job is drawn, the winner row it stands in for).

    A matching-only winner is calculated on the base confirm run and has no job of its own (`job_id` is None, `derived_from` names the
    run): its stills are that run's, so the winner resolves to it. Then the other confirm runs follow."""
    by_ref = {r['values'].get('ref'): r for r in rows if r['values'].get('ref')}

    def has_run(row: dict) -> bool:
        return bool(row['values'].get('run') or row['values'].get('job_id'))

    ordered = sorted(rows, key=lambda r: r['values'].get('label') != 'confirm winner')       # the winner first, the rest in stored order
    out: list[tuple[dict, dict | None]] = []
    for row in ordered:
        if has_run(row):
            out.append((row, None))
            continue
        source = by_ref.get(row['values'].get('derived_from'))
        if source is not None and has_run(source):
            out.append((source, row))
    seen: set[str] = set()
    return [(r, w) for r, w in out if not (r['id'] in seen or seen.add(r['id']))]


def _pattern_freq_hz(lab, run: dict) -> float | None:
    """The frequency the stored far field was computed at, read from the run's pattern.json (None when it cannot be read)."""
    try:
        job = lab.job_dir(run)
        value = json.loads((job / 'pattern.json').read_text()).get('f_hz') if job else None
        return float(value) if value else None
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def pattern_caption(freq_hz: float | None, band_hz: list | None) -> str:
    """What the radiation-pattern still says about its frequency: inside the band, or plainly outside it (older runs computed it at the deepest S11 dip)."""
    base = 'Directivity from the confirm run. A simulation, not a chamber measurement.'
    if not freq_hz:
        return base
    ghz = f'{freq_hz / 1e9:.3f} GHz'
    if band_hz and len(band_hz) == 2 and band_hz[0] <= freq_hz <= band_hz[1]:
        return f'{base} Computed at {ghz}, inside the band.'
    band = f' ({band_hz[0] / 1e9:.5g} to {band_hz[1] / 1e9:.5g} GHz)' if band_hz and len(band_hz) == 2 else ''
    return (f'{base} This far field was computed at {ghz} (runs made before the band-centre change used the deepest S11 dip), which is outside the board\'s band{band}. '
            'This pattern is therefore not the pattern in the band.') if band_hz and len(band_hz) == 2 else f'{base} Computed at {ghz}, which cannot be placed against the band.'


async def rf_stills(store, review: dict, snap: dict) -> dict:
    """Field and radiation-pattern stills from the best confirm run, as inline images. The Lab supplies the PNG bytes
    (`view_result` kinds `field` and `pattern`); anything it cannot draw is reported in `rf_missing`, never invented."""
    from . import service
    out: dict[str, Any] = {}
    if (snap.get('rf') or {}).get('status') != 'tested':
        return out
    rows = [r for r in store.list_evidence(review['id']) if r['type'] == ev.CONFIRM and (r['values'].get('trust') or {}).get('status') == 'ok']
    sources = _still_sources(rows)
    missing: dict[str, str] = {}
    if not sources:
        missing = {k: 'no confirmed run with stored solver output' for k in ('field', 'pattern')}
    else:
        lab = service.get_lab(store)
        for kind in ('field', 'pattern'):
            reason = ''
            for row, stands_in_for in sources:
                try:
                    values = row['values']
                    run = values.get('run') or {'job_id': values.get('job_id'), 'status': values.get('status') or 'done', 'result': values.get('metrics') or {}, 'curve': values.get('curve')}
                    image = await lab.view_result(review_id=review['id'], result=run, kind=kind, params={})
                    data = image.get('data_b64') or (base64.b64encode(image['png']).decode() if image.get('png') else None)
                    if not data:
                        raise ValueError('the lab returned no image')
                    if kind == 'pattern':
                        freq = _pattern_freq_hz(lab, run)
                        if freq:
                            out['rf_pattern_freq_hz'] = freq
                    out['rf_' + kind] = f'<img alt="{kind} still" class="still" src="data:{image.get("mime", "image/png")};base64,{data}">'
                    if stands_in_for is not None:
                        out['rf_' + kind + '_note'] = f'Drawn from confirm run {row["values"].get("ref") or row["code"]}, the solver run the winning matching change was calculated on.'
                    break
                except Exception as exc:
                    reason = reason or str(exc)[:160] or type(exc).__name__
            else:
                missing[kind] = reason
    if missing:
        out['rf_missing'] = missing
    return out


async def render_revision(*, store, review: dict, revision: dict, fmt: str) -> bytes:
    """GET /api/reviews/{id}/report/{rev}.html|pdf. The snapshot is frozen; only the board images are drawn at render time."""
    if fmt not in ('html', 'pdf'):
        raise ValueError('Format must be html or pdf')
    snapshot = revision['snapshot']
    if fmt == 'pdf':
        async with pdf_slot():           # drawing the images and printing are both heavy: one export at a time
            return await html_to_pdf(await _document(store, review, snapshot))
    return (await _document(store, review, snapshot)).encode('utf-8')


async def _document(store, review: dict, snapshot: dict) -> str:
    images = await board_images(store, review, snapshot)
    images.update(await rf_stills(store, review, snapshot))
    return render_html(snapshot, images)
