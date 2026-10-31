"""A Lab with fake board simulations (power, thermal, signal): no numpy, no solver, deterministic numbers, a real run folder so the routes have files to serve."""
import hashlib
import json
import struct
from pathlib import Path

import importlib.util
import sys

# The review folder's conftest, loaded by path: `from conftest import ...` finds another folder's conftest when several test folders run together.
_spec = importlib.util.spec_from_file_location('review_conftest_shared', Path(__file__).with_name('conftest.py'))
review_conftest = importlib.util.module_from_spec(_spec)
sys.modules['review_conftest_shared'] = review_conftest
_spec.loader.exec_module(review_conftest)
FakeLab = review_conftest.FakeLab
from trustpcb.review.lab import LabUnavailable

LABELS = {'power': 'Power delivery', 'thermal': 'Thermal', 'signal': 'Signal integrity'}
OPS = ['widen_track', 'add_via', 'set_current']
# the shapes the real simulators use: power has `id` and a dotted `path`; thermal and signal have a `name`; thermal also takes entries by key (part_power.U1)
SPEC = {
    'power': [{'id': 'current:U3@+3V3', 'path': 'currents.U3@+3V3', 'label': 'Current drawn by U3 from +3V3', 'unit': 'A', 'default': 1.0, 'source': 'default', 'cite': 'placeholder for an unclassified active part', 'needed': True},
              {'id': 'allowed_drop_pct', 'path': 'allowed_drop_pct', 'label': 'Allowed voltage drop', 'unit': '%', 'default': 5.0, 'source': 'default', 'cite': 'placeholder', 'needed': False},
              {'id': 'nominal_v:+3V3', 'path': 'nominal_v.+3V3', 'label': 'Nominal voltage of +3V3', 'unit': 'V', 'default': 3.3, 'source': 'board', 'cite': 'from the net name', 'needed': False}],
    'thermal': [{'name': 'ambient_c', 'label': 'ambient c', 'value': 25.0, 'unit': 'C', 'source': 'default', 'cite': 'still room air', 'required': False},
                {'name': 'part_power', 'label': 'power dissipated by a part', 'value': {}, 'unit': 'W', 'source': 'none', 'cite': 'from a datasheet', 'required': True},
                {'name': 'quality', 'value': 'screen'}],
    'signal': [{'name': 'nets', 'label': 'Nets to analyse', 'default': ['USB_DP'], 'unit': '', 'source': 'default'},
               {'name': 'rise_time_ns', 'label': 'Rise time', 'default': {'USB_DP': 4.0}, 'unit': 'ns', 'source': 'default'},
               {'name': 'quality', 'default': 'screen', 'unit': '', 'source': 'default'}],
}
BASE = {'power': 3.0, 'thermal': 30.0, 'signal': 12.0}


class FakeSimLab(FakeLab):
    def __init__(self, board, clock, data_dir, kinds=('power', 'thermal', 'signal')):
        super().__init__(board, clock)
        self.data_dir = Path(data_dir)
        self.kinds = {k: {'available': True, 'reason': '', 'label': LABELS[k], 'whatif_ops': OPS} for k in kinds}
        self.sim_calls = []
        self.sim_fail_trust = set()          # kinds whose runs fail their trust checks
        self.parts_ready = False

    def count_sims(self, kind=None):
        return sum(1 for c in self.sim_calls if kind in (None, c[0]) and not c[3])

    async def sim_status(self, *, version_id, goal=None):
        return {k: dict(v) for k, v in self.kinds.items()}

    async def sim_inputs(self, *, version_id, kind, goal=None, **kwargs):
        if kind not in self.kinds:
            raise LabUnavailable(f'{kind} is not installed')
        return [dict(item) for item in SPEC[kind]]

    def sim_model(self, kind, inputs, whatif, quality):
        value = BASE[kind]
        used, source = 1.0, 'default'
        if kind == 'power':
            given = ((inputs.get('currents') or {}).get('U3@+3V3'))
            if given is not None:
                used, source = (given['amps'] if isinstance(given, dict) else given), (given.get('source', 'user') if isinstance(given, dict) else 'user')
            value = BASE['power'] * used
        for op in whatif:
            if op['op'] == 'widen_track':
                value *= 0.6
            elif op['op'] == 'add_via':
                value *= 0.99
        if quality == 'confirm':
            value *= 1.04
        value = round(value, 4)
        ok_ = kind not in self.sim_fail_trust
        trust = {'status': 'ok' if ok_ else 'failed', 'checks': [{'name': 'benchmark', 'status': 'ok' if ok_ else 'failed',
                                                                'detail': 'matches the closed form' if ok_ else 'differs from the closed form by 30 %'}, {'name': 'zone_fill', 'status': 'flag', 'detail': 'one zone has no fill'}]}
        if kind == 'power':
            metrics = {'worst_drop_mv': {'value': round(value * 33, 3), 'unit': 'mV', 'label': 'Simulated worst drop'}, 'worst_drop_pct': {'value': value, 'unit': '%', 'label': 'Simulated worst drop as a percent of the nominal voltage'},
                       'worst_path_resistance_mohm': {'value': 41.5, 'unit': 'mOhm', 'label': 'Simulated resistance of the worst supply path'}, 'currents_assumed': {'value': 0 if source != 'default' else 1, 'unit': 'count', 'label': 'defaults'}}
            assumptions = [{'name': 'current U3 @ +3V3', 'value': used, 'unit': 'A', 'source': source, 'cite': 'placeholder' if source == 'default' else 'entered by the user'},
                           {'name': 'allowed voltage drop', 'value': 5.0, 'unit': '%', 'source': 'default', 'cite': 'placeholder'},
                           {'name': 'copper temperature', 'value': 20.0, 'unit': 'C', 'source': 'default', 'cite': 'resistivity at 20 C'},
                           {'name': 'copper thickness', 'value': 0.035, 'unit': 'mm', 'source': 'board', 'cite': 'stackup'}]
        elif kind == 'thermal':
            metrics = {'peak_temperature': {'value': round(25 + value, 3), 'unit': 'C', 'label': 'hottest board point, simulated'}, 'peak_rise': {'value': value, 'unit': 'K', 'label': 'hottest board point above ambient, simulated'},
                       'hottest_powered_part': {'value': 'U3', 'unit': '-', 'label': 'powered part'}}
            assumptions = [{'name': 'ambient_c', 'value': 25.0, 'unit': 'C', 'source': 'default', 'cite': 'still room air'}]
        else:
            metrics = {'USB_DP.z0_worst_deviation': {'value': -value, 'unit': 'percent', 'label': 'USB_DP: largest deviation from target'}, 'nets_analysed': {'value': 1, 'unit': 'count', 'label': 'Nets analysed'}}
            assumptions = [{'name': 'rise time USB_DP', 'value': 4.0, 'unit': 'ns', 'source': 'default', 'cite': 'USB 2.0 full speed'}]
        return {'kind': kind, 'status': 'ok', 'reason': None, 'trust': trust, 'metrics': metrics,
                'overlays': [{'id': 'field', 'label': 'Field', 'unit': '%', 'layer': 'F.Cu', 'x0': 0.0, 'y0': 0.0, 'dx': 1.0, 'dy': 1.0, 'nx': 4, 'ny': 3, 'vmin': 0.0, 'vmax': value * 1.2, 'file': 'field.bin', 'scale': 'sequential'}],
                'assumptions': assumptions, 'notes': [f'fake {kind} model'], 'solve_s': 2.0, 'detail': {}}

    async def run_board_sim(self, *, version_id, kind, goal, inputs, whatif, quality='screen', cache_only=False, **kwargs):
        if kind not in self.kinds:
            raise LabUnavailable(f'{kind} is not installed')
        run_inputs = dict(inputs)
        if quality == 'confirm':
            run_inputs['max_cells'] = 900000
        key = hashlib.sha256(json.dumps([kind, run_inputs, whatif], sort_keys=True).encode()).hexdigest()[:20]
        folder = self.data_dir / 'sims' / key
        stored = (folder / 'result.json').is_file()
        if not stored and cache_only:
            self.sim_calls.append((kind, key, quality, True))
            return {'result': None, 'run_hash': None, 'folder': None, 'cached': False, 'inputs': run_inputs}
        if stored:
            result = json.loads((folder / 'result.json').read_text())
        else:
            result = self.sim_model(kind, inputs, whatif, quality)
            folder.mkdir(parents=True, exist_ok=True)
            (folder / 'field.bin').write_bytes(struct.pack('<12f', *[float(i) for i in range(12)]))
            (folder / 'result.json').write_text(json.dumps(result))
            (folder / 'curve_demo.json').write_text(json.dumps({'x': [0, 1], 'y': [1, 2]}))
        self.clock.advance(1.0)
        self.sim_calls.append((kind, key, quality, stored))
        return {'result': result, 'run_hash': key, 'folder': str(folder), 'cached': stored, 'inputs': run_inputs}

    async def part_lookup(self, *, mpn=None, value=None, footprint=None, maker=None):
        if not self.parts_ready:
            raise LabUnavailable('No DigiKey or Mouser key is set.')
        return {'status': 'found', 'part': {'mpn': mpn, 'params': {'Current - Output': {'value': '2 A', 'source': 'digikey'}}}, 'candidates': [], 'notes': []}

    async def part_datasheet(self, *, mpn, question):
        if not self.parts_ready:
            raise LabUnavailable('No DigiKey or Mouser key is set.')
        if mpn == 'AMBIGUOUS':
            return {'status': 'ambiguous', 'mpn': mpn, 'reason': 'Several parts match.', 'candidates': [{'mpn': 'AMBIGUOUS-1'}, {'mpn': 'AMBIGUOUS-2'}]}
        url = f'https://ds.example/{mpn}.pdf'
        return {'status': 'ok', 'mpn': mpn, 'url': url, 'pages': 12, 'passages': [{'page': 3, 'quote': 'Maximum output current is 1.5 A. Dropout is 300 mV at full load.', 'url': url, 'score': 3.0}],
                'inputs': {'quiescent_current': {'label': 'Quiescent current', 'status': 'stated', 'typ': 0.055, 'unit': 'mA', 'page': 4, 'quote': 'Quiescent current 55 uA typ', 'url': url},
                           'max_output_current': {'label': 'Maximum output current', 'status': 'conflicting', 'candidates': []}}}
