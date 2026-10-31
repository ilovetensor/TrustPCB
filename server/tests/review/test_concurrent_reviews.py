"""Several reviews at once on one instance: scripted agent, fake lab, fake clock. No model, no solver."""
import asyncio
from pathlib import Path

import pytest

from trustpcb.review import followup, service
from trustpcb.runners.base import RunRequest
from trustpcb.sessions import SessionConflict

SCRIPTS = Path(__file__).parent / 'scripts'
GOAL = {'purpose': 'Battery Bluetooth sensor', 'band_hz': [2.4e9, 2.4835e9], 'target_s11_db': -10}


@pytest.fixture(autouse=True)
def steady_clock(env):
    """Every review's watchdog sleeps on the one shared fake clock; with several reviews it would race ahead of the real events. Here time moves only when a test says so."""
    env.clock.tick = 0.0


def add_version(env, project=None, name='other'):
    project = project or env.store.create_project(name)
    return env.store.create_version(project['id'], label=name)


async def start(env, version, depth='quick', script=None):
    if script is not None:
        env.use(script)
    return await service.start_review(store=env.store, manager=env.manager, version_id=version['id'], goal=GOAL, depth=depth)


async def finish_all(env, timeout=30):
    tasks = [t.task for t in env.manager.turns.values() if t.task]
    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout)


async def until(check, timeout=10):
    for _ in range(int(timeout / 0.01)):
        if check():
            return
        await asyncio.sleep(0.01)
    raise AssertionError('condition not reached')


async def test_two_reviews_run_at_once_each_with_its_own_state(env):
    other = add_version(env, name='second board')                          # another project
    sibling = add_version(env, env.project, 'v2')                           # and another version in the first project
    env.lab.solve_s = {'screen': 5.0, 'confirm': 10.0}                      # short runs: the fake clock is shared by all three reviews
    env.use(SCRIPTS / 'standard_success.json')
    started = [await start(env, v, depth='standard') for v in (env.version, other, sibling)]
    assert {r['status'] for r in started} <= {'queued', 'running'} and len(env.manager.turns) == 3
    await finish_all(env)
    reviews = [env.store.get_review(r['id']) for r in started]
    assert [r['end_state'] for r in reviews] == ['complete'] * 3 and [r['status'] for r in reviews] == ['completed'] * 3, [r['summary']['end'] for r in reviews]
    assert len({r['chat_id'] for r in reviews}) == 3 and len(env.runners) == 3        # a runner session of its own each
    for r in reviews:
        findings = env.store.list_findings(r['id'])
        assert [f['code'] for f in findings][:2] == ['F-001', 'F-002']             # numbering is per review: nothing leaked between them
        assert [e['payload']['review_id'] for e in env.events(r['chat_id'], 'report_ready')] == [r['id']]
        assert env.store.latest_report_revision(r['id'])['number'] == 1
    assert len({id(r) for r in env.runners}) == 3 and not env.manager.turns


async def test_reviews_past_the_limit_wait_visibly_and_all_finish(env):
    env.manager.max_reviews = 1
    versions = [env.version, add_version(env, name='b'), add_version(env, name='c')]
    started = [await start(env, v, script=SCRIPTS / 'quick_complete.json') for v in versions]
    await until(lambda: any(env.manager.waiting) and len(env.manager.waiting) == 2 or not env.manager.turns)
    assert [r['status'] for r in started] == ['queued'] * 3           # nothing is marked running before it has a slot
    infos = [env.manager.run_info(r['chat_id']) for r in started]
    assert [i and i['state'] for i in infos][1:] == ['queued', 'queued'] and [i['position'] for i in infos[1:]] == [1, 2]
    await finish_all(env)
    reviews = [env.store.get_review(r['id']) for r in started]
    assert [r['end_state'] for r in reviews] == ['complete'] * 3
    for r in reviews[1:]:
        queued = env.events(r['chat_id'], 'queued')
        assert queued and queued[0]['payload']['limit'] == 1
    first_phase = [env.events(r['chat_id'], 'phase_changed')[0]['sequence'] for r in reviews]
    finished = [env.events(r['chat_id'], 'completed')[0]['sequence'] for r in reviews]
    assert env.events(reviews[1]['chat_id'])[-1]['type'] == 'completed'
    assert all(env.events(reviews[i]['chat_id'], 'completed') for i in range(3)) and first_phase and finished


async def test_stopping_a_waiting_review_ends_it_with_a_report_and_it_never_ran(env):
    env.manager.max_reviews = 1
    env.use(SCRIPTS / 'standard_success.json')
    running = await start(env, env.version, depth='standard')
    waiting = await start(env, add_version(env, name='b'), depth='standard')
    await until(lambda: env.manager.run_info(waiting['chat_id']) and env.manager.run_info(waiting['chat_id'])['state'] == 'queued')
    await env.manager.cancel(waiting['chat_id'])
    await until(lambda: env.store.get_review(waiting['id'])['status'] == 'canceled')
    row = env.store.get_review(waiting['id'])
    assert row['end_state'] == 'canceled' and env.store.latest_report_revision(waiting['id']) is not None
    assert not env.events(waiting['chat_id'], 'phase_changed') and env.events(waiting['chat_id'], 'canceled')
    await finish_all(env)
    assert env.store.get_review(running['id'])['end_state'] == 'complete'      # the other review was not touched


async def test_a_second_review_of_the_same_version_is_refused_and_names_the_first(env):
    env.manager.max_reviews = 1
    first = await start(env, env.version, script=SCRIPTS / 'quick_complete.json')
    with pytest.raises(SessionConflict) as refused:
        await start(env, env.version)
    assert first['id'][:6] in str(refused.value)
    await finish_all(env)
    again = await start(env, env.version)                                        # fine once the first has ended
    await finish_all(env)
    assert env.store.get_review(again['id'])['end_state'] == 'complete'


async def test_chat_stays_locked_only_for_the_review_that_is_running(env):
    env.manager.max_reviews = 3
    done = await env.review(SCRIPTS / 'quick_complete.json', depth='quick')
    running = await start(env, add_version(env, name='b'), depth='standard', script=SCRIPTS / 'standard_success.json')
    thread = followup.start_thread(store=env.store, manager=env.manager, review_id=done['id'], body={})      # open for the finished review
    assert thread['review_id'] == done['id']
    with pytest.raises(ValueError, match='still running'):
        followup.start_thread(store=env.store, manager=env.manager, review_id=running['id'], body={})        # locked for the running one
    await finish_all(env)


# ---- waiting for the solver is not charged ----------------------------------------------------------------

@pytest.fixture
def queued_lab(env, sample_board, fakes):
    """A lab whose simulations wait for the solver for `wait_s` of (fake) time before they start, as behind other reviews."""

    class QueuedLab(fakes.Lab):
        wait_s = 0.0
        probe = None

        async def run_scenario(self, **kwargs):
            feed = kwargs['on_progress']
            await feed({'phase': 'waiting', 'position': 1, 'ahead': 1, 'fraction': 0.0})
            if self.probe:
                self.probe()
            self.clock.advance(self.wait_s)
            await asyncio.sleep(0.02)                                          # the budget monitor looks while the review waits
            await feed({'phase': 'started', 'waited_s': self.wait_s, 'fraction': 0.0})
            return await super().run_scenario(**kwargs)

    lab = QueuedLab(sample_board, env.clock)
    env.lab = lab
    service.configure(lab=lab, clock=env.clock, poll_s=1.0)
    return lab


async def test_time_spent_waiting_for_the_solver_is_not_charged_to_the_wall_clock(env, queued_lab):
    queued_lab.wait_s = 50 * 60.0                                               # 50 minutes in the queue before each of four runs; the standard wall budget is one hour
    states = []

    def probe():
        turn = next(iter(env.manager.turns.values()))
        states.append(env.manager.run_info(turn.chat_id))

    queued_lab.probe = probe
    review = await env.review(SCRIPTS / 'standard_success.json', depth='standard')
    assert review['end_state'] == 'complete' and review['summary']['end']['reason'] == 'completed', review['summary']['end']
    assert states and all(s['waiting_for_solver'] and s['state'] == 'running' for s in states)      # lists could say "Waiting for the solver"
    budget = env.events(review['chat_id'], 'budget')[-1]['payload']
    assert budget['solver_wait_s']['used'] >= 3 * 50 * 60 and budget['solver_wait_s']['waiting'] is False
    assert budget['wall_s']['used'] < 40 * 60 and budget['solver_s']['used'] < 40 * 60              # real work only; and the solver budget has only run time
    waiting_flags = [e['payload']['solver_wait_s']['waiting'] for e in env.events(review['chat_id'], 'budget')]
    assert True in waiting_flags and waiting_flags[-1] is False
    phases = [e['payload'].get('phase') for e in env.events(review['chat_id'], 'sim_progress')]
    assert 'waiting' in phases and 'started' in phases
    assert not env.manager.run_info(review['chat_id'])


async def test_waiting_has_a_hard_limit_after_which_it_counts(env, queued_lab, monkeypatch):
    monkeypatch.setenv('TRUSTPCB_MAX_QUEUE_WAIT_MIN', '30')                    # the cap: 30 minutes of waiting are free, the rest is charged
    queued_lab.wait_s = 3 * 3600.0
    review = await env.review(SCRIPTS / 'standard_success.json', depth='standard')
    assert review['summary']['end']['reason'] == 'hard_deadline' and review['end_state'] == 'complete_with_limits'


def test_budget_tracker_wait_accounting(fakes):
    from trustpcb.review.budgets import BudgetTracker, config_for

    clock = fakes.Clock()
    tracker = BudgetTracker(config_for('standard'), clock, wait_cap_s=600)
    clock.advance(100)
    tracker.begin_wait()
    clock.advance(400)
    assert tracker.elapsed() == 100 and tracker.waiting                        # an open wait is not charged either
    tracker.begin_wait()                                                       # two simulations of one review waiting count once
    clock.advance(100)
    tracker.end_wait()
    assert tracker.waiting
    tracker.end_wait()
    assert not tracker.waiting and tracker.elapsed() == 100
    tracker.begin_wait()
    clock.advance(500)                                                         # 1000 s waited in all, 600 s of it free
    tracker.end_wait()
    assert tracker.credited_wait() == 600 and tracker.elapsed() == 1100 - 600
    tracker.end_wait()                                                         # an unmatched end changes nothing
    assert tracker.snapshot()['solver_wait_s'] == {'used': 600.0, 'cap': 600, 'waiting': False}


# ---- restart ----------------------------------------------------------------------------------------------

async def test_a_stopping_app_leaves_an_interrupted_review_that_one_click_restarts(env):
    env.use([{'text': 'working'}, {'sleep': 30}])
    first = await start(env, env.version, depth='quick')
    await until(lambda: any(e['payload'].get('phase') == 2 for e in env.events(first['chat_id'], 'phase_changed')))
    await env.manager.shutdown()                                               # what the app stopping does to a run
    row = env.store.get_review(first['id'])
    assert row['status'] == 'interrupted' and row['end_state'] is None and env.store.latest_report_revision(first['id']) is None
    last = env.events(first['chat_id'], 'failed')[-1]['payload']
    assert last['interrupted'] is True and not env.events(first['chat_id'], 'canceled')
    with pytest.raises(ValueError, match='interrupted'):
        followup.start_thread(store=env.store, manager=env.manager, review_id=first['id'], body={})
    env.manager.stopping = False                                               # the app is back (a new manager in real life)
    env.use(SCRIPTS / 'quick_complete.json')
    again = await service.restart_review(store=env.store, manager=env.manager, review_id=first['id'])
    assert again['id'] != first['id'] and again['version_id'] == first['version_id'] and again['depth'] == 'quick' and again['goal'] == GOAL
    await finish_all(env)
    assert env.store.get_review(again['id'])['end_state'] == 'complete'
    with pytest.raises(ValueError, match='interrupted'):
        await service.restart_review(store=env.store, manager=env.manager, review_id=again['id'])       # only an interrupted review restarts
