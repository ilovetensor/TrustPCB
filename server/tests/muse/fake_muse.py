"""A fake Meta Messages API for tests: scripted replies rendered as plain JSON or as an SSE stream."""
import asyncio
import json
from dataclasses import dataclass, field

import httpx

KEY = 'mk-test-0123456789abcdef-SECRET'
PIXEL = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=='
USAGE = {'input_tokens': 100, 'output_tokens': 20}


@dataclass
class Msg:
    content: list
    stop: str | None = None
    usage: dict = field(default_factory=lambda: dict(USAGE))


@dataclass
class Err:
    status: int
    message: str = 'boom'
    type: str = 'api_error'
    headers: dict = field(default_factory=dict)


@dataclass
class Boom:
    exc: Exception


class Hang:
    """Never answers; the request ends only when the caller cancels it."""


def text(value):
    return {'type': 'text', 'text': value}


def call(call_id, name, **args):
    return {'type': 'tool_use', 'id': call_id, 'name': name, 'input': args}


def end(value='Done.', usage=None):
    return Msg([text(value)], 'end_turn', usage or dict(USAGE))


def tools(*blocks, usage=None, lead=None):
    return Msg(([text(lead)] if lead else []) + list(blocks), 'tool_use', usage or dict(USAGE))


def render_sse(msg: Msg) -> str:
    events = []

    def add(kind, data):
        events.append(f'event: {kind}\ndata: {json.dumps({"type": kind, **data})}\n')

    usage = dict(msg.usage)
    add('message_start', {'message': {'id': 'msg_1', 'type': 'message', 'role': 'assistant', 'content': [], 'model': 'muse-spark-1.3',
                                       'usage': {'input_tokens': usage.get('input_tokens', 0), 'output_tokens': 1,
                                                 **({'cache_read_input_tokens': usage['cache_read_input_tokens']} if 'cache_read_input_tokens' in usage else {})}}})
    events.append('event: ping\ndata: {"type": "ping"}\n')
    for index, block in enumerate(msg.content):
        kind = block['type']
        if kind == 'text':
            add('content_block_start', {'index': index, 'content_block': {'type': 'text', 'text': ''}})
            half = max(1, len(block['text']) // 2)
            for part in (block['text'][:half], block['text'][half:]):
                if part:
                    add('content_block_delta', {'index': index, 'delta': {'type': 'text_delta', 'text': part}})
        elif kind == 'thinking':
            add('content_block_start', {'index': index, 'content_block': {'type': 'thinking', 'thinking': ''}})
            add('content_block_delta', {'index': index, 'delta': {'type': 'thinking_delta', 'thinking': block['thinking']}})
            if 'signature' in block:
                add('content_block_delta', {'index': index, 'delta': {'type': 'signature_delta', 'signature': block['signature']}})
        elif kind == 'tool_use':
            add('content_block_start', {'index': index, 'content_block': {'type': 'tool_use', 'id': block['id'], 'name': block['name'], 'input': {}}})
            raw = block['input'] if isinstance(block['input'], str) else json.dumps(block['input'])
            half = max(1, len(raw) // 2)
            for part in (raw[:half], raw[half:]):
                add('content_block_delta', {'index': index, 'delta': {'type': 'input_json_delta', 'partial_json': part}})
        else:
            add('content_block_start', {'index': index, 'content_block': dict(block)})
        add('content_block_stop', {'index': index})
    delta_usage = {k: v for k, v in usage.items() if k != 'input_tokens' or False}
    add('message_delta', {'delta': {'stop_reason': msg.stop, 'stop_sequence': None}, 'usage': delta_usage})
    add('message_stop', {})
    return '\n'.join(events) + '\n'


class FakeMuse:
    def __init__(self):
        self.script = []
        self.requests = []          # parsed request bodies
        self.headers = []
        self.responder = None       # callable(body) -> item, used when the script is empty
        self.transport = httpx.MockTransport(self.handle)

    def reply(self, *items):
        self.script.extend(items)
        return self

    async def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(json.loads(request.content))
        self.headers.append(dict(request.headers))
        assert request.url.path == '/v1/messages' and request.method == 'POST'
        item = self.script.pop(0) if self.script else (self.responder(body) if self.responder else None)
        assert item is not None, 'the fake API received more requests than it had replies for'
        if callable(item) and item is not Hang and not isinstance(item, (Msg, Err, Boom)):
            item = item(body)
        if isinstance(item, Boom):
            raise item.exc
        if isinstance(item, Hang) or item is Hang:
            await asyncio.Event().wait()
        if isinstance(item, Err):
            return httpx.Response(item.status, headers=item.headers, json={'type': 'error', 'error': {'type': item.type, 'message': item.message}})
        if body.get('stream'):
            return httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=render_sse(item).encode())
        return httpx.Response(200, json={'id': 'msg_1', 'type': 'message', 'role': 'assistant', 'model': body['model'], 'content': item.content,
                                         'stop_reason': item.stop, 'stop_sequence': None, 'usage': item.usage})


class Sink:
    def __init__(self):
        self.events = []

    async def emit(self, kind, payload):
        self.events.append((kind, payload))

    async def interact(self, kind, payload):
        return {}

    def of(self, kind):
        return [p for k, p in self.events if k == kind]

    def kinds(self):
        return [k for k, _ in self.events]


def results_in(body):
    """tool_result blocks of the last user message of a request body."""
    last = body['messages'][-1]
    return [b for b in last['content'] if b.get('type') == 'tool_result']
