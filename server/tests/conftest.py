"""Shared test setup."""
import pytest


@pytest.fixture(autouse=True)
def deterministic_runner(monkeypatch):
    """Tests never reach a real model: the default runner is the scripted one."""
    monkeypatch.setenv('TRUSTPCB_RUNNER', 'scripted')
    monkeypatch.delenv('TRUSTPCB_SCRIPT', raising=False)
    monkeypatch.setenv('TRUSTPCB_DOCKER_PASS_ENV', 'FAKE_DOCKER_STATE,FAKE_DOCKER_MODE')   # the stand-in docker client reads these
