"""The real power, thermal and signal simulators behind the review's tools and the app's view: no fakes for the physics, a fake agent."""
import types
from pathlib import Path

import pytest

from gate_steps import clear
from simfakes import review_conftest
from test_board_sims import BRIEF, GOAL, NO_RADIO, step
from trustpcb import sims
from trustpcb.review import service, simview
from trustpcb.review.lab_sims import BoardSimLab, registry
from trustpcb.runners.scripted import ScriptedRunner
from trustpcb.sessions import SessionManager
from trustpcb.storage import Store

pytestmark = pytest.mark.skipif(not registry(), reason='the board simulators are not installed')
FakeLab, FakeClock, Env = review_conftest.FakeLab, review_conftest.FakeClock, review_conftest.Env


class RealSimLab(BoardSimLab, FakeLab):
    """The review's fake RF lab with the real board simulations."""

    def __init__(self, board, clock, store):
        FakeLab.__init__(self, board, clock)
        self.store = store

    async def load_board(self, *, version_id):
        return self.board

    @property
    def store_dir(self):
        return self.store.data_dir


@pytest.fixture
def realenv(tmp_path, sample_board):
    store = Store(tmp_path / 'data')
    clock = FakeClock()
    lab = RealSimLab(sample_board, clock, store)
    lab.rf = dict(NO_RADIO)
    holder = {}
    manager = SessionManager(store, runner_factory=lambda profile: ScriptedRunner(holder['env'].script))
    project = store.create_project('nRF node')
    version = store.create_version(project['id'], label='first')
    env = Env(store, manager, clock, lab, project, version)
    holder['env'] = env
    service.configure(lab=lab, clock=clock, poll_s=1.0)
    yield env
    service.configure()


async def test_the_lab_runs_each_real_simulator_and_an_identical_run_is_stored(realenv):
    lab = realenv.lab
    status = await lab.sim_status(version_id='v', goal={})
    assert set(status) == {'power', 'thermal', 'signal'} and all(s['available'] for s in status.values()) and 'widen_track' in status['power']['whatif_ops']
    first = await lab.run_board_sim(version_id='v', kind='power', goal={}, inputs={}, whatif=[], quality='screen')
    again = await lab.run_board_sim(version_id='v', kind='power', goal={}, inputs={}, whatif=[], quality='screen')
    assert not first['cached'] and again['cached'] and first['run_hash'] == again['run_hash'] and len(first['run_hash']) == 20
    only_stored = await lab.run_board_sim(version_id='v', kind='power', goal={}, inputs={'allowed_drop_pct': 3}, whatif=[], quality='screen', cache_only=True)
    assert only_stored['result'] is None
    confirm = await lab.run_board_sim(version_id='v', kind='power', goal={}, inputs={}, whatif=[], quality='confirm')
    assert confirm['run_hash'] != first['run_hash'] and confirm['result']['status'] == 'ok'


async def test_a_real_result_maps_to_the_rows_the_app_reads(realenv):
    lab = realenv.lab
    power = simview.web_result((await lab.run_board_sim(version_id='v', kind='power', goal={}, inputs={}, whatif=[], quality='screen'))['result'])
    loads = {k: v for k, v in power['metrics'].items() if v.get('group') == 'load'}
    assert loads and all(v['unit'] == 'mV' and v['current'] > 0 and v['current_unit'] == 'A' and v['ref'] in k for k, v in loads.items())
    assert power['trust']['status'] == 'ok' and all(set(c) >= {'check', 'ok'} for c in power['trust']['checks']) and power['overlays'][0]['id'].startswith('drop:')
    assert any(a['name'].split(' ')[0] == next(iter(loads.values()))['ref'] for a in power['assumptions'])        # an assumption begins with the part it is about
    thermal = simview.web_result((await lab.run_board_sim(version_id='v', kind='thermal', goal={}, inputs={}, whatif=[], quality='screen'))['result'])
    parts = [v for v in thermal['metrics'].values() if v.get('group') == 'part']
    assert all(p['unit'] == '°C' and 'ref' in p for p in parts) and 'peak_rise' in thermal['metrics']
    signal = simview.web_result((await lab.run_board_sim(version_id='v', kind='signal', goal={}, inputs={}, whatif=[], quality='screen'))['result'])
    assert {s['id'] for s in signal['series']} >= {'profile', 'tdr'} and all(len(s['x']) == len(s['y']) for s in signal['series'])
    assert any(v.get('group') == 'net' for v in signal['metrics'].values()) and 'worst_deviation' in signal['metrics'] and signal['cross_check']


async def test_the_overlay_grids_use_the_corner_of_the_first_cell(realenv):
    """x0, y0 is the corner of cell (0, 0): the first cell covers x0 to x0 + dx. A grid half a cell off would put every heat map beside its copper."""
    import numpy as np
    out = await realenv.lab.run_board_sim(version_id='v', kind='power', goal={}, inputs={}, whatif=[], quality='screen')
    board = realenv.lab.board
    for meta in out['result']['overlays']:
        raster = np.fromfile(Path(out['folder']) / meta['file'], dtype='<f4').reshape(meta['ny'], meta['nx'])
        assert raster.shape == (meta['ny'], meta['nx']) and np.isfinite(raster).any()
        j, i = np.argwhere(np.isfinite(raster))[len(np.argwhere(np.isfinite(raster))) // 2]
        x, y = meta['x0'] + (i + 0.5) * meta['dx'], meta['y0'] + (j + 0.5) * meta['dy']          # the centre of a painted cell lies on copper of the supply net
        near = [t for t in board.tracks if min(t.start[0], t.end[0]) - 1 <= x <= max(t.start[0], t.end[0]) + 1 and min(t.start[1], t.end[1]) - 1 <= y <= max(t.start[1], t.end[1]) + 1]
        zones = [z for z in board.zones if z.net]
        assert near or zones or board.pads
        assert board.bbox[0] - 3 <= meta['x0'] <= board.bbox[2] and board.bbox[1] - 3 <= meta['y0'] <= board.bbox[3]


async def test_thermal_takes_the_copper_loss_of_the_power_run(realenv):
    hotter = lambda kind, code, **extra: step('run_board_sim', kind=kind, suspect_id=code, label='worse', **extra)
    script = [BRIEF, step('advance_phase'), *clear(), step('advance_phase'),
              step('run_board_sim', kind='thermal', heat_from_power=True),
              step('open_investigation', kind='power', question='Does the supply copper drop too much?', suspects=[{'title': 'Thin supply copper', 'why': 'It carries the load.'}]),
              step('run_board_sim', kind='power', suspect_id='S1', label='base'),
              hotter('power', 'S1', whatif=[{'op': 'set_current', 'ref': 'U2', 'amps': 0.5}]),
              step('conclude_suspect', suspect_id='S1', verdict='inconclusive', reasoning='Only one test point was run and it made the drop worse.'),
              step('close_investigation', investigation_id='I1', outcome='no_improvement', summary='The one change made the drop worse; nothing was found to improve.'),
              step('open_investigation', kind='thermal', question='Where does the board get hot?', suspects=[{'title': 'The regulator', 'why': 'It dissipates the most.'}]),
              step('run_board_sim', kind='thermal', suspect_id='S2', label='with copper loss', heat_from_power=True),
              step('run_board_sim', kind='thermal', suspect_id='S2', label='without it', heat_from_power=False),
              hotter('thermal', 'S2', whatif=[{'op': 'set_part_power', 'ref': 'U2', 'watts': 0.9}])]
    review = await realenv.review(script, goal=GOAL, extent=['layout', 'power', 'thermal'])
    rows = [e['payload'] for e in realenv.events(review['chat_id'], 'tool_result')]
    named = [(r['name'], r['is_error'], r['content']) for r in rows]
    runs = [n for n in named if n[0] == 'run_board_sim']
    assert 'no_power_result' in runs[0][2] and runs[0][1]                                    # nothing to take heat from before the power run
    assert not any(n[1] for n in runs[1:]), [n[2][:300] for n in runs if n[1]]
    heat = [r for r in realenv.store.list_evidence(review['id']) if r['type'] == 'board_sim' and (r['values'] or {}).get('label') == 'with copper loss'][0]['values']
    item = next(i for i in heat['inputs_set'] if i['id'] == 'track_watts')
    assert item['handoff']['from'] and item['handoff']['tracks'] > 0 and item['source'] in ('board', 'default') and 'power simulation' in item['cite']
    assert any(a['name'] == 'copper loss from the power simulation' for a in heat['assumptions'])
    plain = [r for r in realenv.store.list_evidence(review['id']) if r['type'] == 'board_sim' and (r['values'] or {}).get('label') == 'without it'][0]['values']
    assert plain['run_hash'] != heat['run_hash'] and plain['measurements']['peak_rise']['value'] != heat['measurements']['peak_rise']['value']
