"""Hardening: the read-only demo mode, and uploads that go wrong (no KiCad, not a board, a board that takes too long)."""
import io
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from trustpcb.api import create_app

HOST = 'http://127.0.0.1:8118'
TOKEN = 'secret'
PATCH_BOARD = Path(__file__).resolve().parents[3] / 'samples' / 'patch-2g5' / 'patch_antenna.kicad_pcb'


def zip_bytes(**files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


@pytest.fixture
def make_client(tmp_path, monkeypatch):
    monkeypatch.setenv('TRUSTPCB_LAB', 'baseline')
    clients = []

    def make():
        app = create_app(data_dir=tmp_path / 'data', token=TOKEN, poll=0.01, keepalive=0.05)
        client = TestClient(app, base_url=HOST, headers={'X-TrustPCB-Token': TOKEN})
        client.__enter__()
        clients.append(client)
        return client
    yield make
    for client in clients:
        client.__exit__(None, None, None)


def test_demo_readonly_refuses_every_mutation_with_a_clear_403(make_client, monkeypatch):
    open_client = make_client()
    project = open_client.post('/api/projects', json={'name': 'Finished board'}).json()
    monkeypatch.setenv('TRUSTPCB_DEMO_READONLY', '1')
    client = make_client()
    assert client.get('/api/bootstrap').json()['demo_readonly'] is True
    assert client.get(f'/api/projects/{project["id"]}').status_code == 200              # reading still works
    for method, path, kwargs in (
        ('post', '/api/projects', {'json': {'name': 'x'}}),
        ('post', f'/api/projects/{project["id"]}/versions', {'files': {'file': ('a.zip', zip_bytes(a='b'), 'application/zip')}}),
        ('post', '/api/versions/nope/reviews', {'json': {}}),
        ('post', '/api/reviews/nope/cancel', {}),
        ('post', '/api/reviews/nope/threads', {'json': {}}),
        ('patch', '/api/findings/nope', {'json': {'status': 'dismissed', 'reason': 'x'}}),
        ('post', '/api/threads/nope/messages', {'json': {'text': 'hi', 'request_id': 'r'}}),
        ('post', '/api/requests/nope/respond', {'json': {'allow': True}}),
    ):
        response = getattr(client, method)(path, **kwargs)
        assert response.status_code == 403 and response.json()['code'] == 'demo_readonly' and 'read-only demo' in response.json()['detail'], (method, path)
    assert len(client.get('/api/projects').json()) == 1, 'nothing was created'
    assert client.post('/api/projects', json={'name': 'x'}, headers={'X-TrustPCB-Token': 'wrong'}).status_code == 401, 'authentication is still checked first'


def test_demo_readonly_is_off_by_default(make_client):
    client = make_client()
    assert client.get('/api/bootstrap').json()['demo_readonly'] is False
    assert client.post('/api/projects', json={'name': 'x'}).status_code == 200


def upload(client, files):
    project = client.post('/api/projects', json={'name': 'b'}).json()
    return client.post(f'/api/projects/{project["id"]}/versions', files={'file': ('b.zip', files, 'application/zip')})


def test_a_zip_without_a_board_is_refused_in_plain_words(make_client):
    response = upload(make_client(), zip_bytes(**{'readme.txt': 'hello', 'photo.png': 'abc'}))
    assert response.status_code == 422 and '.kicad_pcb' in response.json()['detail']


def test_a_file_that_is_not_a_kicad_board_names_the_file_and_what_to_upload(make_client):
    response = upload(make_client(), zip_bytes(**{'a.kicad_pcb': 'this is not a board'}))
    detail = response.json()['detail']
    assert response.status_code == 422 and 'a.kicad_pcb' in detail and 'KiCad 9 or newer' in detail


def test_a_board_that_takes_too_long_to_read_ends_with_a_message(make_client, monkeypatch):
    import time
    from trustpcb.intake import service
    monkeypatch.setenv('TRUSTPCB_PARSE_TIMEOUT_S', '0.2')
    monkeypatch.setattr(service, 'parse_board', lambda *a, **k: time.sleep(1.0))
    response = upload(make_client(), zip_bytes(**{'a.kicad_pcb': PATCH_BOARD.read_text()}))
    assert response.status_code == 422 and 'longer than 0.2 seconds' in response.json()['detail'] and 'TRUSTPCB_PARSE_TIMEOUT_S' in response.json()['detail']


def test_a_giant_board_file_is_refused_before_parsing(make_client, monkeypatch):
    monkeypatch.setenv('TRUSTPCB_MAX_PCB_MB', '0.001')
    response = upload(make_client(), zip_bytes(**{'a.kicad_pcb': PATCH_BOARD.read_text()}))
    assert response.status_code == 422 and 'TRUSTPCB_MAX_PCB_MB' in response.json()['detail']


def test_without_kicad_the_upload_still_works_and_says_what_was_skipped(make_client, monkeypatch):
    monkeypatch.setenv('KICAD_CLI_PATH', '/nonexistent/kicad-cli')
    client = make_client()
    status = client.get('/api/bootstrap').json()['kicad']
    assert status['available'] is False and 'KICAD_CLI_PATH' in status['reason']
    response = upload(client, zip_bytes(**{'a.kicad_pcb': PATCH_BOARD.read_text()}))
    assert response.status_code == 200 and response.json()['status'] == 'ready'
    warnings = ' '.join(response.json()['stackup']['warnings'])
    assert 'KiCad unavailable' in warnings and 'skipped' in warnings
