import asyncio
import importlib.machinery
import sys
import types
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from trustpcb.api import create_app
from trustpcb.storage import Store

HOST = 'http://127.0.0.1:8118'
TOKEN = 'secret'


@dataclass
class FakeResult:
    text: str = ''
    images: list = field(default_factory=list)
    is_error: bool = False
    structured: dict | None = None


@dataclass
class FakeSpec:
    name: str
    description: str
    input_schema: dict
    handler: object


class FakeRegistry:
    def __init__(self, *specs):
        self._specs = list(specs)

    def specs(self):
        return self._specs


def fake_module(monkeypatch, name, **functions):
    """Install a stand-in for a module another lane will own."""
    module = types.ModuleType(name)
    module.__spec__ = importlib.machinery.ModuleSpec(name, None)
    for key, value in functions.items():
        setattr(module, key, value)
    monkeypatch.setitem(sys.modules, name, module)
    return module


def lane_present(module, function):
    import importlib
    import importlib.util
    try:
        if importlib.util.find_spec(module) is None:
            return False
    except ModuleNotFoundError:
        return False
    return hasattr(importlib.import_module(module), function)


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / 'data')


@pytest.fixture
def client(tmp_path):
    app = create_app(data_dir=tmp_path / 'data', token=TOKEN, poll=0.01, keepalive=0.05)
    with TestClient(app, base_url=HOST, headers={'X-TrustPCB-Token': TOKEN}) as client:
        yield client


@pytest.fixture
def seeded(store):
    """A project, version and review ready for findings."""
    project = store.create_project('Demo board')
    version = store.create_version(project['id'], label='first')
    review = store.create_review(version['id'], goal={'purpose': 'test'})
    return project, version, review


@pytest.fixture
def fakes(monkeypatch):
    """Helpers shared by the foundation tests (importlib mode cannot import siblings)."""
    return types.SimpleNamespace(
        Result=FakeResult, Spec=FakeSpec, Registry=FakeRegistry, lane_present=lane_present,
        module=lambda name, **functions: fake_module(monkeypatch, name, **functions),
    )
