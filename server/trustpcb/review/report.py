"""Reports: assembled by code from stored data, numbered revisions, frozen snapshots (spec sections 16 and 17).

`create_revision` freezes everything a report shows into one JSON snapshot. Earlier snapshots are
never edited, so exported files and older revisions stay exactly as they were. The HTML is
self-contained (inline styles, inline SVG), the JSON is the snapshot itself, and the PDF is the HTML
printed by a headless browser when one is installed.
"""
from __future__ import annotations

import asyncio
import base64
import html
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import evidence as ev
from .compare import classify_findings, previous_context
from .machine import PHASE_NAMES

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
    out = []
    for index, inv in enumerate(store.list_investigations(review_id), start=1):
        suspects = []
        for s in store.list_suspects(inv['id']):
            data = s.get('data') or {}
            suspects.append({'code': data.get('code') or f'S{s["rank"]}', 'title': s['title'], 'status': s['status'], 'reason': (s.get('conclusion') or {}).get('reason'),
                             'experiments': data.get('experiments') or []})
        out.append({'code': f'I{index}', 'question': inv['question'], 'status': inv['status'], 'outcome': inv.get('outcome'), 'suspects': suspects})
    return out


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
    section['confirm_base'], section['confirm_winner'] = shown(base_confirm), shown(winner_confirm)
    best = winner_confirm or base_confirm
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


def version_changes(store, review_id: str, new_coverage: set[str] | None = None) -> dict | None:
    """Fixed / still present / changed / new against the previous version's review, or None for a first version."""
    review = store.get_review(review_id)
    previous = previous_context(store, review['version_id'])
    if not previous:
        return None
    out = classify_findings(store, previous['findings'], store.list_findings(review_id), new_coverage=new_coverage)
    out['against'] = {'version_number': previous['version_number'], 'review_id': previous['review']['id']}
    return out


# ---- HTML ------------------------------------------------------------------------------------------

CSS = '''
:root{color-scheme:light}*{box-sizing:border-box}body{font:15px/1.5 -apple-system,"Segoe UI",Inter,Helvetica,Arial,sans-serif;color:#1c2024;background:#fff;margin:0}
main{max-width:920px;margin:0 auto;padding:32px 24px 64px}h1{font-size:28px;margin:0 0 4px}h2{font-size:20px;margin:36px 0 10px;padding-bottom:6px;border-bottom:1px solid #d7dbe0}
h3{font-size:16px;margin:18px 0 4px}p{margin:6px 0}.meta,.muted{color:#5b6570}.mono,td.n{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-variant-numeric:tabular-nums}
table{border-collapse:collapse;width:100%;margin:8px 0}th,td{text-align:left;padding:5px 8px;border-bottom:1px solid #e6e9ed;vertical-align:top}th{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:#5b6570}
.pill{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;font-weight:600;border:1px solid #c8ced5;background:#f4f6f8}
.sev-critical{background:#ffe3e8;border-color:#ff2d55;color:#8a0f2b}.sev-high{background:#ffe5e5;border-color:#ff4d4f;color:#8a1c1c}.sev-medium{background:#fff1dc;border-color:#ff9f1a;color:#7a4a00}
.sev-low{background:#fff9d6;border-color:#d9b800;color:#665500}.sev-info{background:#e3f1ff;border-color:#4da3ff;color:#0c4a8a}
.cls-measured{background:#e5f6ee;border-color:#2a9d68;color:#145a3b}.cls-simulated{background:#e6f4fb;border-color:#22a3d6;color:#0e5a78}.cls-opinion{background:#f1ecfa;border-color:#8e6fcf;color:#46308a}.cls-not_tested{background:#eee;color:#444}
.card{border:1px solid #d7dbe0;border-radius:8px;padding:14px 16px;margin:14px 0;break-inside:avoid}.card h3{margin-top:0}.notice{border-left:4px solid #ff9f1a;background:#fff8ec;padding:8px 12px;margin:12px 0}
.changes{border-left:4px solid #22a3d6;background:#eef8fd;padding:8px 12px;margin:12px 0}svg.board{width:100%;height:auto;border:1px solid #d7dbe0;border-radius:6px;background:#fff}
.counts span{margin-right:8px}ul{padding-left:20px;margin:4px 0}code{font-family:ui-monospace,Menlo,monospace;font-size:13px}.small{font-size:13px}
@media print{main{padding:0}h2{break-after:avoid}}
'''


def esc(value: Any) -> str:
    return html.escape('' if value is None else str(value), quote=True)


def pill(kind: str, label: str | None = None) -> str:
    return f'<span class="pill {esc(kind)}">{esc(label or kind)}</span>'


def sev_pill(sev: str) -> str:
    return pill(f'sev-{sev}', sev.capitalize())


def class_pill(cls: str) -> str:
    return pill(f'cls-{cls}', CLASS_LABEL.get(cls, cls))


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
            out.append(f'<div class="card"><h3>{esc(inv["code"])}: {esc(inv["question"])}</h3><p>{pill("", inv["status"])} {esc(outcome.get("outcome", "") if isinstance(outcome, dict) else outcome)}</p>')
            if isinstance(outcome, dict) and outcome.get('summary'):
                out.append(f'<p>{esc(outcome["summary"])}</p>')
            out.append('<table><tr><th>Suspect</th><th>Result</th><th>Tests</th></tr>')
            for s in inv['suspects']:
                tests = '<br>'.join(esc(f'{e.get("ref")} {e.get("label")} ({e.get("kind")}, {e.get("quality")}){" - failed trust" if not e.get("ok") else ""}') for e in s['experiments']) or '<span class="muted">none</span>'
                out.append(f'<tr><td>{esc(s["code"])} {esc(s["title"])}</td><td>{pill("", s["status"].replace("_", " "))}<br><span class="small">{esc(s.get("reason") or "")}</span></td><td class="small">{tests}</td></tr>')
            out.append('</table></div>')
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
        parts.append('<table>' + ''.join(f'<tr><td>{esc(k.replace("_", " "))}</td><td class="n">{esc(measurement_text(v))}</td></tr>' for k, v in (item.get('measurements') or {}).items()) + '</table></div>')
    if rf.get('meets_target') is not None:
        parts.append(f'<p>Target ({esc(target)} dB across the band): {pill("cls-measured" if rf["meets_target"] else "sev-high", "met" if rf["meets_target"] else "not met")}</p>')
    if rf.get('screen_runs'):
        parts.append('<p class="muted">Only screen runs exist. They compare changes with the base and give no absolute result.</p>')
    for key, title, caption in (('rf_field', 'Field still', 'Electric field in the board plane at the design frequency, from the confirm run.'),
                                ('rf_pattern', 'Radiation pattern', 'Directivity from the confirm run. A simulation, not a chamber measurement.')):
        if images.get(key):
            parts.append(f'<div class="card"><h3>{esc(title)}</h3>{images[key]}<p class="muted small">{esc(caption)}</p></div>')
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
        out.append(f'<p class="notice">Superseded by a newer finding.</p>')
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
            numbers = ', '.join(f'{k.replace("_", " ")} {measurement_text(v)}' for k, v in (values.get('measurements') or {}).items() if isinstance(v, dict))
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


def render_version_changes(changes: dict) -> str:
    out = [f'<p class="muted small">Compared with version {esc(changes["against"]["version_number"])}.</p>']
    for key, label in (('fixed', 'Fixed'), ('still_present', 'Still present'), ('changed', 'Changed'), ('new', 'New'), ('dismissed_carried', 'Dismissed earlier and carried forward'), ('not_rechecked', 'Not rechecked')):
        items = changes.get(key) or []
        if items:
            out.append(f'<h3>{esc(label)} ({len(items)})</h3><ul>' + ''.join(f'<li>{sev_pill(i["severity"])} {esc(i["title"])}'
                       f'{" - " + esc("; ".join(i["changes"])) if i.get("changes") else ""}</li>' for i in items) + '</ul>')
    return ''.join(out)


# ---- images ---------------------------------------------------------------------------------------

def _bbox_of(marks: list[dict]) -> list[float] | None:
    boxes = [g['bbox'] for m in marks for g in m.get('resolved') or [] if g.get('bbox')]
    if not boxes:
        return None
    return [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]


async def board_images(store, review: dict, snap: dict) -> dict:
    """Marked-up board images as inline SVG: an overview and a crop per finding. Empty when the board cannot be drawn."""
    from ..pcb import render_svg
    from . import service
    try:
        board = await service.get_lab(store).load_board(version_id=review['version_id'])
    except Exception:
        return {}

    def draw() -> dict:
        images: dict[str, str] = {}
        live = [f for f in snap['findings'] if f['marks'] and f['status'] in ('open', 'needs_recheck')]
        number = lambda f: int(f['code'].split('-')[-1])
        marks = [{**m, 'number': number(f), 'severity': f['severity']} for f in live for m in f['marks'] if m['type'] == 'pin']
        opts = render_svg.RenderOptions(theme='light', px_per_mm=8)
        base = render_svg.layer_svg(board, 'top', opts)
        images['overview'] = render_svg.annotate(base, marks, board).replace('<svg ', '<svg class="board" ', 1)
        for f in live[:12]:
            box = _bbox_of(f['marks'])
            if not box:
                continue
            pad = max(4.0, 0.6 * max(box[2] - box[0], box[3] - box[1]))
            region = [box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad]
            crop = render_svg.layer_svg(board, 'top', render_svg.RenderOptions(theme='light', px_per_mm=14, region=region))
            fm = [{**m, 'number': number(f), 'severity': f['severity']} for m in f['marks']]
            images[f['code']] = render_svg.annotate(crop, fm, board).replace('<svg ', '<svg class="board" ', 1)
        return images

    try:
        return await asyncio.to_thread(draw)
    except Exception:
        return {}


# ---- the hook the API calls ------------------------------------------------------------------------

def find_browser() -> str | None:
    configured = os.environ.get('TRUSTPCB_BROWSER')
    if configured and Path(configured).exists():
        return configured
    for name in ('chromium', 'chromium-browser', 'google-chrome', 'google-chrome-stable', 'chrome'):
        found = shutil.which(name)
        if found:
            return found
    mac = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
    return mac if Path(mac).exists() else None


async def html_to_pdf(document: str, browser: str | None = None) -> bytes:
    browser = browser or find_browser()
    if not browser:
        raise ValueError('PDF export needs Chrome or Chromium installed. Open the HTML report and print it to PDF instead.')
    with tempfile.TemporaryDirectory(prefix='trustpcb-pdf-') as tmp:
        source, target = Path(tmp) / 'report.html', Path(tmp) / 'report.pdf'
        source.write_text(document, encoding='utf-8')
        process = await asyncio.create_subprocess_exec(
            browser, '--headless=new', '--disable-gpu', '--no-pdf-header-footer', f'--user-data-dir={tmp}/profile', f'--print-to-pdf={target}', source.as_uri(),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        try:
            await asyncio.wait_for(process.wait(), timeout=90)
        except asyncio.TimeoutError:
            process.kill()
            raise ValueError('PDF export timed out.') from None
        if not target.is_file() or target.stat().st_size == 0:
            raise ValueError('The browser did not produce a PDF.')
        return target.read_bytes()


async def rf_stills(store, review: dict, snap: dict) -> dict:
    """Field and radiation-pattern stills from the best confirm run, as inline images. The Lab supplies the PNG bytes
    (`view_result` kinds `field` and `pattern`); anything it cannot draw is reported in `rf_missing`, never invented."""
    from . import service
    out: dict[str, Any] = {}
    if (snap.get('rf') or {}).get('status') != 'tested':
        return out
    rows = [r for r in store.list_evidence(review['id']) if r['type'] == ev.CONFIRM and (r['values'].get('trust') or {}).get('status') == 'ok' and (r['values'].get('run') or r['values'].get('job_id'))]
    winner = next((r for r in rows if r['values'].get('label') == 'confirm winner'), None)
    best = winner or (rows[0] if rows else None)
    missing: dict[str, str] = {}
    if best is None:
        missing = {k: 'no confirmed run with stored solver output' for k in ('field', 'pattern')}
    else:
        lab = service.get_lab(store)
        for kind in ('field', 'pattern'):
            try:
                values = best['values']
                run = values.get('run') or {'job_id': values.get('job_id'), 'status': values.get('status') or 'done', 'result': values.get('metrics') or {}, 'curve': values.get('curve')}
                image = await lab.view_result(review_id=review['id'], result=run, kind=kind, params={})
                data = image.get('data_b64') or (base64.b64encode(image['png']).decode() if image.get('png') else None)
                if not data:
                    raise ValueError('the lab returned no image')
                out['rf_' + kind] = f'<img alt="{kind} still" style="max-width:100%;border:1px solid #d7dbe0;border-radius:6px" src="data:{image.get("mime", "image/png")};base64,{data}">'
            except Exception as exc:
                missing[kind] = str(exc)[:160] or type(exc).__name__
    if missing:
        out['rf_missing'] = missing
    return out


async def render_revision(*, store, review: dict, revision: dict, fmt: str) -> bytes:
    """GET /api/reviews/{id}/report/{rev}.html|pdf. The snapshot is frozen; only the board images are drawn at render time."""
    if fmt not in ('html', 'pdf'):
        raise ValueError('Format must be html or pdf')
    snapshot = revision['snapshot']
    images = await board_images(store, review, snapshot)
    images.update(await rf_stills(store, review, snapshot))
    document = render_html(snapshot, images)
    if fmt == 'html':
        return document.encode('utf-8')
    return await html_to_pdf(document)
