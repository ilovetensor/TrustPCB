"""Runner profiles and loading.

`muse_api` is the real-review runner and `scripted` is the deterministic default when nothing else is
configured. There is never an automatic fallback from one real provider to another: asking for a
runner that is missing is an error.

Extension point: set `TRUSTPCB_RUNNER_MODULE` to the dotted name of a Python module and that module
becomes an additional runner. The module must expose `create_runner(profile) -> Runner` and may expose
`PROFILE = {...}` to set its `model`, `effort`, `description` or `static_tool_list`. The profile is
named after the last part of the module path, and it is the default runner when `TRUSTPCB_RUNNER` is
not set. Reviews remember the profile name they started with, so keep the module name stable.
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import re

from .base import RunRequest, Runner

EXTENSION_VARIABLE = 'TRUSTPCB_RUNNER_MODULE'
_MODULE_PATH = re.compile(r'^[A-Za-z_]\w*(\.[A-Za-z_]\w*)*$')

PROFILES = {
    'muse_api': {'runner': 'muse_api', 'model': 'muse-spark-1.3', 'effort': None, 'static_tool_list': False, 'description': 'Muse via the Messages API'},
    'scripted': {'runner': 'scripted', 'model': None, 'effort': None, 'description': 'Deterministic scripted runner; calls no model'},
}
__all__ = ['EXTENSION_VARIABLE', 'PROFILES', 'RunRequest', 'Runner', 'available', 'extension_profile_name', 'load_runner', 'profile_names', 'resolve_profile']


def _extension_module() -> str | None:
    """The module named by the environment, or None. A malformed value is an error, not silently ignored."""
    module = (os.environ.get(EXTENSION_VARIABLE) or '').strip()
    if not module:
        return None
    if not _MODULE_PATH.match(module):
        raise RuntimeError(f'{EXTENSION_VARIABLE} must be a dotted Python module name, got {module!r}.')
    return module


def extension_profile_name() -> str | None:
    """The profile name of the configured extension runner: the last part of its module path."""
    module = _extension_module()
    name = module.rsplit('.', 1)[-1] if module else None
    if name in PROFILES:
        raise RuntimeError(f'{EXTENSION_VARIABLE} cannot use the reserved runner name {name!r}.')
    return name


def profile_names() -> list[str]:
    """Every profile name this process knows: the built-in ones plus the configured extension, if any."""
    try:
        extension = extension_profile_name()
    except RuntimeError:
        extension = None
    return [*PROFILES, *([extension] if extension else [])]


def _module_for(name: str) -> str | None:
    if name == 'muse_api':
        return 'trustpcb.runners.muse'
    if name == extension_profile_name():
        return _extension_module()
    return None


def available(name: str) -> bool:
    if name == 'scripted':
        return True
    try:
        module = _module_for(name)
        return bool(module) and importlib.util.find_spec(module) is not None
    except (ImportError, ValueError, RuntimeError):  # a parent package that cannot be imported, or a bad setting
        return False


def _import(name: str):
    return importlib.import_module(_module_for(name))


def _muse_configured() -> bool:
    try:
        from .muse import configured
        return configured()
    except ImportError:
        return False


def resolve_profile(name: str | None = None) -> dict:
    """The profile dict a review or thread is bound to.

    No name: TRUSTPCB_RUNNER, else the extension runner when TRUSTPCB_RUNNER_MODULE is set, else muse_api when a
    Muse key is set (environment or Settings), else scripted.
    A configured extension that is missing or cannot be imported is an error that says why, never a
    silent switch to another runner.
    """
    name = name or os.environ.get('TRUSTPCB_RUNNER') or extension_profile_name() or ('muse_api' if _muse_configured() else 'scripted')
    extension = None if name in PROFILES else extension_profile_name()
    if name not in PROFILES and name != extension:
        raise ValueError(f'Unsupported runner profile: {name}')
    if name == 'scripted':
        return {**PROFILES['scripted'], 'name': 'scripted'}
    if not available(name):
        raise RuntimeError(f'Runner profile {name} is not installed on this machine.')
    try:
        module = _import(name)
    except ImportError as exc:
        raise RuntimeError(f'Runner profile {name} could not be imported: {exc}') from exc
    if name == extension:
        # An extension reads its tool list once per session unless its own PROFILE says otherwise.
        base = {'runner': name, 'model': None, 'effort': None, 'static_tool_list': True, 'description': f'Runner from {_extension_module()}'}
    else:
        base = PROFILES[name]
    return {**base, **getattr(module, 'PROFILE', {}), 'name': name}


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
