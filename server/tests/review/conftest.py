"""Shared setup for the review tests: a real parsed board, a fake Lab, a fake clock and a scripted agent."""
import asyncio
import json
import sys
import types
from pathlib import Path

import pytest

from trustpcb.review.lab import BadMatchingRequest, LabUnavailable
from trustpcb.pcb.model import parse_board
from trustpcb.review import service
from trustpcb.runners.scripted import ScriptedRunner
from trustpcb.sessions import SessionManager
from trustpcb.storage import Store

sys.path.insert(0, str(Path(__file__).parent))     # test helpers such as gate_steps
ROOT = Path(__file__).resolve().parents[3]
SAMPLE = ROOT / 'samples' / 'nrf52840-node' / 'nrf52840_rf_iot_node.kicad_pcb'
SCRIPTS = Path(__file__).parent / 'scripts'
PIXEL = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=='


@pytest.fixture(scope='session')
def sample_board():
    return parse_board(SAMPLE, 'v1')


class FakeClock:
    """Time that only moves when told to. Sleeping advances it a little and yields, so watchdogs fire in milliseconds."""

    def __init__(self):
        self.t = 1000.0
        self.tick = 0.2

    def now(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds

    async def sleep(self, seconds):
        self.t += self.tick
        await asyncio.sleep(0.001)


def default_candidates():
    return [
        {'id': 'candidate:decoupling:1', 'rule': 'decoupling', 'title': 'Capacitor C13 is far from the U3 power pin', 'source': 'analyzer',
         'measurements': {'distance': {'value': 5.2, 'unit': 'mm'}, 'limit': {'value': 3.0, 'unit': 'mm'}}, 'targets': ['ref:C13', 'ref:U3'],
         'marks': [{'type': 'link', 'targets': ['ref:C13', 'ref:U3']}], 'severity_hint': 'medium'},
        {'id': 'candidate:decoupling:2', 'rule': 'decoupling', 'title': 'Capacitor C14 is far from the U3 power pin', 'source': 'analyzer',
         'measurements': {'distance': {'value': 4.1, 'unit': 'mm'}}, 'targets': ['ref:C14', 'ref:U3'], 'marks': [], 'severity_hint': 'low'},
        {'id': 'candidate:track_angle:1', 'rule': 'track_angle', 'title': 'Acute corner on a signal track', 'source': 'analyzer',
         'measurements': {'angle': {'value': 62.0, 'unit': 'deg'}}, 'targets': ['track:17bf021d'], 'marks': [], 'severity_hint': 'low'},
        {'id': 'candidate:antenna_keepout:1', 'rule': 'antenna_keepout', 'title': 'Ground pour inside the antenna clearance zone', 'source': 'analyzer',
         'measurements': {'overlap_area': {'value': 38.0, 'unit': 'mm2'}}, 'targets': ['zone:46abab45', 'net:ANT_CHIP'], 'marks': [], 'severity_hint': 'high'},
        {'id': 'candidate:drc_clearance:1', 'rule': 'drc_clearance', 'title': 'Clearance violation (min 0.2 mm; actual 0.15 mm)', 'source': 'kicad',
         'measurements': {'required': {'value': 0.2, 'unit': 'mm'}, 'actual': {'value': 0.15, 'unit': 'mm'}}, 'targets': ['point:30.0,14.0'], 'marks': [], 'severity_hint': 'medium'},
    ]


def accepted_pct(db):
    """100 x (1 - |S11|^2) for an S11 given in dB: the share of power accepted past the mismatch."""
    return round(100.0 * (1.0 - 10 ** (db / 10.0)), 1)


def default_model(scenario):
    """A stand-in antenna: clearing copper near the feed helps a lot; length tweaks and part removal do almost nothing."""
    quality = scenario['quality']
    f, s11, worst, bw = 2.52e9, -6.2, -3.1, 20e6
    for op in scenario.get('whatif_ops') or []:
        if op['op'] == 'clear_copper_region':
            f, s11, worst, bw = 2.447e9, -14.0, -12.4, 95e6
        elif op['op'] == 'adjust_antenna_length':
            f += op['delta_mm'] * 0.5e6
            s11 -= abs(op['delta_mm']) * 0.02
            worst -= abs(op['delta_mm']) * 0.02
        elif op['op'] == 'remove_nearby_part':
            f += 1.5e6
            s11 -= 0.1
            worst -= 0.1
    series = 1.7e9 + sum(op.get('delta_mm', 0) for op in scenario.get('whatif_ops') or []) * -20e6
    if scenario.get('material'):
        f *= (4.5 / scenario['material']['epsilon_r']) ** 0.5
        series *= (4.5 / scenario['material']['epsilon_r']) ** 0.5
    if quality == 'confirm':
        f, s11, worst = f - 2e6, s11 - 0.8 if worst < -10 else s11 + 0.2, worst - 0.7 if worst < -10 else worst + 0.2
    reactance = 62.0 if worst > -10 else 4.0         # an unmatched antenna looks inductive at the band centre
    return {'f_res': f, 's11_min_db': s11, 'band_worst_s11_db': worst, 'band_mean_s11_db': round(worst - 1.5, 2), 'bandwidth': {'width_hz': bw}, 'zin_at_res': {'re': 48.2, 'im': 3.1},
            'band_worst_accepted_power_pct': accepted_pct(worst), 'band_mean_accepted_power_pct': accepted_pct(worst - 1.5),
            'zin_at_band_centre': {'f_hz': 2.44175e9, 're': 31.0, 'im': reactance}, 's11_dips': [{'f_hz': f, 's11_db': s11}, {'f_hz': 2.254e9, 's11_db': -4.2}],
            'first_series_resonance_hz': series, 'dmax_dbi': 2.1,
            'efficiency_est': 0.62, 'trust': {'status': 'ok', 'reasons': []}, 'quality': quality}


class FakeLab:
    """The Lab interface with no solver. Records every call so tests can prove what was and was not run."""

    def __init__(self, board, clock):
        self.board, self.clock = board, clock
        self.calls = []
        self.candidates = default_candidates()
        self.rf = {'simulatable': True, 'reason': '', 'kind': 'printed', 'antenna_id': 'ant1', 'antenna_bbox': [27.0, 11.0, 33.0, 18.0],
                   'region_bbox': [15.0, 0.0, 45.0, 30.0], 'band_hz': [2.4e9, 2.4835e9], 'target_s11_db': -10.0}
        self.solve_s = {'screen': 60.0, 'confirm': 240.0}
        self.model = default_model
        self.fail_trust = 0          # the next N runs fail their trust checks
        self.baseline_error = None
        self.matching_error = None          # make tune_matching say it cannot run
        self.matching_bad = None            # make tune_matching refuse the parts it was given
        self.match_gain = {'screen': 9.0, 'confirm_locked': 8.0, 'confirm_free': 9.5}
        self.run_delay = 0.0

    def count(self, name):
        return sum(1 for c in self.calls if c[0] == name)

    async def ensure_baseline(self, *, version_id, progress=None):
        self.calls.append(('ensure_baseline', version_id))
        if self.baseline_error:
            raise RuntimeError(self.baseline_error)
        return {'default_rules': False, 'kicad_version': '10.0.0', 'warnings': []}

    async def load_board(self, *, version_id):
        for c in self.candidates:
            self.board.register_candidate(c['id'], c['targets'])
        return self.board

    async def list_candidates(self, *, version_id, part_roles=None):
        self.calls.append(('list_candidates', part_roles))
        return list(self.candidates)

    async def run_analyzer(self, *, version_id, rule, targets=None, params=None):
        self.calls.append(('run_analyzer', rule, targets))
        return [dict(c) for c in self.candidates if c['rule'] == rule and (not targets or set(targets) & set(c['targets']))]

    async def view_board(self, *, version_id, view, region=None, marks=None, width=1200):
        self.calls.append(('view_board', view, region, marks))
        return {'mime': 'image/png', 'data_b64': PIXEL, 'caption': f'{view} view'}

    async def rf_status(self, *, version_id, goal=None):
        return dict(self.rf)

    async def run_scenario(self, *, review_id, version_id, scenario, scenario_hash, investigation_id, max_seconds, on_progress):
        self.calls.append(('run_scenario', scenario_hash, scenario['quality']))
        seconds = self.solve_s[scenario['quality']]
        if on_progress:
            await on_progress({'progress': 0.5})
        self.clock.advance(seconds + self.run_delay)
        result = self.model(scenario)
        if self.fail_trust > 0:
            self.fail_trust -= 1
            result = {**result, 'trust': {'status': 'fail', 'reasons': ['only 14.0 cells per wavelength inside the substrate (need 20)']}}
        freqs = [2.3e9 + i * 0.01e9 for i in range(21)]
        curve = {'freq_hz': freqs, 's11_db': [max(-30.0, result['s11_min_db'] + abs(f - result['f_res']) / 1e8) for f in freqs]}
        return {'status': 'done', 'job_id': 'job-' + scenario_hash, 'cached': False, 'solve_s': seconds, 'result': {**result, 'solve_s': seconds}, 'curve': curve}

    async def tune_matching(self, *, review_id, version_id, result, params):
        """Matching on a stored run. `match_gain` is how many dB the band-worst S11 improves: by quality, and for explicit parts (locked) or the search (free)."""
        res = result.get('result') or {}
        quality = res.get('quality') or 'screen'
        locked = bool(params.get('changes'))
        self.calls.append(('tune_matching', 'locked' if locked else 'search', quality))
        if self.matching_error:
            raise LabUnavailable(self.matching_error)
        if self.matching_bad and locked:
            raise BadMatchingRequest(self.matching_bad)
        key = 'screen' if quality == 'screen' else ('confirm_locked' if locked else 'confirm_free')
        worst = res['band_worst_s11_db'] - self.match_gain[key]
        if locked:
            parts = [{'ref': c['ref'], 'position': 'series', 'kind': c['kind'], 'value': c['value'], 'was': '2.2 nH', 'now': f"{c['value'] * 1e12:g} pF" if c['kind'] == 'C' else 'leave empty'} for c in params['changes']]
        else:
            parts = [{'ref': 'L1', 'position': 'series', 'kind': 'C', 'value': 0.75e-12, 'was': '2.2 nH', 'now': '0.75 pF'}]
        return {'topology': 'series_c', 'components': [{'name': 'L1_series', 'kind': 'C', 'value': 0.75, 'unit': 'pF', 'ref': 'L1', 'position': 'series'}], 'parts': parts,
                'changes': [{'ref': p['ref'], 'position': p['position'], 'was': p['was'], 'now': p['now']} for p in parts],
                'predicted': {'s11_min_db': worst - 6, 'f_min_hz': 2.44e9, 'band_worst_db': worst, 'band_best_db': worst - 4, 'band_mean_db': round(worst - 1.5, 2),
                              'band_worst_accepted_power_pct': accepted_pct(worst), 'band_mean_accepted_power_pct': accepted_pct(worst - 1.5)},
                'meets_target': worst <= -10, 'target_db': -10, 'best_single_change': None, 'note': 'E24 values',
                'dips': [{'f_hz': 2.44e9, 's11_db': worst - 6}], 'first_series_resonance_hz': 2.4e9, 'zin_band_centre': {'f_hz': 2.44175e9, 're': 49.0, 'im': 2.0},
                'curve': {'freq_hz': [2.3e9, 2.5e9], 's11_db': [-3.0, worst]}}

    async def view_result(self, *, review_id, result, kind, params):
        self.calls.append(('view_result', kind))
        return {'mime': 'image/png', 'data_b64': PIXEL, 'caption': f'{kind} chart'}

    async def compare_results(self, *, a, b):
        return {'measurements': {}, 'note': 'compared'}

    async def try_placement(self, *, version_id, ref, target, rotation=None):
        self.calls.append(('try_placement', ref, target))
        return {'ref': ref, 'target': target, 'new_position': [30.0, 14.0], 'distances': [{'to': 'ref:U3', 'distance_mm': 2.1}], 'overlaps': [], 'conflicts': [],
                'rough_length_change_mm': -3.4, 'ghost': {'x': 30.0, 'y': 14.0}}

    async def read_datasheet(self, *, version_id, name=None, page=1):
        return {'names': []} if not name else {'name': name, 'page': 1, 'pages': 1, 'text': 'Recommended antenna feed impedance is 50 ohm.'}


def tool_call_with_registry_fallback(original):
    """The scripted runner looks tools up in `specs()`, which lists only the allowed ones. Fall back to the
    registry so a script can try an out-of-phase tool and see `not_allowed_in_phase`, as a real runner would."""
    async def call_tool(registry, name, args):
        try:
            return await original(registry, name, args)
        except KeyError:
            if hasattr(registry, 'call'):
                return await registry.call(name, args)
            raise
    return call_tool


@pytest.fixture(autouse=True)
def scripted_fallback(monkeypatch):
    from trustpcb.runners import scripted
    monkeypatch.setattr(scripted, 'call_tool', tool_call_with_registry_fallback(scripted.call_tool))


class Env:
    def __init__(self, store, manager, clock, lab, project, version):
        self.store, self.manager, self.clock, self.lab, self.project, self.version = store, manager, clock, lab, project, version
        self.script = []
        self.runners = []

    def use(self, script):
        self.script = json.loads(Path(script).read_text()) if isinstance(script, (str, Path)) and Path(script).exists() else script
        return self

    async def wait(self):
        current = self.manager.active_for_project(self.project['id'])
        if current and current.task:
            await asyncio.wait_for(asyncio.shield(current.task), timeout=20)

    async def review(self, script=None, depth='standard', goal=None, wait=True):
        if script is not None:
            self.use(script)
        started = await service.start_review(store=self.store, manager=self.manager, version_id=self.version['id'], goal=goal or {'purpose': 'Battery Bluetooth sensor', 'band_hz': [2.4e9, 2.4835e9], 'target_s11_db': -10}, depth=depth)
        if wait:
            await self.wait()
        return self.store.get_review(started['id'])

    def events(self, chat_id, *types):
        rows = self.store.events(chat_id)
        return [e for e in rows if not types or e['type'] in types]

    def tool_results(self, chat_id):
        return [e['payload'] for e in self.events(chat_id, 'tool_result')]

    def names(self, chat_id):
        return [e['type'] for e in self.events(chat_id)]


@pytest.fixture
def env(tmp_path, sample_board):
    store = Store(tmp_path / 'data')
    clock = FakeClock()
    lab = FakeLab(sample_board, clock)
    holder = {}

    def factory(profile):
        runner = ScriptedRunner(holder['env'].script)
        holder['env'].runners.append(runner)
        return runner

    manager = SessionManager(store, runner_factory=factory)
    project = store.create_project('nRF node')
    version = store.create_version(project['id'], label='first')
    environment = Env(store, manager, clock, lab, project, version)
    holder['env'] = environment
    service.configure(lab=lab, clock=clock, poll_s=1.0)
    yield environment
    service.configure()


@pytest.fixture
def base_model():
    """The fake antenna model, for tests that wrap it in a variation of their own."""
    return default_model


@pytest.fixture
def fakes():
    """The fakes themselves, for tests that build their own app."""
    return types.SimpleNamespace(Clock=FakeClock, Lab=FakeLab)
