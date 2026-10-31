"""Runs belong to the server: several at once, a visible queue past the limit, nothing a browser does stops one, and a stopping app interrupts instead of cancelling."""
import asyncio
import json
import sqlite3
import threading
import time

import httpx
import pytest
import uvicorn

from trustpcb.api import create_app
from trustpcb.review import service
from trustpcb.runners.base import RunRequest
from trustpcb.runners.scripted import ScriptedRunner
from trustpcb.sessions import SessionManager, max_reviews_setting
from trustpcb.storage import Store

TERMINAL = ('completed', 'failed', 'canceled')


def new_review(store, name='p'):
    project = store.create_project(name)
    return store.create_review(store.create_version(project['id'])['id'])


async def settled(store, chat_id, timeout=5):
    for _ in range(int(timeout / 0.01)):
        done = [e for e in store.events(chat_id) if e['type'] in TERMINAL]
        if done:
            return done[0]
        await asyncio.sleep(0.01)
    raise AssertionError('run did not finish')


async def until(check, timeout=5):
    for _ in range(int(timeout / 0.01)):
        if check():
            return
        await asyncio.sleep(0.01)
    raise AssertionError('condition not reached')


def test_the_limit_is_a_setting_with_a_small_default(monkeypatch):
    monkeypatch.delenv('TRUSTPCB_MAX_REVIEWS', raising=False)
    assert max_reviews_setting() == 3
    monkeypatch.setenv('TRUSTPCB_MAX_REVIEWS', '5')
    assert max_reviews_setting() == 5
    for bad in ('0', '-2', 'many', '2.5'):
        monkeypatch.setenv('TRUSTPCB_MAX_REVIEWS', bad)
        assert max_reviews_setting() == 3


async def test_reviews_beyond_the_limit_wait_in_a_visible_queue_in_order(tmp_path):
    store = Store(tmp_path / 'd')
    manager = SessionManager(store, max_reviews=2)
    reviews = [new_review(store, f'p{i}') for i in range(4)]       # different projects, as well as the same project below
    gates = [asyncio.Event() for _ in reviews]
    worked: list[int] = []

    def work_for(i):
        async def work(ctx):
            await ctx.wait_for_slot()
            worked.append(i)
            store.update_review(reviews[i]['id'], status='running')
            await gates[i].wait()
        return work

    for i, review in enumerate(reviews):
        await manager.start(review['chat_id'], f'r{i}', work_for(i), text='go')
    await until(lambda: len(worked) == 2 and len(manager.waiting) == 2)
    assert worked == [0, 1] and manager.working_reviews() == 2
    assert [manager.run_info(r['chat_id'])['state'] for r in reviews] == ['running', 'running', 'queued', 'queued']
    assert [manager.run_info(r['chat_id'])['position'] for r in reviews[2:]] == [1, 2]
    assert store.get_chat(reviews[2]['chat_id'])['status'] == 'queued'
    queued = [e for e in store.events(reviews[3]['chat_id']) if e['type'] == 'queued']
    assert queued[-1]['payload']['position'] == 2 and queued[-1]['payload']['limit'] == 2 and 'Waiting for a free review slot' in queued[-1]['payload']['message']
    # a waiting review can be stopped without ever working
    await manager.cancel(reviews[3]['chat_id'])
    assert (await settled(store, reviews[3]['chat_id']))['type'] == 'canceled' and 3 not in worked
    # the first to finish frees a slot for the first in line
    gates[0].set()
    await settled(store, reviews[0]['chat_id'])
    await until(lambda: worked == [0, 1, 2])
    assert manager.run_info(reviews[2]['chat_id'])['state'] == 'running' and store.get_chat(reviews[2]['chat_id'])['status'] == 'running'
    gates[1].set(), gates[2].set()
    for review in reviews[1:3]:
        assert (await settled(store, review['chat_id']))['type'] == 'completed'
    assert not manager.turns and not manager.waiting and manager.working_reviews() == 0


async def test_two_runs_in_one_project_go_side_by_side(tmp_path):
    store = Store(tmp_path / 'd')
    manager = SessionManager(store, max_reviews=3)
    project = store.create_project('p')
    reviews = [store.create_review(store.create_version(project['id'])['id']) for _ in range(2)]
    gate = asyncio.Event()

    async def work(ctx):
        await ctx.wait_for_slot()
        await gate.wait()

    for i, review in enumerate(reviews):
        await manager.start(review['chat_id'], f'r{i}', work)
    await until(lambda: manager.working_reviews() == 2)
    gate.set()
    for review in reviews:
        await settled(store, review['chat_id'])


async def test_a_stopping_app_interrupts_runs_it_does_not_cancel_them(tmp_path):
    store = Store(tmp_path / 'd')
    manager = SessionManager(store, runner_factory=lambda p: ScriptedRunner([{'text': 'working'}, {'sleep': 30}]), max_reviews=1)
    running, waiting = new_review(store, 'a'), new_review(store, 'b')

    async def work(ctx):
        await ctx.wait_for_slot()
        store.update_review(ctx_review[ctx.chat_id], status='running')
        await ctx.run_agent(RunRequest(None, 'sys', 'go', None, 5, {}))

    ctx_review = {running['chat_id']: running['id'], waiting['chat_id']: waiting['id']}
    for r in (running, waiting):
        await manager.start(r['chat_id'], 'x', work)
    await until(lambda: 'working' in str(store.events(running['chat_id'])) and manager.waiting)
    await manager.shutdown()
    for r in (running, waiting):
        last = [e for e in store.events(r['chat_id']) if e['type'] in TERMINAL][-1]
        assert last['type'] == 'failed' and last['payload']['interrupted'] is True and 'stopped' in last['payload']['message']
        assert store.get_review(r['id'])['status'] == 'interrupted' and store.get_chat(r['chat_id'])['status'] == 'interrupted'
        assert [t['status'] for t in [store.find_turn(r['chat_id'], 'x')]] == ['interrupted']
    assert not manager.turns
    with pytest.raises(Exception):
        await manager.start(running['chat_id'], 'later', work)         # a stopping app starts nothing new


def test_startup_settles_rows_that_say_running_with_nothing_behind_them(tmp_path):
    store = Store(tmp_path / 'd')
    review = new_review(store)
    store.update_review(review['id'], status='running')                # crashed between creating the review and starting its run
    thread = store.create_thread(review['id'])
    store.update_thread(thread['id'], status='running')
    store.recover_interrupted()
    assert store.get_review(review['id'])['status'] == 'interrupted' and store.get_thread(thread['id'])['status'] == 'interrupted'


def test_the_database_takes_concurrent_writers(tmp_path):
    """Several reviews write events and rows from several threads at once: WAL, a long busy timeout, and no 'database is locked'."""
    store = Store(tmp_path / 'd')
    with store._db() as db:
        assert db.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
        assert db.execute('PRAGMA busy_timeout').fetchone()[0] >= 30000
    reviews = [new_review(store, f'p{i}') for i in range(4)]
    errors: list[BaseException] = []

    def writer(review):
        try:
            for n in range(60):
                store.append_event(review['chat_id'], None, 'budget', {'n': n})
                store.update_review(review['id'], phase=str(n))
                store.create_sim_job(f'h{n}', review_id=review['id'])
        except BaseException as exc:   # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(r,)) for r in reviews for _ in range(2)]
    [t.start() for t in threads]
    [t.join(60) for t in threads]
    assert not errors, errors
    for review in reviews:
        sequence = [e['payload']['n'] for e in store.events(review['chat_id'])]
        assert len(sequence) == 120 and len(store.list_sim_jobs(review_id=review['id'])) == 120
        assert [e['sequence'] for e in store.events(review['chat_id'])] == sorted(e['sequence'] for e in store.events(review['chat_id']))


# ---- over a real socket: a browser coming and going --------------------------------------------------

@pytest.fixture
def live(tmp_path, monkeypatch):
    app = create_app(data_dir=tmp_path / 'data', token='secret', poll=0.01, keepalive=0.1,
                     runner_factory=lambda profile: ScriptedRunner([{'text': 'begin '}, {'sleep': 1.5}, {'text': 'later'}]))

    async def start_review(*, store, manager, version_id, goal, depth, **_):
        review = store.create_review(version_id, goal=goal, depth=depth)
        store.update_review(review['id'], status='running')

        async def work(ctx):
            await ctx.run_agent(RunRequest(None, 'sys', 'go', None, 5, {}))
            store.update_review(review['id'], status='completed')

        started = await manager.start(review['chat_id'], f'review-{review["id"]}', work, text='go')
        return {'id': review['id'], **started}

    monkeypatch.setattr(service, 'start_review', start_review)
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=0, log_level='warning', lifespan='on'))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(500):
        if server.started and server.servers:
            break
        time.sleep(0.01)
    yield app, f'http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}'
    server.should_exit = True
    thread.join(10)


HEADERS = {'Host': '127.0.0.1:8118', 'X-TrustPCB-Token': 'secret'}


def read_stream(client, url, until_type, timeout=10):
    """Frames of a stream until one of the wanted type arrives (then the connection is closed by leaving the block)."""
    seen: list[dict] = []
    deadline = time.time() + timeout
    with client.stream('GET', url, headers=HEADERS) as response:
        buffer = ''
        for chunk in response.iter_text():
            buffer += chunk
            while '\n\n' in buffer:
                frame, buffer = buffer.split('\n\n', 1)
                if frame.startswith('id: '):
                    seen.append(json.loads(frame.split('data: ', 1)[1]))
            if any(e['type'] == until_type or (until_type == 'text' and 'begin' in str(e['payload'])) for e in seen) or time.time() > deadline:
                break
    return seen


def test_closing_or_reloading_the_page_does_not_stop_a_run_and_reconnecting_shows_all_of_it(live):
    app, base = live
    store = app.state.store
    versions = [store.create_version(store.create_project(f'p{i}')['id']) for i in range(2)]
    with httpx.Client(timeout=15, base_url=base) as client:
        ids = [client.post(f'/api/versions/{v["id"]}/reviews', json={'goal': {}, 'depth': 'quick'}, headers=HEADERS).json()['id'] for v in versions]
        first = read_stream(client, f'/api/reviews/{ids[0]}/stream?after=0', 'text')           # the tab saw the start ...
        assert any('begin' in str(e['payload']) for e in first)                               # ... and closed (the block above ended the connection)
        time.sleep(0.3)
        info = client.get(f'/api/reviews/{ids[0]}', headers=HEADERS).json()
        assert info['chat_status'] == 'running' and info['live']['state'] == 'running'        # the run is still going
        runs = client.get('/api/runs', headers=HEADERS).json()
        assert {r['review_id'] for r in runs['runs']} == set(ids) and runs['limit'] == 3 and all(r['state'] == 'running' for r in runs['runs'])
        projects = {p['id']: p for p in client.get('/api/projects', headers=HEADERS).json()}
        assert [projects[v['project_id']]['live_reviews'] for v in versions] == [1, 1]
        # a tab opened later, or the same tab reloaded, replays everything from the stored events and follows to the end
        again = read_stream(client, f'/api/reviews/{ids[0]}/stream?after=0', 'completed')
        types = [e['type'] for e in again]
        assert types[0] == 'session' and 'completed' in types and 'later' in str([e['payload'] for e in again])
        sequences = [e['sequence'] for e in again]
        assert sequences == sorted(sequences) and sequences[:len(first)] == [e['sequence'] for e in first]
        # a second tab on the same review sees the same events
        third = read_stream(client, f'/api/reviews/{ids[0]}/stream?after=0', 'completed')
        assert [e['sequence'] for e in third] == sequences
        assert [e['type'] for e in store.events(store.get_review(ids[1])['chat_id'])][-1] in ('completed', 'text_delta', 'session')
        for _ in range(100):
            if not client.get('/api/runs', headers=HEADERS).json()['runs']:
                break
            time.sleep(0.1)
        assert client.get('/api/runs', headers=HEADERS).json()['runs'] == []
