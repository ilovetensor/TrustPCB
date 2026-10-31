"""The ten stop rules, the budget table and the phase machine."""
import pytest

from trustpcb.review import budgets, evidence as ev
from trustpcb.review.budgets import BudgetTracker, Experiment, Ledger, config_for
from trustpcb.review.machine import PhaseError, PhaseMachine
from trustpcb.review.tools import MAX_RESULT_CHARS, ToolResult, bound, ok, page, validate


# ---- the table in spec section 8 -----------------------------------------------------------------

@pytest.mark.parametrize('depth,expected', [
    ('quick', (360, 0, 90, 0, 0, 0, 8, 3_000_000, 30_000)),
    ('standard', (3600, 2400, 160, 1, 5, 2, 14, 4_000_000, 40_000)),
    ('deep', (7200, 5400, 220, 3, 6, 4, 20, 6_000_000, 60_000)),
])
def test_default_budgets_match_the_spec(depth, expected):
    c = config_for(depth)
    assert (c.wall_s, c.solver_s, c.model_turns, c.investigations, c.experiments_per_investigation, c.confirm_runs, c.images, c.input_tokens, c.output_tokens) == expected
    assert c.phase_calls[2] == 10 and c.phase_calls[3] == {'quick': 60, 'standard': 80, 'deep': 80}[depth] and c.phase_images[3] == 8


def test_unknown_depth_is_rejected():
    with pytest.raises(ValueError):
        config_for('thorough')


class Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t


def test_tracker_counts_time_solver_tokens_and_walls():
    clock = Clock()
    tracker = BudgetTracker(config_for('standard'), clock)
    assert tracker.global_exhausted() is None
    clock.t = 3539
    assert tracker.global_exhausted() is None
    clock.t = 3541
    assert tracker.global_exhausted() == 'wall_clock' and not tracker.deadline_reached()
    clock.t = 3600
    assert tracker.deadline_reached()
    tracker = BudgetTracker(config_for('standard'), Clock())
    tracker.add_solver(2400)
    assert tracker.global_exhausted() == 'solver_time'
    tracker = BudgetTracker(config_for('standard'), Clock())
    tracker.add_usage({'input_tokens': 3_000_000, 'output_tokens': 10})
    tracker.add_usage({'usage': {'input_tokens': 900_000, 'cache_read_input_tokens': 1_000_000, 'output_tokens': 1}})
    assert tracker.global_exhausted() == 'input_tokens' and tracker.output_tokens == 11
    assert BudgetTracker(config_for('standard'), Clock()).images_left(3) == 8       # the phase cap is lower than the review cap


# ---- the ledger: rules 3, 4, 5, 6, 7 -----------------------------------------------------------------

def make_ledger():
    ledger = Ledger(config_for('standard'))
    inv = ledger.open_investigation('inv1', 'I1')
    a = ledger.add_suspect(inv, 's1', 'S1', 'Copper near the feed')
    b = ledger.add_suspect(inv, 's2', 'S2', 'Antenna length')
    return ledger, inv, a, b


def run(ledger, suspect, h, kind='whatif', *, ok=True, delta=None, key=None, repair=False, meets=False, quality='screen'):
    exp = Experiment(f'R-{h}', h, key or h, suspect.id, kind, h, quality, ok, {}, delta)
    return ledger.record(suspect, exp, meets_target=meets, base_hash='base', repair=repair)


def decide(ledger, suspect, h, kind='whatif', key=None, optimisation=True):
    return ledger.decide(suspect, h, key or h, kind, optimisation=optimisation, base_hash='base')


def test_whatif_needs_a_base_first():
    ledger, inv, a, b = make_ledger()
    assert decide(ledger, a, 'x').code == 'base_required'
    assert decide(ledger, a, 'base', 'base').action == 'run'
    run(ledger, a, 'base', 'base')
    assert decide(ledger, a, 'x').action == 'run'


def test_identical_scenario_is_cached_and_the_third_repeat_closes_the_suspect():
    ledger, inv, a, b = make_ledger()
    assert decide(ledger, a, 'base', 'base').action == 'run'
    run(ledger, a, 'base', 'base')
    first, second, third = (decide(ledger, a, 'base', 'base') for _ in range(3))
    assert [d.action for d in (first, second, third)] == ['cached'] * 3 and first.close is None and second.close is None
    assert third.close and a.status == 'looped' and third.rule == 'duplicate_scenario'          # the third duplicate closes the suspect
    assert decide(ledger, a, 'other').code == 'suspect_closed'
    # another suspect asking for the same base gets it free, and that first ask is not a repeat
    assert decide(ledger, b, 'base', 'base').action == 'cached' and b.status == 'open'


def test_ruled_out_suspect_cannot_be_reopened_unless_the_base_changes():
    ledger, inv, a, b = make_ledger()
    ledger.close_suspect(a, 'ruled_out', 'no effect', 'base-1')
    assert ledger.reopen_check('Copper near the FEED!', 'base-1') is True
    assert ledger.reopen_check('Copper near the feed', 'base-2') is False          # new base geometry: allowed again
    assert decide(ledger, a, 'x').code == 'suspect_closed'


def test_two_small_experiments_in_a_row_close_the_suspect_as_no_effect():
    ledger, inv, a, b = make_ledger()
    run(ledger, a, 'base', 'base')
    run(ledger, a, 'e1', delta={'s11_db': -0.3, 'f_hz': 2e6})
    assert a.status == 'open'
    run(ledger, a, 'e2', delta={'s11_db': -0.4, 'f_hz': -3e6})
    assert a.status == 'no_effect'
    # a large effect resets the streak
    ledger, inv, a, b = make_ledger()
    run(ledger, a, 'base', 'base')
    run(ledger, a, 'e1', delta={'s11_db': -0.3, 'f_hz': 1e6})
    run(ledger, a, 'e2', delta={'s11_db': -4.0, 'f_hz': 1e6})
    run(ledger, a, 'e3', delta={'s11_db': -0.2, 'f_hz': 1e6})
    assert a.status == 'open'
    # 0.5 dB or 5 MHz exactly is not "under"
    ledger, inv, a, b = make_ledger()
    run(ledger, a, 'base', 'base')
    run(ledger, a, 'e1', delta={'s11_db': -0.5, 'f_hz': 1e6})
    run(ledger, a, 'e2', delta={'s11_db': -0.1, 'f_hz': 6e6})
    assert a.status == 'open'


def test_meeting_the_target_ends_optimisation():
    ledger, inv, a, b = make_ledger()
    run(ledger, a, 'base', 'base')
    run(ledger, a, 'e1', delta={'s11_db': -9.0}, meets=True)
    assert decide(ledger, a, 'e2').code == 'target_met'
    assert decide(ledger, a, 'sens', optimisation=False).action == 'run'          # a sensitivity test is not optimising


def test_experiment_budget_per_investigation():
    ledger, inv, a, b = make_ledger()
    run(ledger, a, 'base', 'base')
    for i in range(5):
        assert decide(ledger, a, f'e{i}').action == 'run'
        run(ledger, a, f'e{i}', delta={'s11_db': -2.0 * (i + 1)})
    assert decide(ledger, a, 'e6').code == 'budget_exhausted'


def test_repair_limits():
    ledger, inv, a, b = make_ledger()
    run(ledger, a, 'base', 'base')
    run(ledger, a, 'e1h', ok=False, key='e1')                                    # the first attempt fails its trust checks
    repair = decide(ledger, a, 'e1h2', key='e1')
    assert repair.action == 'run' and repair.repair
    run(ledger, a, 'e1h2', ok=False, key='e1', repair=True)                       # the repair fails as well
    assert a.status == 'could_not_test'
    # two repairs per investigation
    ledger, inv, a, b = make_ledger()
    run(ledger, a, 'base', 'base')
    for key in ('k1', 'k2'):
        run(ledger, a, key + 'h', ok=False, key=key)
        assert decide(ledger, a, key + 'repair', key=key).repair
        run(ledger, a, key + 'repair', ok=True, key=key, repair=True, delta={'s11_db': -2.0})
    assert inv.repairs == 2
    run(ledger, b, 'k3h', ok=False, key='k3')
    assert b.status == 'could_not_test'                                          # no repair left in this investigation


def test_an_identical_failed_request_is_a_duplicate_not_a_repair():
    ledger, inv, a, b = make_ledger()
    run(ledger, a, 'base', 'base')
    run(ledger, a, 'e1', ok=False)
    assert decide(ledger, a, 'e1').action == 'cached'


def test_winner_is_the_best_trusted_improvement():
    ledger, inv, a, b = make_ledger()
    run(ledger, a, 'base', 'base')
    run(ledger, a, 'weak', delta={'s11_db': -0.6})
    run(ledger, a, 'good', delta={'s11_db': -5.0})
    run(ledger, b, 'best', delta={'s11_db': -7.0}, ok=False)
    assert ledger.winner(inv).hash == 'good'


# ---- the phase machine ----------------------------------------------------------------------------------

def test_phases_only_move_forward_and_skip_by_depth():
    machine = PhaseMachine('standard', True)
    assert machine.phase == 2
    assert machine.advance().phase == 3 and machine.advance().phase == 4
    change = machine.advance()                                   # nothing improved: confirm is skipped
    assert change.phase == 6 and change.skipped[0][0] == 5
    with pytest.raises(PhaseError):
        machine.advance()
    machine = PhaseMachine('standard', True)
    machine.advance(), machine.advance()
    machine.improved = True
    assert machine.advance().phase == 5
    quick = PhaseMachine('quick', True)
    quick.advance()
    assert quick.advance().phase == 6
    blind = PhaseMachine('deep', False, 'Chip antenna.')
    blind.advance()
    change = blind.advance()
    assert change.phase == 6 and [p for p, _ in change.skipped] == [4, 5] and change.skipped[0][1] == 'Chip antenna.'
    forced = PhaseMachine('standard', True)
    assert forced.force_report().phase == 6 and forced.force_report() is None


# ---- tools: schemas and size -----------------------------------------------------------------------------

def test_schema_validation_is_strict():
    schema = {'type': 'object', 'properties': {'a': {'type': 'integer', 'minimum': 1}, 'b': {'type': 'string', 'enum': ['x', 'y']}, 'c': {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 2}},
              'required': ['a'], 'additionalProperties': False}
    assert validate(schema, {'a': 1, 'b': 'x', 'c': ['q']}) == []
    errors = validate(schema, {'a': 0, 'b': 'z', 'c': ['q', 'r', 's'], 'd': 1})
    assert len(errors) == 4
    assert validate(schema, {}) == ['arguments.a is required']
    assert validate(schema, {'a': True}) == ['arguments.a must be integer']


def test_results_are_bounded_and_paged():
    big = ToolResult(text='{"items":[' + ','.join('"x%d"' % i for i in range(5000)) + ']}')
    assert len(bound(big).text) <= MAX_RESULT_CHARS and 'cut' in big.text
    p = page(list(range(25)), 3, 10)
    assert p == {'items': [20, 21, 22, 23, 24], 'page': 3, 'pages': 3, 'total': 25}
    assert page([], 1)['pages'] == 1
    assert len(ok({'a': 1}).text) < 20
