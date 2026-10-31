"""Parts tests never touch the network or a real key: every HTTP client is a fake, every key is a made-up string."""
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent))     # test helpers such as support

from support import FAKE_ID, FAKE_MOUSER, FAKE_SECRET, HOST, PUBLIC_IP, TOKEN  # noqa: E402
from trustpcb import settings_store
from trustpcb.api import create_app
from trustpcb.parts import datasheets, digikey, mouser
from trustpcb.parts import http as parts_http



@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for name in settings_store.KEYS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv('TRUSTPCB_DEMO_READONLY', raising=False)
    settings_store.configure(tmp_path / 'data')
    digikey.forget_tokens()
    mouser._sent.clear()

    def no_network(*args, **kwargs):
        raise AssertionError('a test tried to open a real HTTP client')
    monkeypatch.setattr(parts_http, 'new_client', no_network)

    async def public(host):
        return [PUBLIC_IP]
    monkeypatch.setattr(datasheets, '_resolve', public)
    yield
    settings_store.configure(None)


@pytest.fixture
def fake_http(monkeypatch):
    """fake_http(handler) makes every parts client use httpx.MockTransport(handler). Returns the list of requests seen."""
    seen: list[httpx.Request] = []

    def install(handler):
        def wrapped(request):
            seen.append(request)
            return handler(request)
        monkeypatch.setattr(parts_http, 'new_client', lambda timeout=20.0, **kw: httpx.AsyncClient(transport=httpx.MockTransport(wrapped)))
        return seen
    return install


@pytest.fixture
def keys(monkeypatch):
    monkeypatch.setenv('DIGIKEY_CLIENT_ID', FAKE_ID)
    monkeypatch.setenv('DIGIKEY_CLIENT_SECRET', FAKE_SECRET)
    monkeypatch.setenv('MOUSER_API_KEY', FAKE_MOUSER)


@pytest.fixture
def api(tmp_path):
    app = create_app(data_dir=tmp_path / 'data', token=TOKEN, poll=0.01, keepalive=0.05)
    with TestClient(app, base_url=HOST, headers={'X-TrustPCB-Token': TOKEN}) as client:
        yield client
