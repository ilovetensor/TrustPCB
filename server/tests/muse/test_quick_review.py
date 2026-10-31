"""A whole Quick review through the real review service, with Muse's replies scripted at the HTTP level.

The review tests' fakes (lab, clock) are loaded from its conftest by path; nothing of its fixtures leaks in.
"""
import importlib.util
import json
from pathlib import Path

import pytest

from fake_muse import KEY, PIXEL, FakeMuse, Msg, end, text
from trustpcb import runners
from trustpcb.review import service
from trustpcb.runners.muse import MuseRunner
from trustpcb.sessions import SessionManager
from trustpcb.storage import Store

REVIEW_TESTS = Path(__file__).resolve().parents[1] / 'review'
spec = importlib.util.spec_from_file_location('review_lane_conftest', REVIEW_TESTS / 'conftest.py')
rc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rc)
STEPS = json.loads((REVIEW_TESTS / 'scripts' / 'quick_complete.json').read_text())


def model_reply(steps):
    """The model, stateless: the number of assistant messages so far says which step is next."""
    def respond(body):
        n = sum(1 for m in body['messages'] if m['role'] == 'assistant')
        if n >= len(steps):
            return end('The review is complete.', usage={'input_tokens': 900, 'output_tokens': 40})
        step = steps[n]
        blocks = []
        if n == 0:
            blocks.append({'type': 'thinking', 'thinking': 'Plan: read the board, brief, list candidates.', 'signature': 'sig-quick'})
        blocks.append(text(f'Step {n + 1}.'))
        blocks.append({'type': 'tool_use', 'id': f'call_{n}', 'name': step['tool'], 'input': step.get('args', {})})
        return Msg(blocks, 'tool_use', {'input_tokens': 500 + 10 * n, 'output_tokens': 25, 'cache_read_input_tokens': 400})
    return respond


@pytest.fixture
def quick(tmp_path, sample_board, monkeypatch, mode):
    monkeypatch.setenv('TRUSTPCB_RUNNER', 'muse_api')
    api = FakeMuse()
    api.responder = model_reply(STEPS)
    store = Store(tmp_path / 'data')
    clock = rc.FakeClock()
    lab = rc.FakeLab(sample_board, clock)

    def factory(profile):
        return MuseRunner(data_dir=store.data_dir, transport=api.transport)

    manager = SessionManager(store, runner_factory=factory)
    project = store.create_project('nRF node')
    version = store.create_version(project['id'], label='first')
    env = rc.Env(store, manager, clock, lab, project, version)
    service.configure(lab=lab, clock=clock, poll_s=1.0)
    yield env, api
    service.configure()


@pytest.fixture
def sample_board():
    from trustpcb.pcb.model import parse_board
    return parse_board(rc.SAMPLE, 'v1')


def test_muse_profile_lists_tools_by_phase():
    profile = runners.resolve_profile('muse_api')
    assert profile['name'] == 'muse_api' and profile['model'] == 'muse-spark-1.3' and not profile['static_tool_list']


async def test_quick_review_completes_with_scripted_model_replies(quick):
    env, api = quick
    review = await env.review(depth='quick')
    chat = review['chat_id']
    assert review['end_state'] == 'complete', review['summary']['end']
    assert review['profile']['name'] == 'muse_api' and not review['profile']['static_tool_list']
    findings = env.store.list_findings(review['id'])
    assert [f['code'] for f in findings] == ['F-001']
    assert review['summary']['summary']['text'].startswith('One medium issue')
    assert env.store.latest_report_revision(review['id']) is not None
    # One model request per step, plus the closing reply.
    assert len(api.requests) == len(STEPS) + 1
    # The tool list followed the phase: it changed between requests, and the key travelled only as the bearer token.
    lists = [tuple(sorted(t['name'] for t in body['tools'])) for body in api.requests]
    assert len(set(lists)) >= 3
    assert all(h['authorization'] == f'Bearer {KEY}' for h in api.headers)
    # The thinking block from the first reply is replayed on every later request, unchanged.
    first = api.requests[1]['messages'][1]['content'][0]
    assert first == {'type': 'thinking', 'thinking': 'Plan: read the board, brief, list candidates.', 'signature': 'sig-quick'}
    assert all(body['messages'][1]['content'][0] == first for body in api.requests[1:])
    # view_board's picture went back inside its tool result.
    shots = [b for body in api.requests for m in body['messages'] if m['role'] == 'user' and isinstance(m['content'], list)
             for b in m['content'] if b.get('type') == 'tool_result' and isinstance(b['content'], list)]
    assert shots and shots[0]['content'][1]['source']['data'] == PIXEL
    # Events: usage once per response, thoughts from the model's text, trail lines on tool starts.
    usages = env.events(chat, 'usage')
    assert len(usages) == len(api.requests) and all(u['payload']['cache_read_input_tokens'] == 400 for u in usages[:-1])
    assert env.events(chat, 'thought') and env.events(chat, 'session')
    assert all(s['payload'].get('trail') for s in env.events(chat, 'tool_start'))
    types = env.names(chat)
    assert types.count('phase_changed') >= 4 and 'report_ready' in types and 'completed' in types
    assert not any(e['payload'].get('is_error') for e in env.events(chat, 'tool_result') if e['payload']['name'] not in ('dismiss_rule_group',)), \
        [e['payload']['content'][:200] for e in env.events(chat, 'tool_result') if e['payload'].get('is_error')]
    # The token counts reached the review's budget.
    assert any('input_tokens' in json.dumps(e['payload']) for e in env.events(chat, 'budget'))
    assert KEY not in json.dumps([e['payload'] for e in env.events(chat)])


async def test_review_stopped_by_a_failing_model_still_reports(quick):
    env, api = quick
    api.script.extend([Msg([text('Starting.')], 'end_turn')])
    from fake_muse import Err
    api.script[:] = [Err(401, 'bad key', 'authentication_error')]
    review = await env.review(depth='quick')
    assert review['end_state'] == 'agent_stopped' and review['summary']['end']['reason'] == 'agent_error'
    assert 'Muse API error 401' in review['summary']['end']['detail'] and KEY not in json.dumps(review['summary'])
