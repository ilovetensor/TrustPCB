"""Images in tool results, and the permanent fallback to the user turn."""
import json

import pytest

from fake_muse import PIXEL, Err, call, end, results_in, text, tools


def image_blocks(content):
    return [b for b in content if isinstance(b, dict) and b.get('type') == 'image']


async def test_image_goes_inside_the_tool_result_first(api, runner, sink, ask, mode):
    api.reply(tools(call('s1', 'snap', what='U3')), end())
    await runner.run(ask(), sink.emit, sink.interact)
    block = results_in(api.requests[1])[0]
    assert block['content'][0]['type'] == 'text'
    assert block['content'][1] == {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': PIXEL}}
    payload = sink.of('tool_result')[0]
    assert payload['images'] == [{'mime': 'image/png', 'bytes_b64': len(PIXEL)}] and PIXEL not in json.dumps(sink.events)


async def test_a_400_naming_the_image_falls_back_for_the_rest_of_the_session(api, runner, sink, ask, mode, tmp_path):
    api.reply(tools(call('s1', 'snap', what='U3')),
              Err(400, 'Images are not supported inside tool_result content.', 'invalid_request_error'),
              tools(call('s2', 'snap', what='C13')),
              end())
    await runner.run(ask(), sink.emit, sink.interact)
    assert len(api.requests) == 4
    refused, retried, later = api.requests[1], api.requests[2], api.requests[3]
    assert image_blocks(results_in(refused)[0]['content'])               # first try: inside the tool result
    content = retried['messages'][-1]['content']
    assert results_in(retried)[0]['tool_use_id'] == 's1' and isinstance(results_in(retried)[0]['content'], str)
    assert content[0]['type'] == 'tool_result' and image_blocks(content[1:]) and len(image_blocks(content)) == 1
    naming = next(b['text'] for b in content if b.get('type') == 'text' and 'snap' in b.get('text', ''))
    assert 's1' in naming
    # The second image never goes inside a tool result again, and does not cost another refused request.
    last = later['messages'][-1]['content']
    assert not any(image_blocks(b['content']) for b in last if b['type'] == 'tool_result' and isinstance(b['content'], list))
    assert len(image_blocks(last)) == 1 and 's2' in json.dumps(last)
    assert [p['state'] for p in sink.of('status')] == ['image_fallback']
    saved = json.loads(next((tmp_path / 'data' / 'muse_sessions').glob('*.json')).read_text())
    assert saved['image_mode'] == 'user_message'


async def test_a_resumed_session_keeps_the_fallback(api, runner, sink, ask, mode, tmp_path):
    api.reply(tools(call('s1', 'snap')), Err(400, 'image in tool_result is not accepted'), tools(call('s1b', 'snap')), end())
    await runner.run(ask(), sink.emit, sink.interact)
    session = sink.of('session')[0]['session_id']
    api.reply(tools(call('s2', 'snap')), end())
    sink.events.clear()
    await runner.run(ask('Follow up.', session_id=session), sink.emit, sink.interact)
    after = api.requests[-1]['messages'][-1]['content']
    assert not any(isinstance(b.get('content'), list) and image_blocks(b['content']) for b in after if b['type'] == 'tool_result')
    assert image_blocks(after)


async def test_other_400s_do_not_trigger_the_fallback(api, runner, sink, ask, mode):
    api.reply(tools(call('s1', 'snap')), Err(400, 'Unknown field: top_k', 'invalid_request_error'))
    with pytest.raises(RuntimeError, match='400'):
        await runner.run(ask(), sink.emit, sink.interact)
    assert len(api.requests) == 2


async def test_env_forces_user_message_images(api, runner, sink, ask, monkeypatch, mode):
    monkeypatch.setenv('TRUSTPCB_MUSE_IMAGE_MODE', 'user_message')
    api.reply(tools(call('s1', 'snap')), end())
    await runner.run(ask(), sink.emit, sink.interact)
    content = api.requests[1]['messages'][-1]['content']
    assert isinstance(content[0]['content'], str) and len(image_blocks(content)) == 1
    assert sink.of('status') == []


async def test_env_forced_tool_result_never_falls_back(api, runner, sink, ask, monkeypatch, mode):
    monkeypatch.setenv('TRUSTPCB_MUSE_IMAGE_MODE', 'tool_result')
    api.reply(tools(call('s1', 'snap')), Err(400, 'image in tool_result is not accepted'))
    with pytest.raises(RuntimeError, match='image'):
        await runner.run(ask(), sink.emit, sink.interact)
    assert len(api.requests) == 2


async def test_unknown_image_mode_is_an_error(api, runner, sink, ask, monkeypatch):
    monkeypatch.setenv('TRUSTPCB_MUSE_IMAGE_MODE', 'inline')
    with pytest.raises(RuntimeError, match='TRUSTPCB_MUSE_IMAGE_MODE'):
        await runner.run(ask(), sink.emit, sink.interact)
    assert api.requests == []


@pytest.mark.default_image_mode
def test_default_image_mode_is_a_user_message():
    from trustpcb.runners import muse as muse_module
    assert muse_module.DEFAULT_IMAGE_MODE == 'user_message'
