"""Base URL and model overrides, the host allowlist, and thinking blocks as a hosted endpoint returns them."""
import httpx
import pytest

from fake_muse import KEY, Msg, call, end, text, tools
from trustpcb.runners.muse import MuseError, MuseRunner, resolve_base_url

BASETEN = 'https://inference.baseten.co'
OPEN_MODEL = 'meta-models/muse-spark-open-1026'


def test_default_base_url_is_unchanged(monkeypatch):
    monkeypatch.delenv('TRUSTPCB_MUSE_BASE_URL', raising=False)
    assert resolve_base_url() == 'https://api.meta.ai'


@pytest.mark.parametrize('url', ['http://inference.baseten.co', 'https://evil.example', 'https://inference.baseten.co.evil.example',
                                 'https://user:pw@inference.baseten.co', 'https://inference.baseten.co:8443', 'inference.baseten.co', 'ftp://api.meta.ai'])
def test_other_hosts_never_get_the_key(monkeypatch, url):
    monkeypatch.setenv('TRUSTPCB_MUSE_BASE_URL', url)
    with pytest.raises(MuseError, match='Refusing to send the Muse key') as caught:
        resolve_base_url()
    assert KEY not in str(caught.value)


async def test_env_overrides_reach_the_request(api, make_runner, sink, ask, monkeypatch):
    monkeypatch.setenv('TRUSTPCB_MUSE_BASE_URL', BASETEN + '/')
    monkeypatch.setenv('TRUSTPCB_MUSE_MODEL', OPEN_MODEL)
    seen = []
    inner = api.transport

    async def spy(request):
        seen.append(str(request.url))
        return await inner.handle_async_request(request)

    api.reply(end())
    runner = MuseRunner(data_dir=make_runner().data_dir, transport=httpx.MockTransport(spy))
    await runner.run(ask(profile={'model': 'muse-spark-1.3'}), sink.emit, sink.interact)
    assert seen == [BASETEN + '/v1/messages'] and api.requests[0]['model'] == OPEN_MODEL


async def test_a_refused_host_fails_before_any_request(api, runner, sink, ask, monkeypatch):
    monkeypatch.setenv('TRUSTPCB_MUSE_BASE_URL', 'https://evil.example')
    with pytest.raises(MuseError, match='Refusing'):
        await runner.run(ask(), sink.emit, sink.interact)
    assert api.requests == []


async def test_a_redirect_is_not_followed(sink, ask, make_runner, monkeypatch):
    hits = []

    async def handler(request):
        hits.append(request.url.host)
        return httpx.Response(307, headers={'location': 'https://evil.example/v1/messages'})

    runner = MuseRunner(data_dir=make_runner().data_dir, transport=httpx.MockTransport(handler))
    runner.max_retries = 0
    with pytest.raises(MuseError, match='307'):
        await runner.run(ask(), sink.emit, sink.interact)
    assert hits == ['api.meta.ai']


async def test_thinking_without_signature_and_unknown_fields_round_trip(api, runner, sink, ask, world, mode):
    bare = {'type': 'thinking', 'thinking': 'Check U1 first.'}
    odd = {'type': 'thinking', 'thinking': 'Then C3.', 'signature': 'sig-1', 'index': 0}
    api.reply(Msg([bare, odd, text('Looking.'), call('t1', 'look', what='U1')], 'tool_use'), end('All good.'))
    await runner.run(ask(), sink.emit, sink.interact)
    replayed = api.requests[1]['messages'][1]['content']
    assert replayed[0] == bare and 'signature' not in replayed[0] and replayed[1]['signature'] == 'sig-1'
    assert [p['text'] for p in sink.of('thought')] == ['Check U1 first.', 'Then C3.']
    shown = ''.join(p['text'] for p in sink.of('text_delta'))
    assert 'Check U1' not in shown and 'Then C3' not in shown and shown == 'Looking.All good.'


async def test_empty_thinking_block_is_kept_but_not_shown(api, runner, sink, ask, mode):
    api.reply(Msg([{'type': 'thinking', 'thinking': '', 'signature': 'sig-e'}, text('Fine.')], 'end_turn'))
    await runner.run(ask(), sink.emit, sink.interact)
    assert sink.of('thought') == [] and ''.join(p['text'] for p in sink.of('text_delta')) == 'Fine.'
