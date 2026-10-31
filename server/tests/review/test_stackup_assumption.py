"""An antenna finding that rests on an assumed stackup says so, by code, and cannot be rated high on screen-quality runs."""
import copy
import json
from pathlib import Path

from trustpcb.review import simtrust

SCRIPTS = Path(__file__).parent / 'scripts'


class Board:
    def __init__(self, **stack):
        self.stackup = stack


def row(type_, quality, **extra):
    return {'id': f'id-{type_}-{quality}', 'code': 'E-001', 'type': type_, 'values': {'kind': 'simulated', 'quality': quality, 'trust': {'status': 'ok'}, **extra}}


ASSUMED = Board(source='default', assumed=True, total_thickness_mm=1.6)
REAL = Board(source='board', assumed=False, total_thickness_mm=1.6)


def test_a_screen_run_on_an_assumed_stackup_cannot_support_high():
    problem = simtrust.antenna_severity_problem('high', [row('simulation', 'screen')], ASSUMED)
    assert problem and problem[0] == 'severity_rests_on_default' and 'confirm' in problem[1] and 'medium' in problem[1]
    assert simtrust.antenna_severity_problem('critical', [row('comparison', 'screen')], ASSUMED)


def test_confirm_quality_a_real_stackup_a_lower_severity_or_a_measurement_lift_the_cap():
    assert simtrust.antenna_severity_problem('high', [row('simulation', 'screen'), row('confirm', 'confirm')], ASSUMED) is None
    assert simtrust.antenna_severity_problem('high', [row('simulation', 'screen')], REAL) is None
    assert simtrust.antenna_severity_problem('medium', [row('simulation', 'screen')], ASSUMED) is None
    measured = {'id': 'm', 'code': 'E-009', 'type': 'candidate', 'values': {'kind': 'measured'}}
    assert simtrust.antenna_severity_problem('high', [row('simulation', 'screen'), measured], ASSUMED) is None
    assert simtrust.antenna_severity_problem('high', [measured], ASSUMED) is None                     # no antenna simulation cited: nothing to cap


def test_the_stackup_assumption_is_added_once_and_only_for_antenna_runs():
    lines = simtrust.stackup_assumption(ASSUMED, [row('simulation', 'screen')], [])
    assert len(lines) == 1 and 'Stackup assumed' in lines[0] and '1.6 mm' in lines[0] and 'dielectric' in lines[0]
    assert simtrust.stackup_assumption(ASSUMED, [row('simulation', 'screen')], lines) == lines
    own = ['The stackup is assumed (FR-4, 1.6 mm).']
    assert simtrust.stackup_assumption(ASSUMED, [row('simulation', 'screen')], own) == own            # the agent already said it
    assert simtrust.stackup_assumption(REAL, [row('simulation', 'screen')], []) == []
    assert simtrust.stackup_assumption(ASSUMED, [{'id': 'c', 'code': 'E-002', 'type': 'candidate', 'values': {'kind': 'measured'}}], []) == []
    assert simtrust.stackup_assumption(ASSUMED, [row('board_sim', 'screen', sim_kind='power')], []) == []     # board simulations have their own inputs row


def antenna_steps():
    steps = json.loads((SCRIPTS / 'refine_decline.json').read_text())
    return steps[:14]


async def test_a_live_review_refuses_high_on_screen_runs_and_lists_the_assumption_on_the_finding(env):
    finding = json.loads((SCRIPTS / 'refine_decline.json').read_text())[14]
    high, medium = copy.deepcopy(finding), copy.deepcopy(finding)
    high['args']['severity'], medium['args']['severity'] = 'high', 'medium'
    review = await env.review(antenna_steps() + [high, medium, {'tool': 'advance_phase', 'args': {}}])
    outcomes = [e['payload'] for e in env.events(review['chat_id'], 'tool_result') if e['payload'].get('name') == 'record_finding']
    assert 'severity_rests_on_default' in json.dumps(outcomes[0].get('content')) and not outcomes[1].get('is_error')
    stored = env.store.list_findings(review['id'])[0]
    assert stored['severity'] == 'medium'
    assert any('Stackup assumed' in line for line in stored['assumptions']), stored['assumptions']
