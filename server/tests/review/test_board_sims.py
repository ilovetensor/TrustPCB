"""Board simulations (power, thermal, signal) through the whole review: scripted agent, fake lab with fake simulators, fake clock."""
import json
import types
import re
from pathlib import Path

import pytest

from gate_steps import clear
from simfakes import FakeSimLab, review_conftest

SCRIPTS, Env, FakeClock = review_conftest.SCRIPTS, review_conftest.Env, review_conftest.FakeClock
from trustpcb.review import evidence as ev
from trustpcb.review import extent as ext
from trustpcb.review import report, service
from trustpcb.runners.scripted import ScriptedRunner
from trustpcb.sessions import SessionManager
from trustpcb.storage import Store

GOAL = {'purpose': 'USB powered sensor board', 'focus': 'It draws 0.8 A from the 3.3 V rail when the sensor heater is on.'}
NO_RADIO = {'simulatable': False, 'reason': 'No radio part or antenna was found on this board.', 'kind': 'none', 'code': 'not_wireless'}


@pytest.fixture
def simenv(tmp_path, sample_board):
    store = Store(tmp_path / 'data')
    clock = FakeClock()
    lab = FakeSimLab(sample_board, clock, store.data_dir)
    holder = {}

    def factory(profile):
        runner = ScriptedRunner(holder['env'].script)
        holder['env'].runners.append(runner)
        return runner

    manager = SessionManager(store, runner_factory=factory)
    project = store.create_project('USB node')
    version = store.create_version(project['id'], label='first')
    environment = Env(store, manager, clock, lab, project, version)
    holder['env'] = environment
    service.configure(lab=lab, clock=clock, poll_s=1.0)
    yield environment
    service.configure()


def step(tool, **args):
    return {'tool': tool, 'args': args}


BRIEF = step('submit_board_brief', purpose='USB powered sensor board with a regulator and a heater', part_roles={'U3': 'rf', 'C13': 'decoupling'},
             risks=[], priorities=['power'])


def power_script(*tail, load=True):
    head = [step('get_sim_inputs', kind='power')]
    if load:
        head.append(step('set_sim_input', kind='power', name='current:U3@+3V3', value=0.8, source='user', cite='It draws 0.8 A from the 3.3 V rail'))
    return [*head, BRIEF, step('advance_phase'), *clear(), step('advance_phase'), *tail]


def results(env, chat, name=None):
    return [(e['payload'].get('name'), e['payload']) for e in env.events(chat, 'tool_result') if name in (None, e['payload'].get('name'))]



def investigate_power(*more):
    return [
        step('open_investigation', kind='power', question='Does the 3V3 rail drop too much under load?',
             suspects=[{'title': 'Narrow supply track', 'why': 'It carries the whole load.'}, {'title': 'Few vias', 'why': 'One layer change.'}]),
        step('run_board_sim', kind='power', suspect_id='S1', label='base'),
        step('run_board_sim', kind='power', suspect_id='S1', label='wider track', whatif=[{'op': 'widen_track', 'net': '+3V3', 'width_mm': 1.0}]),
        step('conclude_suspect', suspect_id='S1', verdict='confirmed', reasoning='The wider track makes the largest voltage drop 40 % smaller than the base (screen comparison).'),
        step('conclude_suspect', suspect_id='S2', verdict='inconclusive', reasoning='Not tested: the first suspect already explains the drop.'),
        step('record_finding', title='A narrow supply track drops the 3.3 V rail under load', severity='medium', targets=['ref:U3'], evidence_ids=['E-004'],
             what='In simulation, widening the 3V3 supply track makes the largest voltage drop 40 % smaller than the base (E-004), at the 0.8 A load the engineer gave.',
             why_it_matters='A lower rail at the regulator output leaves less margin for the sensor heater.', recommendation='Widen the 3V3 supply track as tested in simulation, then measure the rail on a built board.'),
        step('close_investigation', investigation_id='I1', outcome='improved', summary='The wider track makes the largest drop 40 % smaller in the screen runs.', finding_code='F-001'),
        step('advance_phase'),
        step('confirm_result', investigation_id='I1', which='base'),
        step('confirm_result', investigation_id='I1', which='winner'),
        step('record_finding', update='F-001', title='A narrow supply track drops the 3.3 V rail under load', severity='medium', targets=['ref:U3'], evidence_ids=['E-006', 'E-008', 'E-009'],
             what='At the finer mesh the simulated largest voltage drop goes from 2.496 % (E-006) to 1.4976 % (E-008) with the wider track, a gain of 40 % (E-009), at the 0.8 A load the engineer gave.',
             why_it_matters='A lower rail at the regulator output leaves less margin for the sensor heater.', recommendation='Widen the 3V3 supply track as tested in simulation, then measure the rail on a built board.'),
        step('advance_phase'),
        step('submit_summary', summary='In simulation the 3.3 V rail drops 2.496 % (E-006) at the 0.8 A the engineer gave, and widening the supply track makes the drop 40 % smaller (E-009).',
             top_actions=['F-001: Widen the 3V3 supply track as tested in simulation, then measure the rail.']),
        *more,
    ]



def findings_of(env, review):
    return env.store.list_findings(review['id'])


def phases_of(env, review):
    return [e['payload']['phase'] for e in env.events(review['chat_id'], 'phase_changed')]


async def power_review(simenv, load=True, goal=None, extent=('layout', 'power'), **kwargs):
    simenv.lab.rf = dict(NO_RADIO)
    return await simenv.review(power_script(*investigate_power(), load=load), goal=goal or GOAL, extent=list(extent), **kwargs)


# ---- the demo of the feature: a board with no radio and power chosen reaches Investigate ----------------------------------

async def test_a_board_with_no_radio_and_power_chosen_reaches_investigate_and_confirm(simenv):
    review = await power_review(simenv)
    assert review['end_state'] == 'complete', review['summary']['end']
    assert phases_of(simenv, review) == [0, 1, 2, 3, 4, 5, 6]
    assert review['extent'] == ['layout', 'power'] and review['summary']['extent']['active_simulations'] == ['power'] and review['summary']['extent']['antenna'] is False
    assert simenv.lab.count('run_scenario') == 0                                   # no antenna solver ran
    assert simenv.lab.count_sims('power') == 4                                     # base, what-if, confirm base, confirm winner
    kinds = [(r['type'], (r['values'] or {}).get('quality'), (r['values'] or {}).get('sim_kind')) for r in simenv.store.list_evidence(review['id']) if r['type'] != 'candidate']
    assert ('board_sim', 'confirm', 'power') in kinds and any(t == 'sim_assumptions' for t, _, _ in kinds)
    finding = findings_of(simenv, review)[0]
    assert finding['class'] == 'simulated' and finding['status'] == 'open'
    investigation = simenv.store.list_investigations(review['id'])[0]
    assert investigation['outcome']['sim_kind'] == 'power' and investigation['outcome']['confirmation']['outcome'] == 'held'


async def test_the_report_has_a_section_per_simulator_with_numbers_assumptions_stills_and_what_was_not_tested(simenv):
    review = await power_review(simenv)
    revision = simenv.store.latest_report_revision(review['id'])
    sims = revision['snapshot']['sims']
    power = sims['kinds'][0]
    assert power['kind'] == 'power' and power['status'] == 'tested' and power['base']['quality'] == 'confirm' and power['winner']['measurements']['worst_drop_pct']['value'] == 1.4976
    assert {a['source'] for a in power['assumptions']} == {'user', 'board', 'default'} and power['defaults'] == ['allowed voltage drop', 'copper temperature']
    assert power['comparison']['outcome'] == 'held' and any('Real load currents' in t for t in power['not_tested'])
    assert 'Extent chosen at the start: rules and layout, power delivery' in revision['snapshot']['method']['extent_line']
    assert revision['snapshot']['method']['sim_runs'] and revision['snapshot']['method']['extent'] == ['layout', 'power']
    html = (await report.render_revision(store=simenv.store, review=review, revision=revision, fmt='html')).decode()
    assert '<h2>Board simulations</h2>' in html and 'Given by the engineer' in html and 'Read from the board' in html and 'Not tested.' in html
    assert html.count('src="data:') + html.count('href="data:image/png;base64') >= 2           # the base and the best change, drawn inline
    assert 'http://' not in re.sub(r'xmlns="[^"]+"', '', html) and 'https://' not in html
    assert 'Extent.' in html and 'Simulation</th>' in html
    assert 'Worst drop pct' in html


async def test_the_report_with_stills_prints_under_the_locked_down_pdf_path(simenv):
    from trustpcb.review import report_pdf
    if report_pdf.find_browser() is None:
        pytest.skip('no browser for printing on this machine')
    review = await power_review(simenv)
    revision = simenv.store.latest_report_revision(review['id'])
    pdf = await report.render_revision(store=simenv.store, review=review, revision=revision, fmt='pdf')
    assert pdf.startswith(b'%PDF')


async def test_a_review_without_a_chosen_simulation_behaves_as_before(simenv):
    simenv.lab.rf = dict(NO_RADIO)
    script = [BRIEF, step('advance_phase'), *clear(), step('advance_phase'),
              step('submit_summary', summary='A USB powered board. Only rules and layout were reviewed.', top_actions=['Check the rail on a built board.'])]
    review = await simenv.review(script, goal=GOAL, extent=['layout'])
    assert review['end_state'] == 'complete' and phases_of(simenv, review) == [0, 1, 2, 3, 6]
    skipped = {s['phase']: s['reason'] for c in review['summary']['phases'] for s in c['skipped']}
    assert 'No simulation was chosen' in skipped[4] and skipped[5].startswith('No investigation')
    assert simenv.lab.count_sims() == 0 and 'get_sim_inputs' not in [n for n, _ in results(simenv, review['chat_id'])]
    snapshot = simenv.store.latest_report_revision(review['id'])['snapshot']
    assert snapshot['sims']['kinds'] == [] and 'No simulation was chosen' in snapshot['method']['extent_line']


async def test_without_an_extent_the_default_applies_and_is_stored(simenv):
    simenv.lab.kinds = {}                                                           # a build without the board simulators
    review = await simenv.review(SCRIPTS / 'quick_complete.json', depth='quick')
    assert review['extent'] == ['layout', 'antenna']                                # as before: the rules and the antenna where the board allows it
    simenv.lab.rf = dict(NO_RADIO)
    other = simenv.store.create_version(simenv.project['id'], label='second')
    started = await service.start_review(store=simenv.store, manager=simenv.manager, version_id=other['id'], goal=GOAL, depth='quick')
    assert started['extent'] == ['layout']
    await simenv.wait()


async def test_start_refuses_an_unknown_extent_and_one_this_board_cannot_do(simenv):
    simenv.lab.rf = dict(NO_RADIO)
    for bad, text in ((['warp'], 'Unknown extent'), (['antenna'], 'cannot be chosen'), (['datasheets'], 'Datasheets'), ('power', 'list of names')):
        with pytest.raises(ValueError, match=text):
            await service.start_review(store=simenv.store, manager=simenv.manager, version_id=simenv.version['id'], goal=GOAL, depth='quick', extent=bad)
    assert simenv.store.list_reviews(version_id=simenv.version['id']) == []


async def test_every_extent_combination_reaches_the_right_phases(simenv):
    steps_after_3 = [step('submit_summary', summary='Only the rules and layout were reviewed in this run.', top_actions=['Check the board.'])]
    simple = [BRIEF, step('advance_phase'), *clear(), step('advance_phase')]
    cases = [(['layout'], NO_RADIO, [0, 1, 2, 3, 6]), (['layout', 'power'], NO_RADIO, [0, 1, 2, 3, 4, 6]), (['layout', 'thermal', 'signal'], NO_RADIO, [0, 1, 2, 3, 4, 6]),
             (['layout', 'antenna'], None, [0, 1, 2, 3, 4, 6]), (['layout', 'antenna', 'power', 'thermal', 'signal'], None, [0, 1, 2, 3, 4, 6])]
    for extent, rf, expected in cases:
        simenv.lab.rf = dict(NO_RADIO) if rf else {'simulatable': True, 'reason': '', 'kind': 'printed', 'antenna_id': 'ant1', 'antenna_bbox': [27.0, 11.0, 33.0, 18.0],
                                                     'region_bbox': [15.0, 0.0, 45.0, 30.0], 'band_hz': [2.4e9, 2.4835e9], 'target_s11_db': -10.0}
        script = [*simple]
        if len(expected) == 6 and 4 in expected:
            script += [step('advance_phase')]                                       # Investigate with nothing tested: move on
        script += steps_after_3
        review = await simenv.review(script, goal=GOAL, extent=extent)
        assert phases_of(simenv, review) == expected, (extent, review['summary']['end'])
        assert review['end_state'] == 'complete', (extent, review['summary']['end'])


async def test_leaving_investigate_with_a_chosen_check_untested_is_refused_once(simenv):
    simenv.lab.rf = dict(NO_RADIO)
    review = await power_review(simenv, extent=('layout', 'power', 'thermal'))        # power is investigated and closed; thermal never is
    texts = [json.dumps(p.get('content')) for n, p in results(simenv, review['chat_id'], 'advance_phase')]
    refused = [t for t in texts if 'checks_not_run' in t]
    assert len(refused) == 1 and 'thermal' in refused[0] and 'power' not in refused[0].split('not_run')[-1][:60]
    assert 5 in phases_of(simenv, review)       # the second advance_phase went through


async def test_datasheets_chosen_but_never_used_is_refused_once_in_understand(simenv, monkeypatch):
    monkeypatch.setattr(ext, 'parts_ready', lambda: (True, ''))
    simenv.lab.parts_ready = True
    simenv.lab.rf = dict(NO_RADIO)
    script = [BRIEF, step('advance_phase'), step('advance_phase'), *clear(), step('advance_phase'), step('advance_phase'), step('advance_phase'),
              step('submit_summary', summary='Only the rules and layout were reviewed in this run.', top_actions=['Check the board.'])]
    review = await simenv.review(script, goal=GOAL, extent=['layout', 'power', 'datasheets'])
    texts = [json.dumps(p.get('content')) for n, p in results(simenv, review['chat_id'], 'advance_phase')]
    assert sum('datasheets_unused' in t for t in texts) == 1 and 'lookup_part' in texts[0]
    assert 3 in phases_of(simenv, review) and review['end_state'] == 'complete'


async def test_quick_reviews_record_a_chosen_simulation_as_not_run(simenv):
    simenv.lab.rf = dict(NO_RADIO)
    script = [BRIEF, step('advance_phase'), *clear(), step('advance_phase'), step('submit_summary', summary='Only the rules and layout were reviewed.', top_actions=['Check the board.'])]
    review = await simenv.review(script, depth='quick', goal=GOAL, extent=['layout', 'power'])
    assert phases_of(simenv, review) == [0, 1, 2, 3, 6] and simenv.lab.count_sims() == 0
    assert 'Quick reviews stop after phase 3' in review['summary']['extent']['could_not_run']['power']
    snapshot = simenv.store.latest_report_revision(review['id'])['snapshot']
    assert any('Power delivery was chosen at the start but did not run' in t for t in snapshot['not_tested'])
    assert snapshot['sims']['kinds'][0]['status'] == 'not_tested' and 'Quick reviews' in snapshot['sims']['kinds'][0]['reason']


async def test_a_chosen_simulation_that_cannot_run_is_reported_and_investigate_is_skipped(simenv):
    simenv.lab.rf = dict(NO_RADIO)
    gone = {'available': False, 'reason': 'No copper nets carry current.', 'label': 'Power delivery', 'whatif_ops': []}
    saved = dict(simenv.lab.kinds['power'])
    simenv.lab.kinds['power'] = gone
    with pytest.raises(ValueError, match='No copper nets carry current'):                  # not offered, so not accepted at the start
        await service.start_review(store=simenv.store, manager=simenv.manager, version_id=simenv.version['id'], goal=GOAL, depth='standard', extent=['layout', 'power'])
    simenv.lab.kinds['power'] = saved
    script = [BRIEF, step('advance_phase'), *clear(), step('advance_phase'), step('submit_summary', summary='Only the rules and layout were reviewed.', top_actions=['Check the board.'])]
    simenv.use(script)
    started = await service.start_review(store=simenv.store, manager=simenv.manager, version_id=simenv.version['id'], goal=GOAL, depth='standard', extent=['layout', 'power'])
    simenv.lab.kinds['power'] = gone                                                        # it stopped being possible between the start and the run
    await simenv.wait()
    review = simenv.store.get_review(started['id'])
    skipped = {s['phase']: s['reason'] for c in review['summary']['phases'] for s in c['skipped']}
    assert 'Power delivery could not run: No copper nets carry current.' in skipped[4]
    assert review['summary']['extent']['could_not_run'] == {'power': 'No copper nets carry current.'}
    assert any('did not run: No copper nets' in t for t in simenv.store.latest_report_revision(review['id'])['snapshot']['not_tested'])


# ---- trust, defaults and the claim checks -------------------------------------------------------------------------------------

def default_run_script(*tail):
    """A power investigation on defaults: nobody gave the load current."""
    return power_script(
        step('open_investigation', kind='power', question='Does the 3V3 rail drop too much under load?', suspects=[{'title': 'Narrow supply track', 'why': 'It carries the whole load.'}]),
        step('run_board_sim', kind='power', suspect_id='S1', label='base'),
        step('run_board_sim', kind='power', suspect_id='S1', label='wider track', whatif=[{'op': 'widen_track', 'net': '+3V3', 'width_mm': 1.0}]),
        *tail, load=False)


FINDING = dict(title='A narrow supply track drops the 3.3 V rail', targets=['ref:U3'], why_it_matters='A lower rail leaves less margin for the sensor heater.',
               recommendation='Widen the 3V3 supply track as tested in simulation, then measure the rail on a built board.')


def last(env, review, name):
    return [p for n, p in results(env, review['chat_id'], name)][-1]


async def test_a_result_that_failed_its_trust_checks_cannot_support_a_finding(simenv):
    simenv.lab.sim_fail_trust = {'power'}
    script = default_run_script(step('record_finding', severity='medium', evidence_ids=['E-002'], what='The simulated drop is large.', **FINDING))
    review = await simenv.review(script, goal=GOAL, extent=['layout', 'power'])
    base = [p for n, p in results(simenv, review['chat_id'], 'run_board_sim')][0]
    assert 'failed its checks' in base['content'] and '"status":"fail"' in base['content']
    refusal = last(simenv, review, 'record_finding')
    assert refusal['is_error'] and 'trust_failed' in refusal['content'] and findings_of(simenv, review) == []


async def test_a_failed_result_cannot_be_compared_to_the_point_of_a_finding_and_is_marked_in_the_report(simenv):
    simenv.lab.sim_fail_trust = {'power'}
    review = await simenv.review(default_run_script(), goal=GOAL, extent=['layout', 'power'])
    snapshot = simenv.store.latest_report_revision(review['id'])['snapshot']
    assert snapshot['sims']['kinds'][0]['status'] == 'not_tested' and 'failed their checks' in snapshot['sims']['kinds'][0]['reason']
    assert 'differs from the closed form' in snapshot['sims']['kinds'][0]['reason']


async def test_default_inputs_are_labelled_and_a_number_from_a_default_is_not_stated_as_fact(simenv):
    base_finding = dict(severity='medium', evidence_ids=['E-004'], **FINDING)
    script = default_run_script(
        step('record_finding', what='Widening the supply track makes the largest voltage drop 40 % smaller than the base (E-004).', **base_finding),
        step('record_finding', what='With the assumed default load of 1 A, widening the supply track makes the largest voltage drop 40 % smaller than the base (E-004).', **base_finding))
    review = await simenv.review(script, goal=GOAL, extent=['layout', 'power'])
    ran = [p for n, p in results(simenv, review['chat_id'], 'run_board_sim')]
    assert 'defaults_in_use' in ran[0]['content'] and 'current U3 @ +3V3' in ran[0]['content'] and 'about the default' in ran[0]['content']
    first, second = [p for n, p in results(simenv, review['chat_id'], 'record_finding')]
    assert first['is_error'] and 'default_stated_as_fact' in first['content'] and 'default inputs nobody gave' in first['content']
    assert not second['is_error']
    finding = findings_of(simenv, review)[0]
    assert any('Assumed default, not from your design: current U3 @ +3V3 1 A' in line for line in finding['assumptions'])
    assert len(finding['evidence_ids']) == 2 and finding['class'] == 'simulated'                     # the run and the row that lists its inputs


async def test_a_finding_that_rests_on_a_default_cannot_be_rated_high(simenv):
    base = dict(evidence_ids=['E-004'], what='With the assumed default load of 1 A, widening the supply track makes the largest voltage drop 40 % smaller than the base (E-004).', **FINDING)
    script = default_run_script(step('record_finding', severity='high', **base), step('record_finding', severity='medium', **base))
    review = await simenv.review(script, goal=GOAL, extent=['layout', 'power'])
    refused, accepted = [p for n, p in results(simenv, review['chat_id'], 'record_finding')]
    assert refused['is_error'] and 'severity_rests_on_default' in refused['content'] and not accepted['is_error']


async def test_an_input_needs_a_source_and_a_user_value_must_be_in_the_request(simenv):
    script = [step('set_sim_input', kind='power', name='current:U3@+3V3', value=2.0, source='user'),                       # not in the request
              step('set_sim_input', kind='power', name='current:U3@+3V3', value=0.8, source='datasheet', cite='LDO page 3'),     # datasheets are not part of the review
              step('set_sim_input', kind='power', name='current:U3@+3V3', value=0.8, source='user'),
              step('set_sim_input', kind='power', name='nonsense', value=1, source='user'),
              step('set_sim_input', kind='thermal', name='ambient', value=25, source='default'),
              step('get_sim_inputs', kind='power'),
              step('lookup_part', mpn='TPS7A2033')]
    review = await simenv.review(script, goal=GOAL, extent=['layout', 'power'])
    rows = [p for n, p in results(simenv, review['chat_id'])]
    assert 'value_not_in_request' in rows[0]['content'] and 'datasheets_not_chosen' in rows[1]['content'] and not rows[2]['is_error']
    assert 'unknown_input' in rows[3]['content'] and 'sim_not_chosen' in rows[4]['content']
    assert '"source":"user"' in rows[5]['content'] and 'not_allowed_in_phase' in rows[6]['content']                   # lookup_part is hidden without datasheets


async def test_a_value_without_a_source_is_refused_on_a_run(simenv):
    script = default_run_script(step('run_board_sim', kind='power', suspect_id='S1', label='ten amps', inputs={'current:U3@+3V3': 10.0}))
    review = await simenv.review(script, goal=GOAL, extent=['layout', 'power'])
    assert 'input_needs_source' in last(simenv, review, 'run_board_sim')['content']


async def test_datasheet_values_come_from_text_read_in_this_review_and_the_server_writes_the_cite(simenv, monkeypatch):
    monkeypatch.setattr(ext, 'parts_ready', lambda: (True, ''))
    simenv.lab.parts_ready = True
    simenv.lab.rf = dict(NO_RADIO)
    ds = lambda value: step('set_sim_input', kind='power', name='current:U3@+3V3', value=value, source='datasheet', cite='my own made up page')
    script = [step('lookup_part', mpn='TPS7A2033'),
              ds(1.5),                                                                     # nothing read yet
              step('read_datasheet', mpn='TPS7A2033', question='What is the maximum output current?'),
              ds(1.5),                                                                     # a number in a passage
              ds(9.0),
              step('read_datasheet', mpn='AMBIGUOUS', question='What is the current?')]
    review = await simenv.review(script, goal=GOAL, extent=['layout', 'power', 'datasheets'])
    rows = [p for n, p in results(simenv, review['chat_id'])]
    assert not rows[0]['is_error'] and 'not a datasheet' in rows[0]['content'] and 'value_not_in_datasheet' in rows[1]['content']
    assert not rows[2]['is_error'] and 'datasheet page 3' not in rows[2]['content'] and 'untrusted' not in rows[2]['content'] and 'ds.example' in rows[2]['content']
    assert not rows[3]['is_error'] and 'page 3: a number in a quoted passage' in rows[3]['content'] and 'my own made up page' not in rows[3]['content']
    assert 'value_not_in_datasheet' in rows[4]['content']
    assert 'ambiguous' in rows[5]['content'] and 'not a pick' in rows[5]['content'] and 'AMBIGUOUS-1' in rows[5]['content']
    assert any(r['type'] == 'reference' for r in simenv.store.list_evidence(review['id']))


async def test_a_value_the_datasheet_states_is_cited_with_its_url_and_page(simenv, monkeypatch):
    monkeypatch.setattr(ext, 'parts_ready', lambda: (True, ''))
    simenv.lab.parts_ready = True
    simenv.lab.rf = dict(NO_RADIO)
    script = [step('read_datasheet', mpn='TPS7A2033', question='quiescent current'),
              step('set_sim_input', kind='power', name='current:U3@+3V3', value=0.000055, source='datasheet')]       # 55 uA, stated on page 4
    review = await simenv.review(script, goal=GOAL, extent=['layout', 'power', 'datasheets'])
    rows = [p for n, p in results(simenv, review['chat_id'])]
    assert not rows[1]['is_error'] and 'https://ds.example/TPS7A2033.pdf, page 4' in rows[1]['content']


# ---- budgets, loops and the cache ------------------------------------------------------------------------------------------------

async def test_board_simulation_runs_are_budgeted_and_a_stored_run_is_free(simenv, monkeypatch):
    from trustpcb.review import budgets
    monkeypatch.setitem(budgets.SIM_BUDGETS, 'standard', (3, 600.0, 6, 1, 14, 3))
    widen = lambda width: [{'op': 'widen_track', 'net': '+3V3', 'width_mm': width}]
    script = default_run_script(
        step('run_board_sim', kind='power', suspect_id='S1', label='again', whatif=widen(1.0)),               # an identical request: free
        step('run_board_sim', kind='power', suspect_id='S1', label='third', whatif=[{'op': 'add_via', 'net': '+3V3', 'at': [10, 10]}]),
        step('run_board_sim', kind='power', suspect_id='S1', label='fourth', whatif=widen(2.0)))              # a fourth new run: over the budget
    review = await simenv.review(script, goal=GOAL, extent=['layout', 'power'])
    rows = [p for n, p in results(simenv, review['chat_id'], 'run_board_sim')]
    assert [r['is_error'] for r in rows] == [False, False, False, False, True]
    assert '"cached":true' in rows[2]['content'] and '"free":true' in rows[2]['content']
    assert 'budget_exhausted' in rows[4]['content'] and 'sim_runs' in rows[4]['content']
    assert simenv.lab.count_sims('power') == 3
    budget = simenv.store.get_review(review['id'])['budget']['sim_runs']
    assert budget['used'] == 3 and budget['limit'] == 3
    assert any(l['rule'] == 'budget_wall' and l.get('budget') == 'sim_runs' for l in review['summary']['limits'])


async def test_an_identical_run_stored_by_an_earlier_review_is_free_even_with_the_budget_used(simenv, monkeypatch):
    from trustpcb.review import budgets
    first = await simenv.review(default_run_script(), goal=GOAL, extent=['layout', 'power'])
    assert simenv.lab.count_sims('power') == 2
    monkeypatch.setitem(budgets.SIM_BUDGETS, 'standard', (1, 600.0, 6, 1, 14, 3))
    other = simenv.store.create_version(simenv.project['id'], label='second')
    started = await service.start_review(store=simenv.store, manager=simenv.manager, version_id=other['id'], goal=GOAL, depth='standard', extent=['layout', 'power'])
    await simenv.wait()
    second = simenv.store.get_review(started['id'])
    assert first['id'] != second['id']
    runs = [p for n, p in results(simenv, second['chat_id'], 'run_board_sim')]
    assert len(runs) == 2 and all(not r['is_error'] for r in runs)       # the budget allows one new run; both were stored by the first review
    assert simenv.lab.count_sims('power') == 2 and second['budget']['sim_runs']['used'] == 0 and second['budget']['sim_runs']['cached'] == 2


async def test_the_third_repeat_of_an_identical_request_closes_the_suspect(simenv):
    base = step('run_board_sim', kind='power', suspect_id='S1', label='base')
    script = default_run_script(base, base, base)
    review = await simenv.review(script, goal=GOAL, extent=['layout', 'power'])
    rows = [p for n, p in results(simenv, review['chat_id'], 'run_board_sim')]
    assert 'suspect_closed' in rows[-1]['content'] or 'looped' in rows[-1]['content']
    assert simenv.lab.count_sims('power') == 2


async def test_two_runs_in_a_row_that_barely_move_the_metric_close_the_suspect(simenv):
    via = lambda x: step('run_board_sim', kind='power', suspect_id='S1', label=f'via {x}', whatif=[{'op': 'add_via', 'net': '+3V3', 'at': [x, 10]}])
    script = power_script(
        step('open_investigation', kind='power', question='Do more vias help the rail?', suspects=[{'title': 'Too few vias', 'why': 'One layer change.'}]),
        step('run_board_sim', kind='power', suspect_id='S1', label='base'), via(1), via(2), via(3))
    review = await simenv.review(script, goal=GOAL, extent=['layout', 'power'])
    rows = [p for n, p in results(simenv, review['chat_id'], 'run_board_sim')]
    assert 'closed as no_effect' in rows[2]['content'] and rows[3]['is_error'] and 'suspect_closed' in rows[3]['content']
    assert any(l['rule'] == 'no_progress' for l in review['summary']['limits'])


async def test_a_what_if_after_the_inputs_changed_asks_for_a_new_base(simenv):
    script = default_run_script(
        step('set_sim_input', kind='power', name='current:U3@+3V3', value=0.8, source='user', cite='0.8 A'),
        step('run_board_sim', kind='power', suspect_id='S1', label='again', whatif=[{'op': 'add_via', 'net': '+3V3', 'at': [10, 10]}]))
    review = await simenv.review(script, goal=GOAL, extent=['layout', 'power'])
    assert 'base_outdated' in last(simenv, review, 'run_board_sim')['content']


async def test_an_operation_the_simulation_does_not_accept_is_refused_without_a_run(simenv):
    script = default_run_script(step('run_board_sim', kind='power', suspect_id='S1', whatif=[{'op': 'delete_zone'}]))
    review = await simenv.review(script, goal=GOAL, extent=['layout', 'power'])
    assert 'op_not_allowed' in last(simenv, review, 'run_board_sim')['content'] and simenv.lab.count_sims('power') == 2


async def test_a_confirm_that_does_not_hold_is_reported_plainly(simenv, monkeypatch):
    original = FakeSimLab.sim_model

    def model(self, kind, inputs, whatif, quality):
        out = original(self, kind, inputs, whatif, quality)
        if quality == 'confirm' and whatif:
            out['metrics']['worst_drop_pct']['value'] = 2.4              # the wider track stops helping at the finer mesh
        return out

    monkeypatch.setattr(FakeSimLab, 'sim_model', model)
    script = power_script(*investigate_power()[:-4], step('confirm_result', investigation_id='I1', which='base'), step('confirm_result', investigation_id='I1', which='winner'))
    review = await simenv.review(script, goal=GOAL, extent=['layout', 'power'])
    investigation = simenv.store.list_investigations(review['id'])[0]
    confirmation = investigation['outcome']['confirmation']
    assert confirmation['outcome'] == 'not_held' and confirmation['meets_target'] is True
    assert 'did not hold' in investigation['outcome']['summary'] or 'did not hold' in confirmation['statement']
    finding = findings_of(simenv, review)[0]
    assert any('did not hold' in line for line in finding['not_tested'])


# ---- the demo: a review run with every extent ---------------------------------------------------------------------------------------

def sim_investigation(kind, number):
    code = f'S{number}'
    return [
        step('open_investigation', kind=kind, question=f'Does the {kind} behaviour of this board need attention?', suspects=[{'title': f'The {kind} path is marginal', 'why': 'It carries the most load on this board.'}]),
        step('run_board_sim', kind=kind, suspect_id=code, label='base'),
        step('run_board_sim', kind=kind, suspect_id=code, label='one more via', whatif=[{'op': 'add_via', 'net': '+3V3', 'at': [12, 12]}]),
        step('conclude_suspect', suspect_id=code, verdict='inconclusive', reasoning='Only one test point was run and it moved the primary metric very little.'),
        step('close_investigation', investigation_id=f'I{number - 2}', outcome='no_improvement', summary='One test point moved the primary metric very little; nothing was found to improve.'),
    ]


async def test_a_review_with_every_extent_runs_the_antenna_and_each_simulation(simenv):
    steps = json.loads((SCRIPTS / 'standard_success.json').read_text())
    close = max(i for i, st in enumerate(steps) if st.get('tool') == 'close_investigation')
    steps[close + 1:close + 1] = [*sim_investigation('power', 4), *sim_investigation('thermal', 5), *sim_investigation('signal', 6)]
    extent = ['layout', 'antenna', 'power', 'thermal', 'signal']
    review = await simenv.review(steps, extent=extent)
    assert review['end_state'] == 'complete', review['summary']['end']
    assert review['extent'] == extent and phases_of(simenv, review) == [0, 1, 2, 3, 4, 5, 6]
    assert simenv.lab.count('run_scenario') >= 2 and {c[0] for c in simenv.lab.sim_calls} == {'power', 'thermal', 'signal'}
    investigations = simenv.store.list_investigations(review['id'])
    assert [(i['outcome'] or {}).get('sim_kind') for i in investigations] == [None, 'power', 'thermal', 'signal']
    snapshot = simenv.store.latest_report_revision(review['id'])['snapshot']
    assert [k['kind'] for k in snapshot['sims']['kinds']] == ['power', 'thermal', 'signal'] and all(k['status'] == 'screen_only' for k in snapshot['sims']['kinds'])
    assert all('Inputs nobody gave' in ' '.join(k['not_tested']) for k in snapshot['sims']['kinds'])           # defaults are on the page, not hidden
    assert snapshot['rf']['status'] == 'tested'
    budget = simenv.store.get_review(review['id'])['budget']
    assert budget['sim_runs']['used'] == 6 and budget['investigations']['limit'] == 4
    html = (await report.render_revision(store=simenv.store, review=review, revision=simenv.store.latest_report_revision(review['id']), fmt='html')).decode()
    assert '<h2>RF result</h2>' in html and html.count('<h3>Power delivery</h3>') == 1 and '<h3>Thermal</h3>' in html and '<h3>Signal integrity</h3>' in html


async def test_the_antenna_tools_are_not_offered_when_the_antenna_was_not_chosen(simenv):
    simenv.lab.rf = {'simulatable': True, 'reason': '', 'kind': 'printed', 'antenna_id': 'ant1', 'antenna_bbox': [27.0, 11.0, 33.0, 18.0], 'region_bbox': [15.0, 0.0, 45.0, 30.0],
                     'band_hz': [2.4e9, 2.4835e9], 'target_s11_db': -10.0}
    script = power_script(step('run_simulation', suspect_id='S1'), step('open_investigation', question='Is the antenna fine here?', suspects=[{'title': 'The feed', 'why': 'It is long.'}]),
                          step('open_investigation', kind='thermal', question='Does anything get hot on this board?', suspects=[{'title': 'The regulator', 'why': 'It dissipates the most.'}]))
    review = await simenv.review(script, goal=GOAL, extent=['layout', 'power'])
    run, antenna, thermal = [p for n, p in results(simenv, review['chat_id']) if n in ('run_simulation', 'open_investigation')]
    assert 'not_allowed_in_phase' in run['content'] and 'run_board_sim' in run['content'] and 'run_simulation' not in run['content'].split('"allowed"')[1]
    assert 'antenna_not_in_extent' in antenna['content'] and 'sim_not_chosen' in thermal['content']


async def test_the_tool_list_follows_the_extent(simenv, sample_board):
    import types
    from trustpcb.review.budgets import config_for
    from trustpcb.review.session import ReviewSession
    from trustpcb.review.toolset import AllHandlers, build_review_tools
    from trustpcb.review.tools import ToolRegistry

    async def noop(*a, **k):
        return None

    review = simenv.store.create_review(simenv.version['id'], goal={}, depth='standard', extent=['layout', 'power', 'datasheets'])
    info = {'available': {'power': {'label': 'Power delivery', 'whatif_ops': ['widen_track']}}, 'unavailable': {}}
    session = ReviewSession(store=simenv.store, lab=simenv.lab, ctx=types.SimpleNamespace(emit=noop, interact=noop), review=review, board=sample_board, clock=simenv.clock,
                            config=config_for('standard', False, ('power',)), goal={}, rf=dict(NO_RADIO), extent=['layout', 'power', 'datasheets'], sims=info)
    definitions = build_review_tools(AllHandlers(session))
    session.tools = {d.name: d for d in definitions}
    registry = ToolRegistry(definitions, session)
    session.machine.phase = 4
    names = {s.name for s in registry.specs()}
    assert {'run_board_sim', 'get_sim_inputs', 'set_sim_input', 'compare_sims', 'lookup_part', 'open_investigation', 'conclude_suspect'} <= names
    assert not names & {'run_simulation', 'tune_matching', 'view_result', 'compare_results'}
    session.machine.phase = 5
    assert {s.name for s in registry.specs()} == {'get_status', 'advance_phase', 'record_finding', 'compare_sims', 'confirm_result'}
    for spec in registry.all_specs():
        assert 10 < len(spec.description) < 460 and spec.input_schema['additionalProperties'] is False, spec.name
    plain = ReviewSession(store=simenv.store, lab=simenv.lab, ctx=types.SimpleNamespace(emit=noop, interact=noop), review=review, board=sample_board, clock=simenv.clock,
                          config=config_for('standard'), goal={}, rf=dict(simenv.lab.rf))
    plain.tools = session.tools
    plain.machine.phase = 4
    assert not {n for n in plain.allowed_names()} & {'run_board_sim', 'get_sim_inputs', 'set_sim_input', 'compare_sims', 'lookup_part'}


async def test_compare_sims_gives_the_difference_between_two_runs_of_the_same_kind(simenv):
    script = default_run_script(step('compare_sims', a='R1', b='R2'), step('compare_sims', a='R1', b='R9'))
    review = await simenv.review(script, goal=GOAL, extent=['layout', 'power'])
    compared, unknown = [p for n, p in results(simenv, review['chat_id'], 'compare_sims')]
    assert not compared['is_error'] and '"delta_worst_drop_pct"' in compared['content'] and '"delta_primary_gain"' in compared['content'] and unknown['is_error']


# ---- older reviews -------------------------------------------------------------------------------------------------------------------

async def test_a_review_without_a_stored_extent_still_runs_and_renders(simenv):
    """A review row made before extents existed has no extent: it runs as it did and its report says so."""
    review = simenv.store.create_review(simenv.version['id'], goal=GOAL, depth='standard')
    assert review['extent'] is None
    snapshot = report.build_snapshot(simenv.store, review['id'], cause='review')
    assert snapshot['method']['extent'] is None and 'not recorded' in snapshot['method']['extent_line'] and snapshot['sims']['kinds'] == []
    html = report.render_html(snapshot)
    assert 'Board simulations' not in html and 'Extent.</strong> Extent: not recorded' in html


async def test_a_report_snapshot_from_before_board_simulations_renders(simenv):
    review = await simenv.review(SCRIPTS / 'quick_complete.json', depth='quick')
    snapshot = json.loads(json.dumps(simenv.store.latest_report_revision(review['id'])['snapshot']))
    for key in ('sims', 'sim_changes'):
        snapshot.pop(key, None)
    for key in ('extent', 'extent_line', 'sim_runs'):
        snapshot['method'].pop(key, None)
    html = report.render_html(snapshot)
    assert '<h2>RF result</h2>' in html and 'Board simulations' not in html and '<h2>Findings</h2>' in html


async def test_the_start_dialog_options_reflect_the_board(simenv):
    from trustpcb.review.extent_api import extent_options
    simenv.lab.rf = dict(NO_RADIO)
    out = await extent_options(store=simenv.store, version_id=simenv.version['id'])
    by_id = {o['id']: o for o in out['options']}
    assert not by_id['antenna']['available'] and by_id['power']['available'] and not by_id['datasheets']['available']
    assert out['default'] == ['layout', 'power']


# ---- the routes ----------------------------------------------------------------------------------------------------------------------

HOST = 'http://127.0.0.1:8118'


def client_for(simenv, monkeypatch=None, readonly=False):
    from fastapi.testclient import TestClient
    from trustpcb.api import create_app
    if readonly:
        monkeypatch.setenv('TRUSTPCB_DEMO_READONLY', '1')
    app = create_app(data_dir=simenv.store.data_dir, token='t', runner_factory=lambda profile: ScriptedRunner([]), poll=0.01, keepalive=0.05)
    return TestClient(app, base_url=HOST, headers={'X-TrustPCB-Token': 't'})


async def test_the_sims_route_lists_runs_with_overlays_and_the_files_are_served_safely(simenv, monkeypatch):
    review = await power_review(simenv)
    other = simenv.store.create_version(simenv.project['id'], label='other')
    other_review = simenv.store.create_review(other['id'], goal=GOAL, depth='standard', extent=['layout'])
    with client_for(simenv) as client:
        runs = client.get(f"/api/reviews/{review['id']}/sims").json()
        board = [r for r in runs if r.get('family') == 'board' and r['kind'] != 'comparison'] if False else [r for r in runs if r.get('family') == 'board' and r.get('role')]
        assert [r['role'] for r in board] == ['base', 'whatif', 'confirm', 'confirm'] and {r['sim_kind'] for r in board} == {'power'}
        run = board[0]
        assert run['primary'] == 'worst_drop_pct' and run['result']['assumptions'][0]['name'] == 'U3 current on +3V3' and run['trust']['status'] == 'ok' and run['whatif_ops'] == []
        assert run['run_id'] == run['run'] and run['status'] == 'done' and run['step'] == 'done' and run['whatif'] == [] and run['base_run'] is None
        assert board[1]['base_run'] == run['run_id'] and board[1]['whatif'][0]['op'] == 'widen_track'
        assert run['result']['trust'] == {'status': 'ok', 'checks': [{'check': 'benchmark', 'ok': True, 'detail': 'matches the closed form'}, {'check': 'zone fill', 'ok': True, 'detail': 'one zone has no fill'}]}
        assert run['result']['metrics']['worst_drop_pct']['unit'] == '%' and 'currents_assumed' not in run['result']['metrics']
        assert client.get(f"/api/reviews/{review['id']}/sims/{run['run_id']}").json()['result']['kind'] == 'power' and client.get(f"/api/reviews/{review['id']}/sims/{'0' * 20}").status_code == 404
        assert client.get(f"/api/reviews/{review['id']}/sims/not-a-hash").status_code == 404
        overlay = run['result']['overlays'][0]
        assert overlay['id'] == 'field' and overlay['url'] == f"/api/reviews/{review['id']}/sims/{run['run']}/overlay/field" and overlay['nx'] == 4 and overlay['ny'] == 3 and overlay['unit'] == '%'
        got = client.get(overlay['url'])
        assert got.status_code == 200 and len(got.content) == 4 * 12 and got.headers['x-content-type-options'] == 'nosniff' and 'sandbox' in got.headers['content-security-policy']
        assert client.get(f"/api/reviews/{review['id']}/sims/{run['run']}/curve/curve_demo").json() == {'x': [0, 1], 'y': [1, 2]}
        base = f"/api/reviews/{review['id']}/sims"
        for path in (f"{base}/{run['run']}/overlay/nope", f"{base}/{run['run']}/overlay/..%2Fresult", f"{base}/{run['run']}/curve/result", f"{base}/{run['run']}/curve/..%2Fresult",
                     f"{base}/{run['run']}/curve/missing", f"{base}/..%2F..%2Fsims/overlay/field", f"{base}/ffffffffffffffffffff/overlay/field", f"{base}/{'0' * 20}/overlay/field", f"{base}/abc/overlay/field"):
            assert client.get(path).status_code == 404, path
        assert client.get(f"/api/reviews/{other_review['id']}/sims/{run['run']}/overlay/field").status_code == 404        # a run that belongs to another review is not served
        assert client.get('/api/reviews/nope/sims/' + run['run'] + '/overlay/field').status_code == 404
        assert client.get(f"/api/versions/{simenv.version['id']}/extent").json()['default'] == ['layout', 'power']


async def test_in_read_only_demo_mode_the_overlays_are_served_and_nothing_can_start(simenv, monkeypatch):
    review = await power_review(simenv)
    with client_for(simenv, monkeypatch, readonly=True) as client:
        run = [r for r in client.get(f"/api/reviews/{review['id']}/sims").json() if r.get('family') == 'board' and r.get('result')][0]
        assert client.get(run['result']['overlays'][0]['url']).status_code == 200
        started = client.post(f"/api/versions/{simenv.version['id']}/reviews", json={'goal': {}, 'depth': 'quick', 'extent': ['layout']})
        assert started.status_code == 403


async def test_the_start_route_takes_an_extent_and_refuses_a_bad_one(simenv):
    simenv.lab.rf = dict(NO_RADIO)
    simenv.use([])
    with client_for(simenv) as client:
        assert client.post(f"/api/versions/{simenv.version['id']}/reviews", json={'goal': GOAL, 'depth': 'quick', 'extent': ['antenna']}).status_code == 400
        assert client.post(f"/api/versions/{simenv.version['id']}/reviews", json={'goal': GOAL, 'depth': 'quick', 'extent': ['warp']}).status_code == 400
        assert client.post(f"/api/versions/{simenv.version['id']}/reviews", json={'goal': GOAL, 'depth': 'quick', 'extent': 'power'}).status_code == 422
        ok = client.post(f"/api/versions/{simenv.version['id']}/reviews", json={'goal': GOAL, 'depth': 'quick', 'extent': ['power']})
        assert ok.status_code == 200 and ok.json()['extent'] == ['layout', 'power']
        assert client.get(f"/api/reviews/{ok.json()['id']}").json()['extent'] == ['layout', 'power']


# ---- recheck: the same simulation compared across versions ----------------------------------------------------------------------------

async def test_a_recheck_compares_each_simulation_both_versions_chose(simenv):
    first = await power_review(simenv)
    second_version = simenv.store.create_version(simenv.project['id'], label='second')
    simenv.version = second_version
    second = await power_review(simenv)
    assert first['id'] != second['id']
    snapshot = simenv.store.latest_report_revision(second['id'])['snapshot']
    change = snapshot['sim_changes'][0]
    assert change['kind'] == 'power' and change['direction'] == 'same' and change['same_inputs'] and change['same_quality']
    assert 'Both simulated, same inputs and quality' in change['text'] and 'E-006' in change['text']
    brief = simenv.runners[-1].requests[0].user_text
    assert 'Version 1 simulated these' in brief and 'Run the unmodified base of each again with the same inputs' in brief
    assert 'What the summary and the antenna finding must say' not in brief and 'What the summary must say' in brief
    html = report.render_html(snapshot)
    assert 'Board simulations compared with the previous version' in html


async def test_a_recheck_with_different_inputs_says_the_difference_is_not_comparable(simenv):
    await power_review(simenv)
    simenv.version = simenv.store.create_version(simenv.project['id'], label='second')
    simenv.lab.rf = dict(NO_RADIO)
    script = power_script(*investigate_power(), load=False)
    second = await simenv.review(script, goal={'purpose': 'USB powered sensor board'}, extent=['layout', 'power'])
    change = simenv.store.latest_report_revision(second['id'])['snapshot']['sim_changes']
    assert not change or change[0]['direction'] in ('not_comparable', 'not_compared')


async def test_a_simulation_only_one_review_chose_is_not_compared(simenv):
    from trustpcb.review import simchange
    first = await power_review(simenv)
    simenv.version = simenv.store.create_version(simenv.project['id'], label='second')
    script = [BRIEF, step('advance_phase'), *clear(), step('advance_phase'), step('submit_summary', summary='Only the rules and layout were reviewed.', top_actions=['Check the board.'])]
    second = await simenv.review(script, goal=GOAL, extent=['layout'])
    assert simchange.compare(simenv.store, second['id'], first['id']) == []


# ---- follow-up: a what-if the user approves ---------------------------------------------------------------------------------------------

async def respond_when_asked(env, thread, allow):
    import asyncio
    for _ in range(400):
        pending = [e for e in env.events(thread['chat_id'], 'test') if e['payload']['request_id'] in env.manager.requests]
        if pending:
            return await env.manager.respond(pending[-1]['payload']['request_id'], allow=allow)
        await asyncio.sleep(0.01)
    raise AssertionError('no test approval was requested')


async def test_a_follow_up_runs_a_simulation_what_if_only_after_the_user_approves(simenv):
    import asyncio
    from trustpcb.review import followup
    review = await power_review(simenv)
    thread = followup.start_thread(store=simenv.store, manager=simenv.manager, review_id=review['id'], body={})
    wider = [{'op': 'widen_track', 'net': '+3V3', 'width_mm': 2.0}]
    script = [step('run_board_sim', kind='power', whatif=wider),
              step('propose_test', title='Widen the 3V3 track further', why='To see how much more margin a wider track gives.', estimated_seconds=30),
              step('run_board_sim', kind='power', label='base'),
              step('run_board_sim', kind='power', label='twice as wide', whatif=wider),
              step('end_followup', reply='Done.')]
    simenv.use(script)
    before = simenv.lab.count_sims('power')
    await followup.send_message(store=simenv.store, manager=simenv.manager, thread_id=thread['id'], text='What if the 3V3 track is 2 mm wide?', selected=[], request_id='r1')
    await respond_when_asked(simenv, thread, allow=True)
    await simenv.wait()
    rows = [e['payload'] for e in simenv.events(thread['chat_id'], 'tool_result')]
    named = [(r['name'], r['is_error'], r['content']) for r in rows]
    assert 'approval_required' in named[0][2] or 'not_allowed_in_phase' in named[0][2]
    assert simenv.events(thread['chat_id'], 'test_decided')[0]['payload']['allow'] is True
    runs = [r for r in named if r[0] == 'run_board_sim' and not r[1]]
    assert len(runs) == 2 and '"vs_base"' in runs[1][2] and simenv.lab.count_sims('power') == before + 1       # the base is the review's stored run; only the what-if is new
    assert '"cached":true' in runs[0][2] or '"cached":false' in runs[0][2]


async def test_a_declined_test_runs_no_simulation(simenv):
    from trustpcb.review import followup
    review = await power_review(simenv)
    thread = followup.start_thread(store=simenv.store, manager=simenv.manager, review_id=review['id'], body={})
    simenv.use([step('propose_test', title='Widen the 3V3 track', why='To see how much margin it gives.', estimated_seconds=30),
                step('run_board_sim', kind='power', whatif=[{'op': 'widen_track', 'net': '+3V3', 'width_mm': 2.0}]), step('end_followup', reply='No test was run.')])
    before = simenv.lab.count_sims()
    await followup.send_message(store=simenv.store, manager=simenv.manager, thread_id=thread['id'], text='What if it is wider?', selected=[], request_id='r2')
    await respond_when_asked(simenv, thread, allow=False)
    await simenv.wait()
    assert simenv.lab.count_sims() == before


# ---- the field solve against the closed form ----------------------------------------------------------------------------------------

def signal_row():
    return {'code': 'E-009', 'type': 'board_sim', 'values': {'kind': 'simulated', 'sim_kind': 'signal', 'trust': {'status': 'ok'}, 'quality': 'screen', 'cross_check': [
        {'net': 'ANT_FEED', 'analyzer_closed_form_ohm': 123.45, 'field_solver_on_the_real_board_median_ohm': 68.41}], 'beyond_quasi_static': {'ANT_FEED': 1.78}}}


def test_a_closed_form_impedance_is_only_the_estimate_a_field_solve_replaces():
    from trustpcb.review import simtrust
    codes = lambda text: [p.code for p in simtrust.signal_problems({'what': text}, universe=[signal_row()])]
    assert codes('The ANT_FEED track is 123 ohm wide of the target.') == ['closed_form_stated_as_fact']
    assert codes('The analyzer estimates 123 ohm for ANT_FEED.') == ['field_solve_not_stated']
    assert codes('ANT_FEED measures 68.4 ohm in the solve.') == ['quasi_static_limit_unsaid']
    assert codes('The analyzer estimates 123.45 ohm for ANT_FEED, but the field solve (E-009) gives 68.4 ohm. It is a quasi-static estimate, not valid at the RF band.') == []
    assert codes('The track is 0.3 mm wide.') == []
    assert simtrust.signal_problems({'what': 'It is 123 ohm.'}, universe=[]) == []


async def test_the_field_solve_reaches_the_class_width_finding(simenv):
    from trustpcb.review.handlers import Handlers
    from trustpcb.review import simmetrics as sm
    row = signal_row()
    simenv.lab.rf = dict(NO_RADIO)
    review = simenv.store.create_review(simenv.version['id'], goal=GOAL, depth='standard', extent=['layout', 'signal'])
    simenv.store.add_evidence(review['id'], 'board_sim', 'run_board_sim', {**row['values'], 'measurements': {'ANT_FEED.analyzer_closed_form_z0': {'value': 123.45, 'unit': 'ohm'}, 'ANT_FEED.z0_median': {'value': 68.41, 'unit': 'ohm'}}})
    handler = Handlers(types.SimpleNamespace(store=simenv.store, review_id=review['id']))
    out = handler.carry_field_solve({'what': 'The class asks 0.8 mm.'}, [{'net': 'ANT_FEED'}])
    assert 'A field solve of ANT_FEED (E-001) found a median of 68.41 ohm' in out['what'] and 'quasi-static' in out['what'] and out['evidence_ids'] == ['E-001']
    assert handler.carry_field_solve(out, [{'net': 'ANT_FEED'}]) == out                   # not added twice
