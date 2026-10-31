"""Shared setup: a fake Messages API, a real ToolRegistry whose tools change with a phase, and a runner wired to both."""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))     # helper modules next to the tests

from fake_muse import KEY, PIXEL, FakeMuse, Sink
from trustpcb.review.tools import ToolDef, ToolRegistry, ToolResult, ok
from trustpcb.runners.base import RunRequest
from trustpcb.runners.muse import MuseRunner


@pytest.fixture(autouse=True)
def muse_env(monkeypatch):
    monkeypatch.setenv('MODEL_API_KEY', KEY)
    for name in ('TRUSTPCB_MUSE_EFFORT', 'TRUSTPCB_MUSE_IMAGE_MODE', 'TRUSTPCB_MUSE_STREAM', 'TRUSTPCB_MUSE_TIMEOUT_S'):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(params=['stream', 'plain'])
def mode(request, monkeypatch):
    monkeypatch.setenv('TRUSTPCB_MUSE_STREAM', '1' if request.param == 'stream' else '0')
    return request.param


@pytest.fixture
def api():
    return FakeMuse()


@pytest.fixture
def sink():
    return Sink()


class Gate:
    def __init__(self):
        self.phase = 1

    def allowed_names(self):
        return [n for n, phases in self.names.items() if self.phase in phases]

    def phase_label(self):
        return f'phase {self.phase}'

    async def before(self, tool, args):
        return None

    async def after(self, tool, args, result):
        return None


class World:
    """A registry with phase-dependent tools: look (1,2), advance (1,2), wrap_up (2), snap (image, 1,2)."""

    def __init__(self):
        self.gate = Gate()
        self.calls = []
        self.slow = None

        async def look(args):
            self.calls.append(('look', args))
            return ok({'seen': args.get('what', 'nothing')})

        async def advance(args):
            self.calls.append(('advance', args))
            self.gate.phase += 1
            return ok({'phase': self.gate.phase})

        async def wrap_up(args):
            self.calls.append(('wrap_up', args))
            return ok({'done': True})

        async def snap(args):
            self.calls.append(('snap', args))
            return ToolResult(text='{"caption":"view"}', images=[{'mime': 'image/png', 'data_b64': PIXEL}])

        async def boom(args):
            raise RuntimeError(f'exploded with {KEY}')

        async def slow(args):
            self.calls.append(('slow', args))
            if self.slow is not None:
                await self.slow()
            else:
                await asyncio.Event().wait()
            return ok({'slept': True})

        schema = {'type': 'object', 'properties': {'what': {'type': 'string'}}, 'required': [], 'additionalProperties': False}
        defs = [ToolDef('look', 'Look at something.', schema, look, phases=frozenset({1, 2})),
                ToolDef('advance', 'Move to the next phase.', {'type': 'object', 'properties': {}, 'required': [], 'additionalProperties': False}, advance, phases=frozenset({1, 2})),
                ToolDef('wrap_up', 'Finish.', {'type': 'object', 'properties': {}, 'required': [], 'additionalProperties': False}, wrap_up, phases=frozenset({2})),
                ToolDef('snap', 'Take a picture.', schema, snap, phases=frozenset({1, 2}), image=True),
                ToolDef('boom', 'Always fails.', schema, boom, phases=frozenset({1, 2})),
                ToolDef('slow', 'Waits.', {'type': 'object', 'properties': {}, 'required': [], 'additionalProperties': True}, slow, phases=frozenset({1, 2}))]
        self.gate.names = {d.name: d.phases for d in defs}
        self.registry = ToolRegistry(defs, self.gate)


@pytest.fixture
def world():
    return World()


@pytest.fixture
def delays():
    return []


@pytest.fixture
def make_runner(api, tmp_path, delays):
    def make(**kwargs):
        async def sleep(seconds):
            delays.append(seconds)
            await asyncio.sleep(0)
        kwargs.setdefault('sleep', sleep)
        return MuseRunner(data_dir=tmp_path / 'data', transport=api.transport, **kwargs)
    return make


@pytest.fixture
def runner(make_runner):
    return make_runner()


@pytest.fixture
def ask(world):
    def make(text='Review the board.', session_id=None, max_turns=20, profile=None, tools=None):
        return RunRequest(session_id=session_id, system_prompt='You review boards.', user_text=text, tools=tools or world.registry,
                          max_turns=max_turns, profile=profile or {})
    return make


@pytest.fixture(autouse=True)
def image_mode_under_test(monkeypatch, request):
    """These tests drive the tool_result path and its fallback; test_default_image_mode checks the real default."""
    if 'default_image_mode' not in request.keywords:
        from trustpcb.runners import muse as muse_module
        monkeypatch.setattr(muse_module, 'DEFAULT_IMAGE_MODE', 'tool_result')
