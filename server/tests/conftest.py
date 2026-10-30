"""Shared test setup for every lane."""
import pytest


@pytest.fixture(autouse=True)
def deterministic_runner(monkeypatch):
    """Tests never reach a real model: the default runner is the scripted one."""
    monkeypatch.setenv('TRUSTPCB_RUNNER', 'scripted')
    monkeypatch.delenv('TRUSTPCB_SCRIPT', raising=False)
