"""Retries, error messages, secrets, the model and key checks."""
import asyncio
import logging

import httpx
import pytest

from fake_muse import KEY, Boom, Err, call, end, tools


async def test_429_is_retried_after_the_wait_the_server_asks_for(api, runner, sink, ask, delays, mode):
    api.reply(Err(429, 'slow down', 'rate_limit_error', {'retry-after': '7'}), Err(503, 'overloaded', 'overloaded_error'), end())
    await runner.run(ask(), sink.emit, sink.interact)
    assert len(api.requests) == 3 and len(delays) == 2
    assert delays[0] >= 7 and 0.25 <= delays[1] <= 1.0
    assert [p['attempt'] for p in sink.of('status') if p['state'] == 'retrying'] == [1, 2]


async def test_timeouts_and_dropped_connections_are_retried(api, runner, sink, ask, delays, mode):
    api.reply(Boom(httpx.ReadTimeout('slow')), Boom(httpx.ConnectError('down')), end())
    await runner.run(ask(), sink.emit, sink.interact)
    assert len(api.requests) == 3 and len(delays) == 2


async def test_backoff_grows_with_jitter_and_stops_after_five_retries(api, runner, sink, ask, delays, mode):
    api.reply(*[Err(500, 'server error') for _ in range(6)])
    with pytest.raises(RuntimeError, match='500'):
        await runner.run(ask(), sink.emit, sink.interact)
    assert len(api.requests) == 6 and len(delays) == 5
    caps = [0.5, 1, 2, 4, 8]
    assert all(cap * 0.5 <= d <= cap for d, cap in zip(delays, caps))


@pytest.mark.parametrize('status', [400, 401, 402, 403, 404, 413])
async def test_other_client_errors_are_not_retried(api, runner, sink, ask, delays, status, mode):
    api.reply(Err(status, 'no'), end())
    with pytest.raises(RuntimeError, match=str(status)):
        await runner.run(ask(), sink.emit, sink.interact)
    assert len(api.requests) == 1 and delays == []


async def test_a_stream_error_event_before_content_is_retried(api, ask, sink, tmp_path, delays, monkeypatch):
    monkeypatch.setenv('TRUSTPCB_MUSE_STREAM', '1')
    import json
    from trustpcb.runners.muse import MuseRunner
    state = {'n': 0}

    def handler(request):
        state['n'] += 1
        if state['n'] == 1:
            return httpx.Response(200, headers={'content-type': 'text/event-stream'},
                                  content=b'event: error\ndata: {"type":"error","error":{"type":"overloaded_error","message":"busy"}}\n\n')
        from fake_muse import render_sse
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=render_sse(end()).encode())

    async def sleep(seconds):
        delays.append(seconds)

    runner = MuseRunner(data_dir=tmp_path / 'd', transport=httpx.MockTransport(handler), sleep=sleep)
    await runner.run(ask(), sink.emit, sink.interact)
    assert state['n'] == 2 and len(delays) == 1


async def test_missing_key_is_a_clear_error_before_any_request(api, runner, sink, ask, monkeypatch):
    monkeypatch.delenv('MODEL_API_KEY')
    with pytest.raises(RuntimeError, match='MODEL_API_KEY is not set'):
        await runner.run(ask(), sink.emit, sink.interact)
    assert api.requests == [] and sink.events == []
    monkeypatch.setenv('MODEL_API_KEY', '   ')
    with pytest.raises(RuntimeError, match='MODEL_API_KEY is not set'):
        await runner.run(ask(), sink.emit, sink.interact)


@pytest.mark.parametrize('model', ['muse-spark-1.3-contributor', 'muse-spark-1.2-Contributor'])
async def test_contributor_models_are_refused(api, make_runner, sink, ask, model):
    with pytest.raises(RuntimeError, match='contributor'):
        make_runner(model=model)
    runner = make_runner()
    with pytest.raises(RuntimeError, match='contributor'):
        await runner.run(ask(profile={'model': model}), sink.emit, sink.interact)
    assert api.requests == []


async def test_profile_model_and_standard_default(api, runner, sink, ask):
    api.reply(end(), end())
    await runner.run(ask(profile={'model': 'muse-spark-1.1'}), sink.emit, sink.interact)
    await runner.run(ask(), sink.emit, sink.interact)
    assert [r['model'] for r in api.requests] == ['muse-spark-1.1', 'muse-spark-1.3']


async def test_only_the_muse_key_is_sent(api, runner, sink, ask, monkeypatch):
    monkeypatch.setenv('SOME_OTHER_API_KEY', 'other-secret-value')
    monkeypatch.setenv('AUTH_TOKEN', 'other-token-value')
    api.reply(end())
    await runner.run(ask(), sink.emit, sink.interact)
    headers = api.headers[0]
    assert headers['authorization'] == f'Bearer {KEY}'
    assert 'x-api-key' not in headers and 'other-secret-value' not in str(headers) and 'other-token-value' not in str(headers)
    assert set(headers) <= {'authorization', 'content-type', 'user-agent', 'accept', 'accept-encoding', 'connection', 'host', 'content-length'}


async def test_the_key_never_reaches_logs_errors_events_or_disk(api, runner, sink, ask, caplog, tmp_path, mode):
    caplog.set_level(logging.DEBUG)
    api.reply(tools(call('t', 'boom')), Err(401, f'Invalid key. Authorization: Bearer {KEY} and MODEL_API_KEY={KEY}', 'authentication_error'))
    with pytest.raises(RuntimeError) as caught:
        await runner.run(ask(), sink.emit, sink.interact)
    assert 'check MODEL_API_KEY' in str(caught.value) and KEY not in str(caught.value) and '[REDACTED]' in str(caught.value)
    assert KEY not in caplog.text
    assert KEY not in repr(sink.events)
    for path in (tmp_path / 'data').rglob('*'):
        if path.is_file():
            assert KEY not in path.read_text()
    assert KEY not in repr(caught.value.__cause__) and caught.value.__suppress_context__


async def test_key_with_bad_characters_is_refused_without_echoing_it(api, runner, sink, ask, monkeypatch):
    monkeypatch.setenv('MODEL_API_KEY', 'abc defé')
    with pytest.raises(RuntimeError) as caught:
        await runner.run(ask(), sink.emit, sink.interact)
    assert 'abc' not in str(caught.value)
