"""Reports: sections, self-contained HTML, JSON, PDF hook and frozen revisions (spec section 16)."""
import json
import re
from pathlib import Path

import pytest

from trustpcb.review import report

SCRIPTS = Path(__file__).parent / 'scripts'


async def standard(env):
    return await env.review(SCRIPTS / 'standard_success.json')


async def html_of(env, review, number=None):
    revision = env.store.get_report_revision(review['id'], number) if number else env.store.latest_report_revision(review['id'])
    return (await report.render_revision(store=env.store, review=review, revision=revision, fmt='html')).decode()


async def test_report_contains_every_section_of_the_spec(env):
    review = await standard(env)
    html = await html_of(env, review)
    for heading in ('Summary', 'Board overview', 'RF result', 'Investigation story', 'Findings', 'Not tested, assumptions and limits', 'Method'):
        assert f'<h2>{heading}</h2>' in html, heading
    assert 'nRF node' in html and 'Version 1' in html and 'report revision r1' in html and 'scripted' in html and 'complete' in html          # the cover
    assert 'F-004' in html and 'Simulated' in html and 'Measured' in html and 'Engineering opinion' in html
    assert 'The antenna is detuned by ground copper' in html and 'Top actions' in html
    assert 'Confirmed base' in html and 'Confirmed best change' in html and re.search(r'Target \(-10(\.0)? dB across the band\):\s*<span class="pill cls-measured">met', html)
    assert 'Simulated. Not a substitute for a real antenna measurement.' in html
    assert 'Recommendation for the engineer' in html and 'no overall score' in html.lower()
    assert re.search(r'[0-9a-f]{16}', html)                                       # scenario hashes in the method section
    assert html.count('<svg') >= 3                                                # board overview, a finding crop and the S11 curve
    snapshot = env.store.latest_report_revision(review['id'])['snapshot']
    assert snapshot['investigations'][0]['suspects'][0]['status'] == 'confirmed' and snapshot['investigations'][0]['suspects'][1]['status'] == 'inconclusive'


async def test_html_is_self_contained_and_escapes_board_text(env):
    review = await standard(env)
    finding = env.store.create_finding(review['id'], 'x|net:A|', '<script>alert(1)</script> & <img src=x onerror=alert(2)>', 'low', 'opinion', what='<b>bold</b>',
                                       why_it_matters='w', recommendation='r', marks=[], source='review')
    report.create_revision(env.store, review['id'], cause='test')
    html = await html_of(env, review)
    assert '<script' not in html and '<img src=x' not in html and 'onerror=alert' not in html.replace('&lt;img src=x onerror=alert(2)&gt;', '')
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in html and finding['code'] in html
    externals = re.findall(r'(?:src|href)="(https?:[^"]+)"|url\((https?:[^)]+)\)|@import', html)
    assert externals == []
    assert html.startswith('<!doctype html>') and '<meta name="viewport"' in html


async def test_json_snapshot_is_the_frozen_report(env):
    review = await standard(env)
    snapshot = env.store.latest_report_revision(review['id'])['snapshot']
    assert set(snapshot) >= {'revision', 'cause', 'cover', 'summary', 'overview', 'rf', 'investigations', 'findings', 'evidence', 'assumptions', 'not_tested', 'limits', 'method', 'changes_since_last'}
    assert snapshot['changes_since_last'] is None and snapshot['revision'] == 1
    assert json.loads(json.dumps(snapshot)) == snapshot                          # plain JSON, nothing to rebuild
    codes = {e['code'] for e in snapshot['evidence']}
    assert {c for f in snapshot['findings'] for c in f['evidence']} <= codes     # every finding's evidence is in the report
    assert {a['source'] for a in snapshot['assumptions']} >= {'default', 'user_provided'}
    assert all('run' not in e['values'] for e in snapshot['evidence'])


async def test_assumptions_are_listed_with_their_source(env):
    review = await standard(env)
    html = await html_of(env, review)
    assert 'tuning target s11 db' in html and '-10 dB' in html and 'user provided' in html and 'default' in html and 'stackup' in html and 'thickness mm 1.6' in html


async def test_earlier_revisions_never_change(env):
    review = await standard(env)
    first = await html_of(env, review, 1)
    snapshot = json.dumps(env.store.get_report_revision(review['id'], 1)['snapshot'], sort_keys=True)
    env.store.update_finding(env.store.get_finding_by_code(review['id'], 'F-001')['id'], 'user', 'not relevant', status='dismissed', dismissed_reason='It is intentional.')
    second = report.create_revision(env.store, review['id'], cause='follow-up: dismissed F-001')
    assert second['number'] == 2
    assert second['snapshot']['changes_since_last']['dismissed'][0]['code'] == 'F-001'
    assert await html_of(env, review, 1) == first
    assert json.dumps(env.store.get_report_revision(review['id'], 1)['snapshot'], sort_keys=True) == snapshot
    assert [r['number'] for r in env.store.list_report_revisions(review['id'])] == [1, 2]
    assert 'Changes since revision r1' in await html_of(env, review, 2)


def test_diff_between_revisions_lists_every_kind_of_change():
    finding = lambda code, **kw: {'code': code, 'title': f't{code}', 'severity': 'low', 'status': 'open', 'class': 'measured', 'what': 'a', 'why_it_matters': 'b', 'recommendation': 'c', 'source': 'review', **kw}
    old = {'revision': 1, 'findings': [finding('F-001'), finding('F-002'), finding('F-003'), finding('F-004')], 'assumptions': [], 'not_tested': ['x']}
    new = {'revision': 2, 'findings': [finding('F-001', what='changed'), finding('F-002', status='dismissed', dismissed_reason='fine'), finding('F-003', status='superseded', superseded_by='F-005'),
                                       finding('F-004', status='needs_recheck'), finding('F-005', source='followup')],
           'assumptions': [{'name': 'material', 'value': 'Rogers', 'source': 'user_provided'}], 'not_tested': ['x', 'y']}
    diff = report.diff_snapshots(old, new)
    assert [f['code'] for f in diff['updated']] == ['F-001'] and diff['updated'][0]['fields'] == ['what']
    assert [f['code'] for f in diff['dismissed']] == ['F-002'] and [f['code'] for f in diff['superseded']] == ['F-003']
    assert [f['code'] for f in diff['needs_recheck']] == ['F-004'] and diff['new_findings'][0]['found_in_followup']
    assert diff['assumptions'][0]['name'] == 'material' and diff['not_tested_added'] == ['y'] and diff['empty'] is False
    assert report.diff_snapshots(old, old)['empty'] is True


async def test_pdf_needs_a_browser_and_uses_it_when_present(env, tmp_path, monkeypatch):
    review = await standard(env)
    revision = env.store.latest_report_revision(review['id'])
    monkeypatch.delenv('TRUSTPCB_BROWSER', raising=False)
    monkeypatch.setattr(report, 'find_browser', lambda: str(tmp_path / 'no-such-browser'))
    with pytest.raises(ValueError, match='Chrome or Chromium'):
        await report.render_revision(store=env.store, review=review, revision=revision, fmt='pdf')
    with pytest.raises(ValueError):
        await report.render_revision(store=env.store, review=review, revision=revision, fmt='docx')


async def test_pdf_is_printed_by_a_real_browser(env):
    review = await standard(env)
    revision = env.store.latest_report_revision(review['id'])
    try:
        pdf = await report.render_revision(store=env.store, review=review, revision=revision, fmt='pdf')
    except ValueError as e:
        pytest.skip(str(e))
    assert pdf.startswith(b'%PDF')


async def test_a_report_for_a_failed_run_says_where_and_why(env):
    review = await env.review(SCRIPTS / 'agent_crash.json')
    html = await html_of(env, review)
    assert 'agent_stopped'.replace('_', ' ') in html and 'The agent stopped before finishing' in html and 'upstream connection lost' in html
    assert 'ended before its run finished' in html and 'Written by code from the stored findings' in html
    assert 'Not tested' in html and 'Unreviewed' in html


async def test_report_images_survive_a_board_that_cannot_be_drawn(env):
    review = await standard(env)

    async def broken(**kwargs):
        raise RuntimeError('board file missing')

    env.lab.load_board = broken
    html = await html_of(env, review)
    assert '<h2>Findings</h2>' in html and 'F-004' in html and 'Board with numbered issue pins' not in html


def test_power_bars_hatch_the_earlier_value_and_mark_the_improved_one():
    html = report.power_bars('Accepted power', [('Version 1', 11), ('Version 2', 74.1)])
    assert html.count('<b class="before" style="width:11%">') == 1 and html.count('<b class="now" style="width:74.1%">') == 1
    assert html.index('Version 1') < html.index('class="before"') < html.index('Version 2') < html.index('class="now"')
    assert report.power_bars('x', [('a', None), ('b', 5)]) == ''
    css = report.CSS
    assert 'repeating-linear-gradient' in css and '25%' in css and '50%' in css and '75%' in css and 'print-color-adjust:exact' in css


def test_print_css_lets_long_cards_flow_and_keeps_images_and_rows_whole():
    printed = report.CSS.split('@media print', 1)[1]
    assert '.card{break-inside:auto}' in printed and 'svg.board' in printed and 'tr' in printed and 'max-height' in printed


def test_clamp_region_stays_inside_the_board():
    from trustpcb.review.report import _clamp_region
    bbox = (0.0, 0.0, 80.0, 60.0)
    assert _clamp_region([-6, -6, 10, 12], bbox) == [0, 0, 16, 18]          # moved in, not shrunk
    assert _clamp_region([70, 50, 90, 70], bbox) == [60, 40, 80, 60]
    assert _clamp_region([-20, -20, 120, 100], bbox) == [0, 0, 80, 60]      # larger than the board: the board


def test_still_sources_follow_a_matching_winner_to_its_base_run():
    from trustpcb.review.report import _still_sources
    base = {'id': 'a', 'code': 'E-1', 'values': {'ref': 'R3', 'label': 'confirm base', 'job_id': 'j1'}}
    winner = {'id': 'b', 'code': 'E-2', 'values': {'ref': 'R6', 'label': 'confirm winner', 'job_id': None, 'derived_from': 'R3'}}
    out = _still_sources([base, winner])
    assert out == [(base, winner)]                                         # one source, listed once, standing in for the winner
