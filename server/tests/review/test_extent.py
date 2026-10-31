"""The extent chosen at review start: options, validation, the default, the stored field, the phase flow it gives and the budgets it adds."""
import pytest

from trustpcb.review import extent as ext
from trustpcb.review.budgets import config_for
from trustpcb.review.machine import PhaseMachine


def opts(rf=True, sims=('power', 'thermal', 'signal'), datasheets=False):
    return ext.options(rf_simulatable=rf, rf_reason='' if rf else 'No antenna was found.', sims={k: {'available': True} for k in sims}, datasheets_ready=datasheets)


def test_antenna_is_offered_only_when_the_board_can_be_simulated():
    on, off = {o['id']: o for o in opts(rf=True)}, {o['id']: o for o in opts(rf=False)}
    assert on['antenna']['available'] and not off['antenna']['available'] and 'No antenna' in off['antenna']['reason']
    assert on['layout']['always'] and on['layout']['available']


def test_datasheets_are_offered_only_when_keys_are_set():
    assert not {o['id']: o for o in opts(datasheets=False)}['datasheets']['available']
    assert {o['id']: o for o in opts(datasheets=True)}['datasheets']['available']


def test_the_default_is_layout_antenna_when_possible_and_power():
    assert ext.default_extent(opts()) == ['layout', 'antenna', 'power']
    assert ext.default_extent(opts(rf=False)) == ['layout', 'power']
    assert ext.default_extent(opts(rf=False, sims=())) == ['layout']          # a build without the simulators behaves as before: the rule review, plus the antenna where it is possible
    assert ext.default_extent(opts(rf=True, sims=())) == ['layout', 'antenna']


def test_normalize_adds_layout_orders_and_validates():
    o = opts()
    assert ext.normalize(['power', 'antenna'], o) == ['layout', 'antenna', 'power']
    assert ext.normalize([], o) == ['layout']
    assert ext.normalize(None, o) == ['layout', 'antenna', 'power']
    for bad in ('power', {'power': 1}, [1], ['warp']):
        with pytest.raises(ext.ExtentError):
            ext.normalize(bad, o)
    with pytest.raises(ext.ExtentError, match='Antenna simulation cannot be chosen'):
        ext.normalize(['antenna'], opts(rf=False))
    with pytest.raises(ext.ExtentError, match='Datasheets'):
        ext.normalize(['datasheets'], opts(datasheets=False))
    with pytest.raises(ext.ExtentError, match='Thermal'):
        ext.normalize(['thermal'], opts(sims=('power',)))
    assert ext.normalize(['datasheets', 'thermal', 'signal'], opts(datasheets=True)) == ['layout', 'thermal', 'signal', 'datasheets']


def test_method_line_for_chosen_and_for_older_reviews():
    assert 'rules and layout, antenna simulation, power delivery' in ext.method_line(['layout', 'antenna', 'power'])
    assert 'No simulation was chosen' in ext.method_line(['layout'])
    assert 'not recorded' in ext.method_line(None, stored=False)
    assert 'could not run: Docker is missing' in ext.method_line(['layout', 'antenna'], skipped={'antenna': 'Docker is missing'})


# ---- the phase flow of each extent --------------------------------------------------------------------------------------------

def flow(depth, antenna, sims, improved=False):
    machine = PhaseMachine(depth, antenna, '' if antenna else 'No antenna.', sims)
    machine.improved = improved
    seen, skipped = [machine.phase], {}
    while machine.phase < 6:
        change = machine.advance()
        skipped.update({p: r for p, r in change.skipped})
        seen.append(machine.phase)
    return seen, skipped


@pytest.mark.parametrize('antenna,sims,expected', [
    (False, (), [2, 3, 6]),                      # layout only: no Investigate, no Confirm
    (True, (), [2, 3, 4, 6]),                    # the antenna alone, nothing improved: as before
    (False, ('power',), [2, 3, 4, 6]),           # no radio, power chosen: Investigate runs
    (False, ('thermal', 'signal'), [2, 3, 4, 6]),
    (True, ('power', 'thermal', 'signal'), [2, 3, 4, 6]),
])
def test_each_extent_reaches_the_right_phases_standard(antenna, sims, expected):
    assert flow('standard', antenna, sims)[0] == expected


def test_confirm_follows_an_improvement_for_any_simulation():
    assert flow('standard', False, ('power',), improved=True)[0] == [2, 3, 4, 5, 6]
    assert flow('deep', True, ('power', 'signal'), improved=True)[0] == [2, 3, 4, 5, 6]


def test_quick_stops_after_phase_3_whatever_the_extent():
    assert flow('quick', True, ('power', 'thermal', 'signal'))[0] == [2, 3, 6]


def test_the_skip_reason_says_why_nothing_runs():
    _, skipped = flow('standard', False, ())
    assert skipped[4] == 'No antenna.' and skipped[5].startswith('No investigation')
    machine = PhaseMachine('standard', False, '', (), 'Power delivery could not run: no copper nets.')
    assert 'Power delivery could not run' in machine.skip_reason_no_simulation()
    assert PhaseMachine('standard', False).skip_reason_no_simulation() == 'Nothing on this board can be simulated.'


def test_board_simulations_add_investigations_calls_and_a_run_budget():
    plain = config_for('standard', True)
    with_sims = config_for('standard', True, ('power', 'thermal'))
    assert with_sims.investigations == plain.investigations + 2 and with_sims.sim_runs > 0 and with_sims.sim_confirm_runs > 0
    assert with_sims.phase_calls[4] > plain.phase_calls[4] and with_sims.phase_calls[5] > plain.phase_calls[5]
    assert plain.sim_runs == 0
    no_radio = config_for('standard', False, ('power',))
    assert no_radio.solver_s == 0 and no_radio.investigations == 1 and no_radio.experiments_per_investigation == plain.experiments_per_investigation and no_radio.refine_rounds == 0
    assert config_for('quick', True, ('power',)).sim_runs == 0
    assert config_for('deep', True, ('power',)).sim_runs > with_sims.sim_runs
