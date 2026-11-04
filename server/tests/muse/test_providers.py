"""Which key is used and where it goes: MODEL_API_KEY first, then a Meta key, then a Baseten key (environment or Settings)."""
import httpx
import pytest
from fake_muse import end

from trustpcb import settings_store
from trustpcb.runners import muse
from trustpcb.runners.muse import MuseError, MuseRunner, resolve_base_url_for, resolve_target


def test_model_api_key_wins(monkeypatch):
    monkeypatch.setenv('BASETEN_API_KEY', 'b-key')
    target = resolve_target()
    assert target['name'] == 'MODEL_API_KEY' and target['base_url'] is None


def test_meta_before_baseten(monkeypatch):
    monkeypatch.delenv('MODEL_API_KEY')
    monkeypatch.setenv('META_API_KEY', 'm-key')
    monkeypatch.setenv('BASETEN_API_KEY', 'b-key')
    target = resolve_target()
    assert target['name'] == 'META_API_KEY' and target['base_url'] == 'https://api.meta.ai' and target['model'] == 'muse-spark-1.3'


def test_baseten_from_settings(monkeypatch):
    monkeypatch.delenv('MODEL_API_KEY')
    settings_store.set_secret('BASETEN_API_KEY', 'stored-key')
    target = resolve_target()
    assert target == {'name': 'BASETEN_API_KEY', 'key': 'stored-key', 'base_url': 'https://inference.baseten.co',
                      'model': 'meta-models/muse-spark-open-1026'}
    assert muse.configured()


def test_nothing_set(monkeypatch):
    monkeypatch.delenv('MODEL_API_KEY')
    assert resolve_target() is None and not muse.configured()


def test_named_key_stays_on_its_host(monkeypatch):
    monkeypatch.setenv('TRUSTPCB_MUSE_BASE_URL', 'https://api.meta.ai')
    with pytest.raises(MuseError, match='belongs to inference.baseten.co'):
        resolve_base_url_for('https://inference.baseten.co')
    monkeypatch.delenv('TRUSTPCB_MUSE_BASE_URL')
    assert resolve_base_url_for('https://inference.baseten.co') == 'https://inference.baseten.co'


async def test_run_sends_baseten_key_to_baseten(monkeypatch, api, make_runner, ask, sink):
    monkeypatch.delenv('MODEL_API_KEY')
    monkeypatch.setenv('BASETEN_API_KEY', 'b-key')
    seen = []
    inner = api.transport

    async def spy(request):
        seen.append(request.url.host)
        return await inner.handle_async_request(request)

    api.reply(end())
    runner = MuseRunner(data_dir=make_runner().data_dir, transport=httpx.MockTransport(spy))
    await runner.run(ask(profile={'model': 'muse-spark-1.3'}), sink.emit, sink.interact)
    assert seen == ['inference.baseten.co'] and api.headers[0]['authorization'] == 'Bearer b-key'
    assert api.requests[0]['model'] == 'meta-models/muse-spark-open-1026'


async def test_run_without_key_says_where_to_add_one(monkeypatch, runner, ask, sink):
    monkeypatch.delenv('MODEL_API_KEY')
    with pytest.raises(MuseError, match='Settings'):
        await runner.run(ask(), sink.emit, sink.interact)
