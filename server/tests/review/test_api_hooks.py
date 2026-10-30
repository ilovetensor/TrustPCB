"""The review hooks as the HTTP API calls them (signatures, JSON-safe returns, report files)."""
import json
import time
from pathlib import Path

from fastapi.testclient import TestClient

from trustpcb.api import create_app
from trustpcb.review import service
from trustpcb.runners.scripted import ScriptedRunner

SCRIPTS = Path(__file__).parent / 'scripts'
HOST = 'http://127.0.0.1:8118'


def wait_idle(client, path, key='chat_status', want=('completed', 'failed', 'canceled'), timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(path).json()
        if body.get(key) in want:
            return body
        time.sleep(0.05)
    raise AssertionError(f'{path} did not finish: {body}')


def test_a_review_a_followup_and_the_reports_through_the_api(tmp_path, sample_board, fakes):
    clock = fakes.Clock()
    lab = fakes.Lab(sample_board, clock)
    service.configure(lab=lab, clock=clock, poll_s=1.0)
    script = {'steps': json.loads((SCRIPTS / 'quick_complete.json').read_text())}
    app = create_app(data_dir=tmp_path / 'data', token='t', runner_factory=lambda profile: ScriptedRunner(script['steps']), poll=0.01, keepalive=0.05)
    try:
        with TestClient(app, base_url=HOST, headers={'X-TrustPCB-Token': 't'}) as client:
            project = client.post('/api/projects', json={'name': 'nRF node'}).json()
            version = app.state.store.create_version(project['id'])
            started = client.post(f"/api/versions/{version['id']}/reviews", json={'goal': {'purpose': 'Sensor', 'band_hz': [2.4e9, 2.4835e9]}, 'depth': 'quick'})
            assert started.status_code == 200, started.text
            review = started.json()
            assert review['depth'] == 'quick' and review['profile']['name'] == 'scripted' and review['turn_id']
            assert client.post(f"/api/versions/{version['id']}/reviews", json={'goal': {}, 'depth': 'bogus'}).status_code == 400
            done = wait_idle(client, f"/api/reviews/{review['id']}")
            assert done['end_state'] == 'complete' and done['counts']['medium'] == 1 and done['report_revision'] == 1
            findings = client.get(f"/api/reviews/{review['id']}/findings").json()
            assert findings[0]['code'] == 'F-001' and findings[0]['marks'][0]['resolved']
            assert client.get(f"/api/reviews/{review['id']}/assumptions").json()
            events = client.get(f"/api/reviews/{review['id']}/events").json()
            assert {'phase_changed', 'finding_upserted', 'report_ready', 'completed'} <= {e['type'] for e in events}
            html = client.get(f"/api/reviews/{review['id']}/report/1.html")
            assert html.status_code == 200 and html.headers['content-type'].startswith('text/html') and 'F-001' in html.text
            assert 'default-src' in html.headers['content-security-policy']
            assert client.get(f"/api/reviews/{review['id']}/report/1.json").json()['findings'][0]['code'] == 'F-001'
            assert client.get(f"/api/reviews/{review['id']}/report/1.pdf").status_code in (200, 400)       # a PDF where a browser exists, a clear 400 where not
            # follow-up
            thread = client.post(f"/api/reviews/{review['id']}/threads", json={}).json()
            script['steps'] = json.loads((SCRIPTS / 'followup_new_issue.json').read_text())
            sent = client.post(f"/api/threads/{thread['id']}/messages", json={'text': 'Is C14 too close to U3?', 'selected': [{'type': 'part', 'id': 'ref:C14'}], 'request_id': 'r1'})
            assert sent.status_code == 200, sent.text
            wait_idle(client, f"/api/reviews/{review['id']}", key='report_revision', want=(2,))
            revisions = client.get(f"/api/reviews/{review['id']}/report/revisions").json()
            assert [r['number'] for r in revisions] == [1, 2] and 'snapshot' not in revisions[0]
            latest = client.get(f"/api/reviews/{review['id']}/report").json()
            assert latest['number'] == 2 and latest['snapshot']['changes_since_last']['new_findings'][0]['code'] == 'F-002'
            first = client.get(f"/api/reviews/{review['id']}/report/1.json").json()
            assert [f['code'] for f in first['findings']] == ['F-001']                              # revision 1 still says what it said
            assert client.post(f"/api/threads/{thread['id']}/messages", json={'text': 'Is C14 too close to U3?', 'selected': [], 'request_id': 'r1'}).json()['duplicate'] is True
            events = client.get(f"/api/threads/{thread['id']}/events").json()
            assert {'followup_started', 'report_revised', 'finding_changed'} <= {e['type'] for e in events}
            # dismiss through the API, then compare two versions
            patched = client.patch(f"/api/findings/{findings[0]['id']}", json={'status': 'dismissed', 'reason': 'intentional'})
            assert patched.json()['status'] == 'dismissed'
            second = app.state.store.create_version(project['id'])
            result = client.get('/api/compare', params={'from': version['id'], 'to': second['id']})
            assert result.status_code == 200 and result.json()['from']['number'] == 1 and result.json()['findings'] is None
            assert client.get('/api/compare', params={'from': version['id'], 'to': version['id']}).status_code == 200
    finally:
        service.configure()
