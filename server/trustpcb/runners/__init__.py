"""Runner profiles and loading.

`local_agent` (development) is a git-ignored module, `muse_api` is the real-review runner, and
`scripted` is the deterministic default when nothing else exists. There is never an automatic
fallback from one real provider to another: asking for a runner that is missing is an error.
"""
from __future__ import annotations

import importlib
import importlib.util
import os

from .base import RunRequest, Runner

# Only generic facts live here. A local runner module may expose PROFILE = {...} to fill in
# its own model and effort; nothing in tracked code names it.
PROFILES = {
    'local_agent': {'runner': 'local_agent', 'model': None, 'effort': None, 'static_tool_list': True, 'description': 'Local development runner (git-ignored)'},
    'muse_api': {'runner': 'muse_api', 'model': 'muse-spark-1.3', 'effort': None, 'static_tool_list': False, 'description': 'Muse via the Messages API'},
    'scripted': {'runner': 'scripted', 'model': None, 'effort': None, 'description': 'Deterministic scripted runner; calls no model'},
}
__all__ = ['PROFILES', 'RunRequest', 'Runner', 'available', 'load_runner', 'resolve_profile']


def _module_for(name: str) -> str | None:
    return {'local_agent': 'trustpcb.runners.local_agent', 'muse_api': 'trustpcb.runners.muse'}.get(name)


def available(name: str) -> bool:
    if name == 'scripted':
        return True
    module = _module_for(name)
    return bool(module) and importlib.util.find_spec(module) is not None


def _import(name: str):
    return importlib.import_module(_module_for(name))


def resolve_profile(name: str | None = None) -> dict:
    """The profile dict a review or thread is bound to.

    No name: TRUSTPCB_RUNNER, else local_agent when its module exists and imports, else scripted.
    A local_agent that exists but cannot import (for example its extra requirements are not
    installed) resolves to scripted with a `note` saying why, so the UI shows what really runs.
    """
    name = name or os.environ.get('TRUSTPCB_RUNNER') or None
    explicit = name is not None
    if name is None:
        name = 'local_agent' if available('local_agent') else 'scripted'
    if name not in PROFILES:
        raise ValueError(f'Unsupported runner profile: {name}')
    if name == 'scripted':
        return {**PROFILES['scripted'], 'name': 'scripted'}
    if not available(name):
        raise RuntimeError(f'Runner profile {name} is not installed on this machine.')
    try:
        module = _import(name)
    except ImportError as exc:
        if explicit:
            raise RuntimeError(f'Runner profile {name} could not be imported: {exc}') from exc
        return {**PROFILES['scripted'], 'name': 'scripted', 'note': f'local_agent present but not importable ({exc}); using scripted'}
    return {**PROFILES[name], **getattr(module, 'PROFILE', {}), 'name': name}


def load_runner(profile: dict | str | None = None) -> Runner:
    """Instantiate the runner for a profile (dict from resolve_profile, a name, or None for the default)."""
    if isinstance(profile, dict):
        profile = profile.get('name') or profile.get('runner')
    profile = resolve_profile(profile)
    if profile['name'] == 'scripted':
        from .scripted import ScriptedRunner
        return ScriptedRunner()
    module = _import(profile['name'])
    return module.create_runner(profile)  # every runner module exposes create_runner(profile)
