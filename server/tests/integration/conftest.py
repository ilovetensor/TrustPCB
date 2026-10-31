"""Shared setup for the integration tests: the real nRF sample board and a real SimQueue over a fake docker."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))     # the helpers module next to this file

from helpers import FAKE_DOCKER, make_version  # noqa: E402
from trustpcb.review import service  # noqa: E402
from trustpcb.review.lab_real import RealLab  # noqa: E402
from trustpcb.rf.queue import SimQueue  # noqa: E402
from trustpcb.storage import Store  # noqa: E402


@pytest.fixture
def fake_docker(tmp_path, monkeypatch):
    state = tmp_path / 'docker-state'
    state.mkdir()
    monkeypatch.setenv('FAKE_DOCKER_STATE', str(state))
    wrapper = tmp_path / 'docker'
    wrapper.write_text(f'#!/bin/sh\nexec {sys.executable} {FAKE_DOCKER} "$@"\n')
    wrapper.chmod(0o755)
    return wrapper, state


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / 'data')


@pytest.fixture
def version(store):
    return make_version(store)


@pytest.fixture
async def lab(store, version, fake_docker):
    wrapper, _ = fake_docker
    real = RealLab(store, queue=SimQueue(store, docker=str(wrapper), poll_s=0.1, timeout_s=30))

    async def fine():            # the stand-in docker answers `image inspect` like any other call: pretend the image exists
        return None
    real.solver_problem = fine
    yield real
    await real.queue.close()


@pytest.fixture(autouse=True)
def reset_service():
    yield
    service.configure()
