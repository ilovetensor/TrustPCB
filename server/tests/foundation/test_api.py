import asyncio
import io
import time
import zipfile
from pathlib import Path

import pytest

from trustpcb import api
from trustpcb.runners.scripted import ScriptedRunner


def zip_bytes(**files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


# ---- auth ---------------------------------------------------------------------------

def test_security_rejects_missing_or_wrong_token_foreign_host_and_origin(client):
    assert client.get('/api/projects', headers={'X-TrustPCB-Token': ''}).status_code == 401
    assert client.get('/api/projects', headers={'X-TrustPCB-Token': 'wrong'}).status_code == 401
    assert client.get('/api/projects', headers={'X-TrustPCB-Token': 'caf\u00e9'.encode('utf-8')}).status_code == 401   # non-ASCII bytes on the wire: 401, not a 500
    assert client.get('/api/projects', headers={'X-TrustPCB-Token': b'\xff\xfe'}).status_code == 401
    assert client.get('/api/projects', headers={'Host': 'evil.example'}).status_code == 403
    assert client.get('/api/projects', headers={'Host': '127.0.0.1:9999'}).status_code == 403
    assert client.get('/api/projects', headers={'Origin': 'https://evil.example'}).status_code == 403
    assert client.get('/api/projects', headers={'Origin': 'http://localhost:3118'}).status_code == 200
    assert client.get('/api/projects').status_code == 200


def test_health_needs_no_token_but_still_checks_host(client):
    assert client.get('/api/health', headers={'X-TrustPCB-Token': ''}).json() == {'status': 'ok'}
    assert client.get('/health', headers={'X-TrustPCB-Token': ''}).status_code == 200
    assert client.get('/health', headers={'Host': 'evil.example'}).status_code == 403


def test_token_comes_from_the_environment(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv('TRUSTPCB_TOKEN', 'from-env')
    with TestClient(api.create_app(data_dir=tmp_path / 'd'), base_url='http://127.0.0.1:8118') as c:
        assert c.get('/api/projects', headers={'X-TrustPCB-Token': 'from-env'}).status_code == 200
        assert c.get('/api/projects', headers={'X-TrustPCB-Token': 'secret'}).status_code == 401


# ---- projects, versions, bootstrap --------------------------------------------------

def test_bootstrap_reports_default_runner(client):
    body = client.get('/api/bootstrap').json()
    assert body['profile']['name'] == 'scripted' and body['runners']['scripted'] is True


def test_projects_list_get_and_missing(client):
    project = client.post('/api/projects', json={'name': 'nRF board'}).json()
    assert project['name'] == 'nRF board'
    assert client.post('/api/projects', json={'name': ''}).status_code == 422
    listed = client.get('/api/projects').json()
    assert listed[0]['id'] == project['id'] and listed[0]['version_count'] == 0
    detail = client.get(f"/api/projects/{project['id']}").json()
    assert detail['versions'] == [] and detail['dashboard']['last_review'] is None
    assert client.get('/api/projects/nope').status_code == 404


def test_project_dashboard_counts_follow_the_latest_review(client, store_of):
    store = store_of(client)
    project = store.create_project('Dash')
    version = store.create_version(project['id'])
    review = store.create_review(version['id'])
    store.create_finding(review['id'], 'a', 'A', 'high')
    store.create_finding(review['id'], 'b', 'B', 'low')
    detail = client.get(f"/api/projects/{project['id']}").json()
    assert detail['dashboard']['open_by_severity']['high'] == 1
    assert detail['dashboard']['trend'][0]['number'] == 1 and detail['versions'][0]['reviews'][0]['counts']['low'] == 1
    version_view = client.get(f"/api/versions/{version['id']}").json()
    assert version_view['reviews'][0]['id'] == review['id'] and version_view['artifacts'] == []
    assert client.get('/api/versions/nope').status_code == 404


@pytest.fixture
def store_of():
    return lambda client: client.app.state.store


# ---- service hooks ------------------------------------------------------------------

def test_service_functions_are_called_with_the_documented_arguments(client, store_of, fakes):
    calls = {}

    def start_review(*, store, manager, version_id, goal, depth, extent=None):
        calls['review'] = (version_id, goal, depth)
        return {'review_id': 'r1'}

    async def send_message(*, store, manager, thread_id, text, selected, request_id):
        calls['message'] = (thread_id, text, selected, request_id)
        return {'turn_id': 't'}

    fakes.module('trustpcb.review.service', start_review=start_review)
    fakes.module('trustpcb.review.followup', send_message=send_message)
    store = store_of(client)
    project = store.create_project('p')
    version = store.create_version(project['id'])
    review = store.create_review(version['id'])
    thread = store.create_thread(review['id'])
    assert client.post(f"/api/versions/{version['id']}/reviews", json={'goal': {'purpose': 'BLE'}, 'depth': 'quick'}).json() == {'review_id': 'r1'}
    assert calls['review'] == (version['id'], {'purpose': 'BLE'}, 'quick')
    body = {'text': 'what about C12?', 'selected': [{'type': 'pin', 'targets': ['ref:C12']}], 'request_id': 'req-1'}
    assert client.post(f"/api/threads/{thread['id']}/messages", json=body).json() == {'turn_id': 't'}
    assert calls['message'] == (thread['id'], 'what about C12?', body['selected'], 'req-1')
    assert client.post(f"/api/threads/{thread['id']}/messages", json={'text': '', 'request_id': 'r'}).status_code == 422
    assert client.post('/api/versions/nope/reviews', json={}).status_code == 404


def test_service_value_error_is_a_400_and_conflict_a_409(client, store_of, fakes):
    from trustpcb.sessions import SessionConflict

    def start_review(**kwargs):
        raise ValueError('Goal is incomplete')

    fakes.module('trustpcb.review.service', start_review=start_review)
    store = store_of(client)
    version = store.create_version(store.create_project('p')['id'])
    response = client.post(f"/api/versions/{version['id']}/reviews", json={})
    assert response.status_code == 400 and response.json()['detail'] == 'Goal is incomplete'

    def busy(**kwargs):
        raise SessionConflict('A run is already active in this project')

    fakes.module('trustpcb.review.service', start_review=busy)
    assert client.post(f"/api/versions/{version['id']}/reviews", json={}).status_code == 409


# ---- upload -------------------------------------------------------------------------

@pytest.fixture
def ingest_spy(fakes):
    seen = []

    def ingest(*, store, version, upload_path, filename):
        path = Path(upload_path)
        listing = sorted(p.relative_to(path).as_posix() for p in path.rglob('*') if p.is_file()) if path.is_dir() else None
        seen.append({'version': version, 'is_dir': path.is_dir(), 'filename': filename, 'listing': listing,
                     'bytes': None if path.is_dir() else path.read_bytes()})
        return {'status': 'ready', 'detection': {'board': 'main.kicad_pcb'}}

    fakes.module('trustpcb.intake.service', ingest=ingest)
    return seen


def test_zip_upload_streams_to_disk_and_calls_ingest(client, ingest_spy):
    project = client.post('/api/projects', json={'name': 'p'}).json()
    data = zip_bytes(**{'main.kicad_pcb': '(kicad_pcb)'})
    response = client.post(f"/api/projects/{project['id']}/versions", files={'file': ('board.zip', data, 'application/zip')}, data={'label': 'rev A'})
    assert response.status_code == 200, response.text
    version = response.json()
    assert version['number'] == 1 and version['label'] == 'rev A' and version['status'] == 'ready'
    assert version['detection'] == {'board': 'main.kicad_pcb'} and len(version['upload_hash']) == 64 and version['duplicate_of'] is None
    seen = ingest_spy[0]
    assert seen['bytes'] == data and seen['filename'] == 'board.zip' and seen['version']['project_id'] == project['id']
    again = client.post(f"/api/projects/{project['id']}/versions", files={'file': ('board.zip', data, 'application/zip')}).json()
    assert again['number'] == 2 and again['duplicate_of'] == version['id'] and again['upload_hash'] == version['upload_hash']
    uploads = Path(client.app.state.store.data_dir) / 'uploads'
    assert list(uploads.iterdir()) == []  # temporary upload folders are removed


def test_raw_body_upload(client, ingest_spy):
    project = client.post('/api/projects', json={'name': 'p'}).json()
    response = client.post(f"/api/projects/{project['id']}/versions?label=raw", content=b'PK-not-really', headers={'X-Filename': 'x.zip', 'Content-Type': 'application/octet-stream'})
    assert response.status_code == 200 and response.json()['label'] == 'raw'
    assert ingest_spy[0]['bytes'] == b'PK-not-really' and ingest_spy[0]['filename'] == 'x.zip'


def test_folder_upload_gives_ingest_a_directory(client, ingest_spy):
    project = client.post('/api/projects', json={'name': 'p'}).json()
    files = [('file', ('board/a.kicad_pcb', b'pcb', 'text/plain')), ('file', ('board/a.kicad_pro', b'{}', 'text/plain')), ('file', ('board/libs/x.kicad_sym', b'sym', 'text/plain'))]
    response = client.post(f"/api/projects/{project['id']}/versions", files=files)
    assert response.status_code == 200, response.text
    assert ingest_spy[0]['is_dir'] and ingest_spy[0]['listing'] == ['board/a.kicad_pcb', 'board/a.kicad_pro', 'board/libs/x.kicad_sym']


def test_upload_size_limit_is_enforced_while_streaming(client, ingest_spy, monkeypatch):
    project = client.post('/api/projects', json={'name': 'p'}).json()
    monkeypatch.setattr(api, 'MAX_UPLOAD_BYTES', 1000)
    big = b'x' * 5000
    chunked = client.post(f"/api/projects/{project['id']}/versions", content=iter([big[:2000], big[2000:]]), headers={'X-Filename': 'big.zip'})
    assert chunked.status_code == 413
    declared = client.post(f"/api/projects/{project['id']}/versions", content=big, headers={'X-Filename': 'big.zip'})
    assert declared.status_code == 413
    multipart = client.post(f"/api/projects/{project['id']}/versions", files={'file': ('big.zip', big)})
    assert multipart.status_code == 413
    assert ingest_spy == [] and client.get(f"/api/projects/{project['id']}").json()['versions'] == []
    assert list((Path(client.app.state.store.data_dir) / 'uploads').iterdir()) == []


def test_upload_rejects_traversal_duplicates_and_empty(client, ingest_spy):
    project = client.post('/api/projects', json={'name': 'p'}).json()
    url = f"/api/projects/{project['id']}/versions"
    assert client.post(url, files={'file': ('../../evil.zip', b'x')}).status_code == 400
    assert client.post(url, files=[('file', ('a/b.txt', b'1')), ('file', ('a/b.txt', b'2'))]).status_code == 400
    assert client.post(url, data={'label': 'only a field'}).status_code == 400
    assert client.post(url, content=b'', headers={'X-Filename': 'empty.zip'}).status_code == 400
    assert client.post('/api/projects/nope/versions', content=b'x').status_code == 404
    assert ingest_spy == [] and client.get(f"/api/projects/{project['id']}").json()['versions'] == []


def test_ingest_value_error_marks_the_version_failed(client, fakes):
    def ingest(**kwargs):
        raise ValueError('No .kicad_pcb found in the zip')

    fakes.module('trustpcb.intake.service', ingest=ingest)
    project = client.post('/api/projects', json={'name': 'p'}).json()
    response = client.post(f"/api/projects/{project['id']}/versions", content=b'zip', headers={'X-Filename': 'a.zip'})
    assert response.status_code == 422 and 'No .kicad_pcb' in response.json()['detail']
    version = client.get(f"/api/versions/{response.json()['version_id']}").json()
    assert version['status'] == 'failed' and 'No .kicad_pcb' in version['error']


def test_async_ingest_is_supported(client, fakes):
    async def ingest(*, store, version, upload_path, filename):
        await asyncio.sleep(0)
        return {'status': 'ready'}

    fakes.module('trustpcb.intake.service', ingest=ingest)
    project = client.post('/api/projects', json={'name': 'p'}).json()
    assert client.post(f"/api/projects/{project['id']}/versions", content=b'zip', headers={'X-Filename': 'a.zip'}).json()['status'] == 'ready'


# ---- findings, assumptions, reports, artifacts --------------------------------------

@pytest.fixture
def review_setup(client, store_of):
    store = store_of(client)
    project = store.create_project('p')
    version = store.create_version(project['id'])
    review = store.create_review(version['id'])
    finding = store.create_finding(review['id'], 'fp|1', 'Detour', 'medium', 'measured', what='long way round')
    return store, review, finding


def test_findings_read_filter_and_history(client, review_setup):
    store, review, finding = review_setup
    listed = client.get(f"/api/reviews/{review['id']}/findings").json()
    assert [f['code'] for f in listed] == ['F-001'] and listed[0]['what'] == 'long way round'
    assert client.get(f"/api/reviews/{review['id']}/findings", params={'status': 'dismissed'}).json() == []
    assert client.get(f"/api/findings/{finding['id']}/history").json()[0]['source'] == 'review'
    assert client.get('/api/findings/nope/history').status_code == 404
    assert client.get('/api/reviews/nope/findings').status_code == 404


def test_patch_finding_dismiss_requires_reason_and_reopen_clears_it(client, review_setup):
    store, review, finding = review_setup
    url = f"/api/findings/{finding['id']}"
    assert client.patch(url, json={'status': 'dismissed'}).status_code == 400
    assert client.patch(url, json={'status': 'dismissed', 'reason': '  '}).status_code == 400
    assert client.patch(url, json={'status': 'fixed'}).status_code == 400
    dismissed = client.patch(url, json={'status': 'dismissed', 'reason': 'intentional layout'}).json()
    assert dismissed['status'] == 'dismissed' and dismissed['dismissed_reason'] == 'intentional layout'
    reopened = client.patch(url, json={'status': 'open'}).json()
    assert reopened['status'] == 'open' and reopened['dismissed_reason'] is None
    history = client.get(f'{url}/history').json()
    assert [h['source'] for h in history] == ['review', 'user', 'user'] and history[1]['reason'] == 'intentional layout'
    assert history[1]['after']['status'] == 'dismissed'
    events = client.get(f"/api/reviews/{review['id']}/events").json()
    assert [e['type'] for e in events] == ['finding_changed', 'finding_changed'] and events[0]['payload']['status'] == 'dismissed'
    assert client.patch('/api/findings/nope', json={'status': 'open'}).status_code == 404


def test_assumptions_and_report_revisions(client, review_setup):
    store, review, _ = review_setup
    id = review['id']
    store.add_assumption(id, 'substrate', {'epsilon_r': 4.4}, 'default')
    assert client.get(f'/api/reviews/{id}/assumptions').json()[0]['source'] == 'default'
    assert client.get(f'/api/reviews/{id}/report').status_code == 404
    store.add_report_revision(id, 'review complete', {'counts': {'high': 1}})
    store.add_report_revision(id, 'follow-up', {'counts': {'high': 2}})
    assert client.get(f'/api/reviews/{id}/report').json()['snapshot'] == {'counts': {'high': 2}}
    revisions = client.get(f'/api/reviews/{id}/report/revisions').json()
    assert [r['number'] for r in revisions] == [1, 2] and all('snapshot' not in r for r in revisions)
    assert client.get(f'/api/reviews/{id}/report/1.json').json() == {'counts': {'high': 1}}
    assert client.get(f'/api/reviews/{id}/report/9.json').status_code == 404
    assert client.get(f'/api/reviews/{id}/report/latest.json').status_code == 404
    assert client.get(f'/api/reviews/{id}').json()['report_revision'] == 2


def test_report_html_goes_through_the_review_service(client, review_setup, fakes):
    store, review, _ = review_setup
    store.add_report_revision(review['id'], 'done', {'x': 1})
    seen = {}

    def render_revision(*, store, review, revision, fmt):
        seen.update(fmt=fmt, number=revision['number'])
        return b'<html>report</html>'

    fakes.module('trustpcb.review.report', render_revision=render_revision)
    response = client.get(f"/api/reviews/{review['id']}/report/1.html")
    assert response.status_code == 200 and response.content == b'<html>report</html>'
    assert response.headers['content-type'].startswith('text/html') and seen == {'fmt': 'html', 'number': 1}


def test_artifacts_are_served_with_safe_headers(client, review_setup):
    store, review, _ = review_setup
    path = store.version_dir(review['version_id']) / 'top.svg'
    path.write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
    artifact = store.add_artifact('version', review['version_id'], 'layer_svg', path, mime='image/svg+xml')
    response = client.get(f"/api/artifacts/{artifact['id']}")
    assert response.status_code == 200 and response.headers['content-type'] == 'image/svg+xml'
    assert response.headers['x-content-type-options'] == 'nosniff' and 'sandbox' in response.headers['content-security-policy']
    listed = client.get(f"/api/versions/{review['version_id']}").json()['artifacts']
    assert listed[0]['url'] == f"/api/artifacts/{artifact['id']}" and 'path' not in listed[0]
    path.unlink()
    assert client.get(f"/api/artifacts/{artifact['id']}").status_code == 404
    assert client.get('/api/artifacts/nope').status_code == 404


def test_compare_needs_both_versions(client):
    assert client.get('/api/compare').status_code == 400
    assert client.get('/api/compare', params={'from': 'a', 'to': 'b'}).status_code == 404


# ---- events, cancel, respond --------------------------------------------------------

def test_event_replay_endpoints_and_cursor_validation(client, review_setup):
    store, review, _ = review_setup
    thread = store.create_thread(review['id'])
    first = store.append_event(review['chat_id'], None, 'phase_changed', {'phase': 'intake'})
    store.append_event(review['chat_id'], None, 'budget', {})
    store.append_event(thread['chat_id'], None, 'followup_started', {})
    assert [e['type'] for e in client.get(f"/api/reviews/{review['id']}/events").json()] == ['phase_changed', 'budget']
    assert [e['type'] for e in client.get(f"/api/reviews/{review['id']}/events", params={'after': first['sequence']}).json()] == ['budget']
    assert [e['type'] for e in client.get(f"/api/threads/{thread['id']}/events").json()] == ['followup_started']
    assert client.get(f"/api/reviews/{review['id']}/stream", headers={'Last-Event-ID': 'abc'}).status_code == 400
    assert client.get('/api/reviews/nope/stream').status_code == 404
    assert client.get('/api/threads/nope/stream').status_code == 404


def test_cancel_idle_and_unknown(client, review_setup):
    _, review, _ = review_setup
    assert client.post(f"/api/reviews/{review['id']}/cancel").json() == {'status': 'idle'}
    assert client.post('/api/reviews/nope/cancel').status_code == 404
    assert client.post('/api/threads/nope/cancel').status_code == 404
    assert client.post('/api/requests/nope/respond', json={'allow': True}).status_code == 409


def test_start_cancel_and_respond_through_the_api(client, store_of, fakes):
    """A fake review service starts a real managed run; cancel and respond go through HTTP."""
    store = store_of(client)
    manager = client.app.state.manager
    manager.runner_factory = lambda profile: ScriptedRunner([
        {'text': 'begin'}, {'interact': 'test', 'payload': {'title': 'Run?'}},
        {'if_answer': {'allow': True}, 'then': [{'text': 'approved'}, {'sleep': 30}], 'else': [{'text': 'declined'}]}])

    async def start_review(*, store, manager, version_id, goal, depth, extent=None):
        review = store.create_review(version_id, goal=goal, depth=depth)
        from trustpcb.runners.base import RunRequest

        async def work(ctx):
            await ctx.run_agent(RunRequest(None, 'sys', 'go', None, 5, {}))

        started = await manager.start(review['chat_id'], 'start-1', work, text='go')
        return {'review_id': review['id'], **started}

    fakes.module('trustpcb.review.service', start_review=start_review)
    version = store.create_version(store.create_project('p')['id'])
    started = client.post(f"/api/versions/{version['id']}/reviews", json={'goal': {'a': 1}}).json()
    chat = store.get_review(started['review_id'])['chat_id']

    def wait_for(kind):
        for _ in range(300):
            found = [e for e in store.events(chat) if e['type'] == kind]
            if found:
                return found[0]
            time.sleep(0.01)
        raise AssertionError(kind)

    asked = wait_for('test')
    second = client.post(f"/api/versions/{version['id']}/reviews", json={})
    assert second.status_code == 200  # a second review is not blocked by the first: runs belong to their own chat
    second_chat = store.get_review(second.json()['review_id'])['chat_id']
    assert client.post(f"/api/reviews/{second.json()['review_id']}/cancel").json() == {'status': 'canceled'}
    assert [e['type'] for e in store.events(second_chat)][-1] == 'canceled'
    assert client.post(f"/api/requests/{asked['payload']['request_id']}/respond", json={}).status_code == 400
    assert client.post(f"/api/requests/{asked['payload']['request_id']}/respond", json={'allow': True}).json() == {'status': 'received'}
    wait_for('request_resolved')
    for _ in range(300):
        if 'approved' in str(store.events(chat)):
            break
        time.sleep(0.01)
    assert client.post(f"/api/reviews/{started['review_id']}/cancel").json() == {'status': 'canceled'}
    last = wait_for('canceled')
    assert last['turn_id'] == started['turn_id']
    assert client.get(f"/api/reviews/{started['review_id']}").json()['chat_status'] == 'canceled'
