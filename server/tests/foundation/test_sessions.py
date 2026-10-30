import asyncio

import pytest

from trustpcb.runners.base import RunRequest
from trustpcb.runners.scripted import ScriptedRunner
from trustpcb.sessions import SessionConflict, SessionManager


@pytest.fixture
def manager(store):
    return SessionManager(store, runner_factory=lambda profile: ScriptedRunner(profile.get('script')))


async def finished(store, chat_id, timeout=5):
    for _ in range(int(timeout / 0.01)):
        done = [e for e in store.events(chat_id) if e['type'] in ('completed', 'failed', 'canceled')]
        if done:
            return done
        await asyncio.sleep(0.01)
    raise AssertionError('run did not finish')


async def waiting_request(manager):
    for _ in range(300):
        if manager.requests:
            return next(iter(manager.requests))
        await asyncio.sleep(0.01)
    raise AssertionError('no interaction was requested')


def types(store, chat_id):
    return [e['type'] for e in store.events(chat_id)]


async def test_run_completes_with_exactly_one_terminal_event(store, manager, seeded):
    _, _, review = seeded
    chat = review['chat_id']

    async def work(ctx):
        await ctx.emit('phase_changed', {'phase': 'a'})
        await ctx.emit('completed', {'sneaky': True})  # runs may not write terminal events
        ctx.final_payload = {'summary': 'ok'}

    started = await manager.start(chat, 'r1', work, text='go', user_event={'text': 'go'})
    done = await finished(store, chat)
    assert types(store, chat) == ['user_message', 'phase_changed', 'completed']
    assert done[0]['payload'] == {'summary': 'ok'} and done[0]['turn_id'] == started['turn_id']
    assert store.get_turn(started['turn_id'])['status'] == 'completed' and store.get_chat(chat)['status'] == 'completed'
    assert not manager.active


async def test_same_request_id_runs_once(store, manager, seeded):
    _, _, review = seeded
    calls = []

    async def work(ctx):
        calls.append(1)

    first = await manager.start(review['chat_id'], 'same', work)
    await finished(store, review['chat_id'])
    second = await manager.start(review['chat_id'], 'same', work)
    assert second == {'turn_id': first['turn_id'], 'duplicate': True} and first['duplicate'] is False
    await asyncio.sleep(0.05)
    assert calls == [1]


async def test_request_id_is_required(manager, seeded):
    _, _, review = seeded
    with pytest.raises(ValueError):
        await manager.start(review['chat_id'], '', lambda ctx: None)
    with pytest.raises(ValueError):
        await manager.start('missing-chat', 'r', lambda ctx: None)


async def test_one_run_per_project_across_chats(store, manager, seeded):
    _, _, review = seeded
    thread = store.create_thread(review['id'])
    release = asyncio.Event()

    async def work(ctx):
        await release.wait()

    await manager.start(review['chat_id'], 'a', work)
    with pytest.raises(SessionConflict):
        await manager.start(thread['chat_id'], 'b', work)
    release.set()
    await finished(store, review['chat_id'])
    await manager.start(thread['chat_id'], 'b', work)  # the lock was released before the terminal event
    await finished(store, thread['chat_id'])


async def test_failure_is_reported_and_releases_the_project(store, manager, seeded):
    _, _, review = seeded

    async def work(ctx):
        raise RuntimeError('solver exploded')

    await manager.start(review['chat_id'], 'r', work)
    done = await finished(store, review['chat_id'])
    assert done[0]['type'] == 'failed' and done[0]['payload']['message'] == 'solver exploded'
    assert store.get_chat(review['chat_id'])['status'] == 'failed' and not manager.active


async def test_cancel_stops_the_run_drops_late_events_and_closes_the_runner(store, seeded):
    _, _, review = seeded
    chat = review['chat_id']
    runners = []

    def factory(profile):
        runners.append(ScriptedRunner([{'text': 'working'}, {'sleep': 30}, {'text': 'never shown'}]))
        return runners[-1]

    manager = SessionManager(store, runner_factory=factory)

    async def work(ctx):
        await ctx.run_agent(RunRequest(None, 'sys', 'hi', None, 10, {}))

    await manager.start(chat, 'r', work)
    for _ in range(300):
        if any(e['type'] == 'text_delta' for e in store.events(chat)):
            break
        await asyncio.sleep(0.01)
    assert await manager.cancel(chat) == {'status': 'canceled'}
    done = await finished(store, chat)
    assert [e['type'] for e in done] == ['canceled']
    assert 'never shown' not in str(store.events(chat))
    assert store.get_chat(chat)['status'] == 'canceled' and not manager.active
    assert await manager.cancel(chat) == {'status': 'idle'}


async def test_cancel_while_waiting_for_a_user_decision(store, manager, seeded):
    _, _, review = seeded

    async def work(ctx):
        await ctx.interact('test', {'title': 'Run simulation?'})

    await manager.start(review['chat_id'], 'r', work)
    await waiting_request(manager)
    assert store.get_chat(review['chat_id'])['status'] == 'waiting_test'
    await manager.cancel(review['chat_id'])
    assert (await finished(store, review['chat_id']))[0]['type'] == 'canceled'
    assert not manager.requests


async def test_interaction_roundtrip_and_expiry(store, manager, seeded):
    _, _, review = seeded
    answers = []

    async def work(ctx):
        answers.append(await ctx.interact('test', {'title': 'Run?', 'estimate_s': 120}))

    await manager.start(review['chat_id'], 'r', work)
    request_id = await waiting_request(manager)
    with pytest.raises(ValueError):
        await manager.respond(request_id)  # needs allow
    assert await manager.respond(request_id, allow=True) == {'status': 'received'}
    await finished(store, review['chat_id'])
    assert answers == [{'allow': True}]
    resolved = [e for e in store.events(review['chat_id']) if e['type'] == 'request_resolved']
    assert resolved[0]['payload'] == {'request_id': request_id, 'kind': 'test', 'allow': True}
    asked = [e for e in store.events(review['chat_id']) if e['type'] == 'test'][0]
    assert asked['payload']['request_id'] == request_id and asked['payload']['estimate_s'] == 120
    with pytest.raises(SessionConflict):
        await manager.respond(request_id, allow=False)


async def test_question_needs_text_answers(store, manager, seeded):
    _, _, review = seeded

    async def work(ctx):
        await ctx.interact('question', {'questions': ['Which?']})

    await manager.start(review['chat_id'], 'r', work)
    request_id = await waiting_request(manager)
    with pytest.raises(ValueError):
        await manager.respond(request_id, answers={'q': 1})
    await manager.respond(request_id, answers={'q': 'a'})
    await finished(store, review['chat_id'])


async def test_unknown_interaction_kind_is_refused(store, manager, seeded):
    _, _, review = seeded

    async def work(ctx):
        await ctx.interact('shell', {})

    await manager.start(review['chat_id'], 'r', work)
    assert 'Unsupported' in (await finished(store, review['chat_id']))[0]['payload']['message']


async def test_send_runs_a_scripted_agent_turn_and_saves_the_session(store, seeded):
    _, _, review = seeded
    thread = store.create_thread(review['id'])
    script = [{'text': 'hello there'}, {'usage': {'input_tokens': 3, 'output_tokens': 2}}]
    manager = SessionManager(store, runner_factory=lambda profile: ScriptedRunner(profile['script']))

    async def build(ctx):
        return RunRequest(None, 'sys', 'why?', None, 6, {'script': script})

    await manager.send(thread['chat_id'], 'why?', 'm1', build)
    await finished(store, thread['chat_id'])
    kinds = types(store, thread['chat_id'])
    assert kinds[0] == 'user_message' and kinds[-1] == 'completed' and 'usage' in kinds
    assert store.get_chat(thread['chat_id'])['session_id'].startswith('scripted-')
    assert ''.join(e['payload']['text'] for e in store.events(thread['chat_id']) if e['type'] == 'text_delta') == 'hello there'
