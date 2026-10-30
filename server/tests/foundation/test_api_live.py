"""SSE over a real socket: replay, resume from Last-Event-ID, live follow, keepalive."""
import threading
import time

import httpx
import pytest
import uvicorn

from trustpcb.api import create_app

HEADERS = {'Host': '127.0.0.1:8008', 'X-TrustPCB-Token': 'secret'}


@pytest.fixture
def live(tmp_path):
    app = create_app(data_dir=tmp_path / 'data', token='secret', poll=0.01, keepalive=0.1)
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=0, log_level='warning', lifespan='on'))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(500):
        if server.started and server.servers:
            break
        time.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield app, f'http://127.0.0.1:{port}'
    server.should_exit = True
    thread.join(10)


class Frames:
    """Incremental SSE reader: one text iterator per response, frames split on blank lines."""

    def __init__(self, response):
        self.chunks, self.buffer, self.frames = response.iter_text(), '', []

    def until(self, want, timeout=5):
        deadline = time.time() + timeout
        while not want(self.frames) and time.time() < deadline:
            while '\n\n' in self.buffer:
                frame, self.buffer = self.buffer.split('\n\n', 1)
                self.frames.append(frame)
            if want(self.frames):
                break
            self.buffer += next(self.chunks)
        return self.frames


def ids(frames):
    return [int(f.split('\n')[0][4:]) for f in frames if f.startswith('id: ')]


def test_stream_replays_resumes_and_follows(live):
    app, base = live
    store = app.state.store
    project = store.create_project('p')
    review = store.create_review(store.create_version(project['id'])['id'])
    chat = review['chat_id']
    sequences = [store.append_event(chat, None, 'budget', {'n': n})['sequence'] for n in range(3)]
    url = f"{base}/api/reviews/{review['id']}/stream"
    with httpx.Client(timeout=10) as client:
        with client.stream('GET', url, headers=HEADERS) as response:
            assert response.status_code == 200 and response.headers['content-type'].startswith('text/event-stream')
            frames = Frames(response).until(lambda f: len(ids(f)) >= 3)
        assert frames[0] == ': connected' and ids(frames) == sequences
        with client.stream('GET', url, params={'after': sequences[0]}, headers=HEADERS) as response:
            assert ids(Frames(response).until(lambda f: len(ids(f)) >= 2)) == sequences[1:]
        with client.stream('GET', url, headers={**HEADERS, 'Last-Event-ID': str(sequences[1])}) as response:
            reader = Frames(response)
            assert ids(reader.until(lambda f: len(ids(f)) >= 1)) == [sequences[2]]
            late = store.append_event(chat, None, 'thought', {'text': 'live'})['sequence']
            assert late in ids(reader.until(lambda f: late in ids(f)))
        with client.stream('GET', url, params={'after': sequences[-1]}, headers=HEADERS) as response:
            assert ': keepalive' in Frames(response).until(lambda f: ': keepalive' in f)


def test_stream_requires_the_token(live):
    _, base = live
    response = httpx.get(f'{base}/api/reviews/x/stream', headers={'Host': '127.0.0.1:8008'})
    assert response.status_code == 401
