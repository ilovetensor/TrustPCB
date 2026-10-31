"""Reports: sections, self-contained HTML, JSON, PDF hook and frozen revisions."""
import json
import re
from pathlib import Path

import pytest

from trustpcb.review import report, report_css, report_pdf

SCRIPTS = Path(__file__).parent / 'scripts'


def squash(css: str) -> str:
    return re.sub(r'\s+', '', css).replace(';}', '}')


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
    monkeypatch.setattr(report_pdf, 'find_browser', lambda: str(tmp_path / 'no-such-browser'))
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
    assert 'stopped early' in html and 'agent stopped' not in html.lower() and 'the agent ended before finishing' in html and 'upstream connection lost' in html
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
    css = squash(report_css.CSS)
    assert 'repeating-linear-gradient' in css and '25%' in css and '50%' in css and '75%' in css and 'print-color-adjust:exact' in css


def test_print_css_lets_long_cards_flow_and_keeps_images_and_rows_whole():
    printed = squash(report_css.CSS).split('@mediaprint', 1)[1]
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


def test_best_confirmed_change_is_the_lowest_worst_s11_not_just_the_screen_winner():
    from trustpcb.review.report import best_confirmed
    row = lambda code, worst: {'code': code, 'values': {'metrics': {'band_worst_s11_db': worst}}}
    locked_screen_set, free_retune = row('E-178', -3.86), row('E-177', -5.78)
    assert best_confirmed(locked_screen_set, free_retune)['code'] == 'E-177'     # the headline's best change, not the screen's parts
    assert best_confirmed(row('E-4', -12.0), row('E-5', -9.0))['code'] == 'E-4'
    assert best_confirmed(locked_screen_set, None)['code'] == 'E-178' and best_confirmed(None, None) is None
    assert best_confirmed({'code': 'E-9', 'values': {}}, None)['code'] == 'E-9'     # no numbers to compare: keep what exists


def test_metric_names_are_readable_and_unknown_keys_degrade_gracefully():
    from trustpcb.review.report import metric_name
    assert metric_name('resonance') == 'Deepest dip' and metric_name('s11_band_worst') == 'Worst S11 in the band'
    assert metric_name('band_worst_accepted_power_pct') == 'Accepted power, worst point of the band'
    assert metric_name('antenna_only_band_mean_accepted_power_pct').endswith('antenna alone')
    assert metric_name('s11_dip_2_depth') == 'S11 dip 2 depth' and metric_name('some_new_key') == 'Some new key' and metric_name('s11_new') == 'S11 new'


def test_pattern_caption_says_plainly_when_the_pattern_is_outside_the_band():
    from trustpcb.review.report import pattern_caption
    band = [2.4e9, 2.4835e9]
    assert 'outside the board' in pattern_caption(1.869e9, band) and '1.869 GHz' in pattern_caption(1.869e9, band)
    assert 'inside the band' in pattern_caption(2.44e9, band)
    assert pattern_caption(None, band).startswith('Directivity from the confirm run')


def test_a_board_with_no_radio_gets_one_plain_line_in_the_rf_section():
    html = report.render_rf({'status': 'not_tested', 'kind': 'none', 'reason': 'no wireless part or antenna was found on this board', 'target_db': -10.0, 'band_hz': None})
    assert 'No wireless part or antenna was found on this board.' in html and 'covers the rules and the layout only' in html
    assert 'antenna as unverified' not in html and 'S11 curve' not in html and 'radiation pattern' not in html


def test_a_chip_antenna_board_keeps_the_not_tested_warning_with_a_sentence_reason():
    html = report.render_rf({'status': 'not_tested', 'kind': 'chip', 'reason': 'the antenna is a chip antenna', 'target_db': -10.0, 'band_hz': None})
    assert 'The antenna is a chip antenna.' in html and 'Treat the antenna as unverified' in html


async def test_the_code_written_summary_does_not_apologise_for_antenna_tuning_on_a_board_with_no_radio(env):
    from trustpcb.review.service import deterministic_summary
    review = await standard(env)
    none = deterministic_summary(env.store, review['id'], 'complete', 'completed', 'not_tested', extra={'rf_kind': 'none'})
    chip = deterministic_summary(env.store, review['id'], 'complete', 'completed', 'not_tested', extra={'rf_kind': 'chip'})
    assert 'antenna tuning' not in none['text'] and 'antenna tuning' in chip['text']


def test_board_simulation_keys_have_plain_names_that_keep_net_and_part_names():
    names = {
        'worst_drop_mv': 'Largest voltage drop', 'drop_pct:VBUS_F': 'Voltage drop on VBUS_F, share of nominal', 'drop_mv:VBUS_F': 'Voltage drop on VBUS_F',
        'v_pin:U1@VBUS_F': 'Voltage at the lowest U1 supply pin on VBUS_F', 'peak_rise': 'Hottest point above ambient', 'ANT_FEED.z0_median': 'ANT_FEED: median impedance',
        'CC1.reference_gap_length': 'CC1: track length with no ground under it', 'delta_peak_rise': 'Change in hottest point above ambient',
        'confirm_winner_worst_drop_pct': 'Best change at the finer mesh: largest voltage drop, share of nominal', 'USB_D+.z0_worst_deviation': 'USB_D+: largest impedance deviation from the target',
    }
    for key, expected in names.items():
        assert report.metric_name(key) == expected, key
    assert report.metric_name('s11_min') == 'Lowest S11' and report.metric_name('some_new_key') == 'Some new key'          # antenna names and unknown keys are as before
    assert not any(raw in report.metric_name(k) for k in names for raw in ('_mv', '_pct', '@', 'v_pin', 'drop_'))
