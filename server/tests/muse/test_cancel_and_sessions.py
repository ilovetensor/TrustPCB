"""Cancel, saved conversations and resume."""
import asyncio
import json

import pytest

from fake_muse import Hang, Msg, PIXEL, call, end, results_in, text, tools


async def settle(task, timeout=2):
    return await asyncio.wait_for(task, timeout)


async def test_cancel_stops_a_request_in_flight(api, runner, sink, ask, mode):
    api.reply(Hang)
    task = asyncio.ensure_future(runner.run(ask(), sink.emit, sink.interact))
    for _ in range(50):
        if api.requests:
            break
        await asyncio.sleep(0.01)
    assert api.requests and not task.done()
    await runner.cancel()
    await settle(task)
    assert sink.of('usage') == [] and sink.of('status') == []


async def test_cancel_stops_a_running_tool_and_keeps_the_history_valid(api, runner, sink, ask, world, tmp_path, mode):
    api.reply(tools(call('w', 'slow'), call('after', 'look', what='x')))
    task = asyncio.ensure_future(runner.run(ask(), sink.emit, sink.interact))
    for _ in range(50):
        if ('slow', {}) in world.calls:
            break
        await asyncio.sleep(0.01)
    await runner.cancel()
    await settle(task)
    assert [c[0] for c in world.calls] == ['slow']                       # the second tool never ran
    saved = json.loads(next((tmp_path / 'data' / 'muse_sessions').glob('*.json')).read_text())
    last = saved['messages'][-1]
    assert last['role'] == 'user' and [b['tool_use_id'] for b in last['content']] == ['w', 'after'] and all(b['is_error'] for b in last['content'])


async def test_cancel_between_tools_skips_the_rest(api, runner, sink, ask, world, mode):
    async def cancel_here():
        await runner.cancel()
    world.slow = cancel_here
    api.reply(tools(call('w', 'slow'), call('after', 'look', what='x')), end())
    await settle(asyncio.ensure_future(runner.run(ask(), sink.emit, sink.interact)))
    assert [c[0] for c in world.calls] == ['slow'] and len(api.requests) == 1


async def test_cancel_during_a_retry_wait(api, make_runner, sink, ask, mode):
    started = asyncio.Event()

    async def long_sleep(seconds):
        started.set()
        await asyncio.sleep(30)
    runner = make_runner(sleep=long_sleep)
    from fake_muse import Err
    api.reply(Err(503, 'busy'), end())
    task = asyncio.ensure_future(runner.run(ask(), sink.emit, sink.interact))
    await asyncio.wait_for(started.wait(), 2)
    await runner.close()
    await settle(task)
    assert len(api.requests) == 1


async def test_a_canceled_runner_can_run_again(api, runner, sink, ask, mode):
    await runner.cancel()
    api.reply(end('ok'))
    await runner.run(ask(), sink.emit, sink.interact)
    assert len(api.requests) == 1


async def test_second_concurrent_run_is_refused(api, runner, sink, ask, mode):
    api.reply(Hang)
    first = asyncio.ensure_future(runner.run(ask(), sink.emit, sink.interact))
    while not api.requests:
        await asyncio.sleep(0.01)
    with pytest.raises(RuntimeError, match='active run'):
        await runner.run(ask(), sink.emit, sink.interact)
    await runner.cancel()
    await settle(first)


async def test_conversation_is_saved_and_resumed(api, runner, sink, ask, world, tmp_path, mode):
    api.reply(tools(call('t1', 'look', what='U3')), end('First answer.'))
    await runner.run(ask('First question.'), sink.emit, sink.interact)
    session = sink.of('session')[0]['session_id']
    path = tmp_path / 'data' / 'muse_sessions' / f'{session}.json'
    assert path.exists() and (path.stat().st_mode & 0o777) == 0o600
    saved = json.loads(path.read_text())
    assert [m['role'] for m in saved['messages']] == ['user', 'assistant', 'user', 'assistant']
    api.reply(end('Second answer.'))
    sink.events.clear()
    await runner.run(ask('Second question.', session_id=session), sink.emit, sink.interact)
    assert sink.of('session') == [{'session_id': session}]
    body = api.requests[-1]
    assert [m['role'] for m in body['messages']] == ['user', 'assistant', 'user', 'assistant', 'user']
    assert body['messages'][0]['content'][0]['text'] == 'First question.' and body['messages'][-1]['content'][0]['text'] == 'Second question.'


async def test_session_ids_are_local_uuids(api, runner, sink, ask, mode):
    import uuid
    api.reply(end(), end())
    await runner.run(ask(), sink.emit, sink.interact)
    await runner.run(ask(), sink.emit, sink.interact)
    ids = [p['session_id'] for p in sink.of('session')]
    assert len(set(ids)) == 2 and all(uuid.UUID(i) for i in ids)


async def test_image_bytes_survive_only_in_the_latest_turn(api, runner, sink, ask, tmp_path, mode):
    api.reply(tools(call('s1', 'snap')), tools(call('s2', 'snap')), end())
    await runner.run(ask(), sink.emit, sink.interact)
    path = next((tmp_path / 'data' / 'muse_sessions').glob('*.json'))
    raw = path.read_text()
    assert raw.count(PIXEL) == 1
    saved = json.loads(raw)
    first_result, second_result = saved['messages'][2]['content'][0], saved['messages'][4]['content'][0]
    assert first_result['content'][1] == {'type': 'text', 'text': '[image removed from saved history]'}
    assert second_result['content'][1]['type'] == 'image'


async def test_resume_after_the_turn_limit_merges_the_follow_up(api, runner, sink, ask, world, mode):
    api.responder = lambda body: tools(call(f'c{len(body["messages"])}', 'look', what='again')) if len(body['messages']) < 4 else end('Wrapped up.')
    await runner.run(ask(max_turns=1), sink.emit, sink.interact)
    session = sink.of('session')[0]['session_id']
    assert sink.of('status')[-1]['state'] == 'max_turns'
    await runner.run(ask('Please finish.', session_id=session, max_turns=5), sink.emit, sink.interact)
    messages = api.requests[-1]['messages']
    assert [m['role'] for m in messages][:3] == ['user', 'assistant', 'user']
    assert [b['type'] for b in messages[2]['content']] == ['tool_result', 'text'] and messages[2]['content'][1]['text'] == 'Please finish.'


async def test_unknown_or_unsafe_session_ids_are_errors(api, runner, sink, ask):
    with pytest.raises(RuntimeError, match='cannot be resumed'):
        await runner.run(ask(session_id='11111111-2222-3333-4444-555555555555'), sink.emit, sink.interact)
    with pytest.raises(RuntimeError, match='not valid'):
        await runner.run(ask(session_id='../../etc/passwd'), sink.emit, sink.interact)
    assert api.requests == []
