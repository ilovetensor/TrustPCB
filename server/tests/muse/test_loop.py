"""The tool loop: requests, events, phases, thinking replay, usage, turn limit."""
import json

from fake_muse import KEY, Msg, call, end, results_in, text, tools

FORBIDDEN = {'stop_sequences', 'top_k', 'container', 'inference_geo', 'temperature', 'top_p'}


async def test_tool_loop_requests_and_events(api, runner, sink, ask, world, mode):
    api.reply(tools(call('t1', 'look', what='U3'), lead='Checking the radio.', usage={'input_tokens': 120, 'output_tokens': 30}),
              tools(call('t2', 'look', what='C13'), usage={'input_tokens': 200, 'output_tokens': 10, 'cache_read_input_tokens': 90,
                                                              'output_tokens_details': {'thinking_tokens': 4}}),
              end('All checked.', usage={'input_tokens': 260, 'output_tokens': 12}))
    await runner.run(ask(), sink.emit, sink.interact)
    assert len(api.requests) == 3
    first = api.requests[0]
    assert first['model'] == 'muse-spark-1.3' and first['max_tokens'] == 8000
    assert first['thinking'] == {'type': 'adaptive'} and first['output_config'] == {'effort': 'medium'}
    assert first['tool_choice'] == {'type': 'auto'} and not (FORBIDDEN & set(first))
    assert first['system'] == [{'type': 'text', 'text': 'You review boards.'}]
    assert first['messages'] == [{'role': 'user', 'content': [{'type': 'text', 'text': 'Review the board.'}]}]
    assert first.get('stream', False) is (mode == 'stream')
    assert {t['name'] for t in first['tools']} == {'look', 'advance', 'snap', 'boom', 'slow'}      # phase 1 only
    assert all(t['type'] == 'custom' and 'input_schema' in t for t in first['tools'])
    assert api.headers[0]['authorization'] == f'Bearer {KEY}'
    # The results go back to the model in the next request, after the assistant message that asked for them.
    second = api.requests[1]
    assert second['messages'][1]['role'] == 'assistant' and second['messages'][1]['content'][-1]['id'] == 't1'
    assert results_in(second)[0]['tool_use_id'] == 't1' and 'U3' in results_in(second)[0]['content']
    assert world.calls == [('look', {'what': 'U3'}), ('look', {'what': 'C13'})]
    kinds = sink.kinds()
    assert kinds[0] == 'session' and sink.of('session')[0]['session_id']
    assert [p['tool_call_id'] for p in sink.of('tool_start')] == ['t1', 't2']
    assert [p['name'] for p in sink.of('tool_result')] == ['look', 'look'] and not any(p['is_error'] for p in sink.of('tool_result'))
    assert ''.join(p['text'] for p in sink.of('text_delta')) == 'Checking the radio.All checked.'
    # One usage event per response, so a token wall sees every call.
    usages = sink.of('usage')
    assert [u['input_tokens'] for u in usages] == [120, 200, 260] and [u['output_tokens'] for u in usages] == [30, 10, 12]
    assert usages[1]['cache_read_input_tokens'] == 90 and usages[1]['thinking_tokens'] == 4
    assert kinds.index('tool_start') < kinds.index('tool_result') < kinds.index('usage', kinds.index('tool_result'))


async def test_effort_comes_from_the_environment(api, runner, sink, ask, monkeypatch):
    monkeypatch.setenv('TRUSTPCB_MUSE_EFFORT', 'high')
    api.reply(end())
    await runner.run(ask(), sink.emit, sink.interact)
    assert api.requests[0]['output_config'] == {'effort': 'high'}


async def test_bad_effort_is_refused_before_any_request(api, runner, sink, ask, monkeypatch):
    monkeypatch.setenv('TRUSTPCB_MUSE_EFFORT', 'maximum')
    try:
        await runner.run(ask(), sink.emit, sink.interact)
    except RuntimeError as exc:
        assert 'TRUSTPCB_MUSE_EFFORT' in str(exc)
    else:
        raise AssertionError('expected an error')
    assert api.requests == []


async def test_tool_list_is_read_again_every_turn(api, runner, sink, ask, world, mode):
    api.reply(tools(call('a', 'advance')), tools(call('b', 'wrap_up')), end())
    await runner.run(ask(), sink.emit, sink.interact)
    names = [sorted(t['name'] for t in body['tools']) for body in api.requests]
    assert 'wrap_up' not in names[0] and 'wrap_up' in names[1] and 'wrap_up' in names[2]
    assert world.calls[-1][0] == 'wrap_up'


async def test_out_of_phase_call_returns_the_refusal_to_the_model(api, runner, sink, ask, world, mode):
    api.reply(tools(call('x', 'wrap_up')), end())
    await runner.run(ask(), sink.emit, sink.interact)
    block = results_in(api.requests[1])[0]
    assert block['is_error'] is True and 'not_allowed_in_phase' in block['content']
    assert world.calls == []                                           # the handler never ran
    assert sink.of('tool_result')[0]['is_error'] is True


async def test_unknown_tool_failing_tool_and_bad_arguments_are_results_not_crashes(api, runner, sink, ask, world, mode):
    api.reply(tools(call('u', 'no_such_tool'), call('b', 'boom'), call('v', 'look', what=5)), end())
    await runner.run(ask(), sink.emit, sink.interact)
    blocks = results_in(api.requests[1])
    assert [b['tool_use_id'] for b in blocks] == ['u', 'b', 'v'] and all(b['is_error'] for b in blocks)
    assert 'unknown_tool' in blocks[0]['content'] and 'tool_failed' in blocks[1]['content'] and 'invalid_arguments' in blocks[2]['content']
    assert KEY not in json.dumps(api.requests)


async def test_unparsable_streamed_arguments_become_an_error_result(api, runner, sink, ask, world, monkeypatch):
    monkeypatch.setenv('TRUSTPCB_MUSE_STREAM', '1')
    api.reply(tools({'type': 'tool_use', 'id': 'j', 'name': 'look', 'input': '{"what": "U3'}), end())
    await runner.run(ask(), sink.emit, sink.interact)
    block = results_in(api.requests[1])[0]
    assert block['is_error'] and 'not valid JSON' in block['content'] and world.calls == []


async def test_thinking_blocks_are_replayed_exactly(api, runner, sink, ask, world, mode):
    thinking = {'type': 'thinking', 'thinking': 'The antenna needs a closer look.', 'signature': 'sig-abc=='}
    redacted = {'type': 'redacted_thinking', 'data': 'ENCRYPTED-BLOB'}
    api.reply(Msg([thinking, redacted, text('Looking.'), call('t1', 'look', what='ANT')], 'tool_use'),
              Msg([{'type': 'thinking', 'thinking': 'Second thoughts.', 'signature': 'sig-2'}, call('t2', 'look', what='C1')], 'tool_use'),
              end())
    await runner.run(ask(), sink.emit, sink.interact)
    replayed = api.requests[1]['messages'][1]['content']
    assert replayed == [thinking, redacted, text('Looking.'), call('t1', 'look', what='ANT')]
    third = api.requests[2]['messages']
    assert third[1]['content'] == replayed and third[3]['content'][0] == {'type': 'thinking', 'thinking': 'Second thoughts.', 'signature': 'sig-2'}
    assert [p['text'] for p in sink.of('thought')] == ['The antenna needs a closer look.', 'Second thoughts.']


async def test_turn_limit_ends_cleanly(api, runner, sink, ask, world, mode):
    api.responder = lambda body: tools(call(f'c{len(body["messages"])}', 'look', what='again'))
    await runner.run(ask(max_turns=2), sink.emit, sink.interact)
    assert len(api.requests) == 2 and len(world.calls) == 2
    status = sink.of('status')[-1]
    assert status['state'] == 'max_turns' and status['max_turns'] == 2
    assert len(sink.of('usage')) == 2


async def test_output_limit_nudges_then_continues(api, runner, sink, ask, mode):
    api.reply(Msg([text('Partial thought')], 'max_tokens'), end('Finished.'))
    await runner.run(ask(), sink.emit, sink.interact)
    nudge = api.requests[1]['messages'][-1]
    assert nudge['role'] == 'user' and 'output limit' in nudge['content'][0]['text']


async def test_refusal_stop_is_an_error(api, runner, sink, ask, mode):
    api.reply(Msg([text('No.')], 'refusal'))
    try:
        await runner.run(ask(), sink.emit, sink.interact)
    except RuntimeError as exc:
        assert 'refusal' in str(exc)
    else:
        raise AssertionError('expected an error')
