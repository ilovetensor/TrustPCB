"""Optional service keys (Meta Muse, DigiKey, Mouser) kept on this machine.

A key comes from the environment first and from `<data>/secrets.json` second. The file is written with mode 0600, is
never copied by the demo seed script and is listed in `.gitignore`. A value goes in and is read back only by code inside the
server that calls that service (a model key goes only to its model provider, a supplier key only to its supplier). Nothing
here returns a value to the web app, writes one to a log or an event, or puts one into a model prompt, a report, a solver container, kicad-cli or the PDF browser (those programs get an allow-listed environment,
see `pcb/env.py`).

    get_secret(name)        the value, or None. Only for the supplier clients.
    set_secret(name, value) store a value in the app (the environment still wins when it is set)
    clear_secret(name)      remove the stored value; an environment value is not touched
    secret_status()         {name: {'set': bool, 'source': 'env' | 'app' | None}}
"""
from __future__ import annotations

import json
import logging
import os
import stat
import tempfile
import threading
from pathlib import Path

logger = logging.getLogger('trustpcb')

SECRETS_FILE = 'secrets.json'
REPO_ROOT = Path(__file__).resolve().parents[2]
MAX_VALUE_CHARS = 512

# name -> what the web app shows. The names are also the environment variables.
KEYS: dict[str, dict[str, str]] = {
    'META_API_KEY': {'service': 'Meta', 'label': 'Meta API key (Muse Spark on api.meta.ai)', 'url': 'https://developer.meta.ai/'},
    'BASETEN_API_KEY': {'service': 'Baseten', 'label': 'Baseten API key (Muse Spark Open)', 'url': 'https://app.baseten.co/settings/api_keys'},
    'DIGIKEY_CLIENT_ID': {'service': 'DigiKey', 'label': 'DigiKey client ID', 'url': 'https://developer.digikey.com/'},
    'DIGIKEY_CLIENT_SECRET': {'service': 'DigiKey', 'label': 'DigiKey client secret', 'url': 'https://developer.digikey.com/'},
    'MOUSER_API_KEY': {'service': 'Mouser', 'label': 'Mouser search API key', 'url': 'https://www.mouser.com/api-hub/'},
}

_lock = threading.RLock()
_data_dir: Path | None = None


class UnknownKey(ValueError):
    """The name is not one of the keys TrustPCB knows."""


def configure(data_dir: Path | str | None) -> None:
    """Point the store at a data folder. The server does this at start; with no call the folder is TRUSTPCB_DATA_DIR or .trustpcb."""
    global _data_dir
    with _lock:
        _data_dir = Path(data_dir).expanduser().resolve() if data_dir else None


def data_dir() -> Path:
    if _data_dir is not None:
        return _data_dir
    configured = os.getenv('TRUSTPCB_DATA_DIR')
    return (Path(configured).expanduser() if configured else REPO_ROOT / '.trustpcb').resolve()


def secrets_path() -> Path:
    return data_dir() / SECRETS_FILE


def _known(name: str) -> str:
    if name not in KEYS:
        raise UnknownKey(f'Unknown key name. Known names: {", ".join(KEYS)}.')
    return name


def _read_file() -> dict[str, str]:
    path = secrets_path()
    try:
        info = path.stat()
        if info.st_mode & 0o077:                    # somebody loosened it: close it again before reading
            path.chmod(0o600)
        loaded = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        logger.warning('The stored keys file could not be read, so no stored key is used.')
        return {}
    if not isinstance(loaded, dict):
        return {}
    return {name: value for name, value in loaded.items() if name in KEYS and isinstance(value, str) and value.strip()}


def _write_file(values: dict[str, str]) -> None:
    path = secrets_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not values:
        path.unlink(missing_ok=True)
        return
    handle, temp = tempfile.mkstemp(prefix='.secrets-', suffix='.tmp', dir=path.parent)   # mkstemp creates the file as 0600
    try:
        with os.fdopen(handle, 'w', encoding='utf-8') as out:
            json.dump(values, out)
        os.chmod(temp, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def _env_value(name: str) -> str | None:
    value = (os.environ.get(name) or '').strip()
    return value or None


def get_secret(name: str) -> str | None:
    """The key's value (environment first, then the app's file), or None. Never pass the result to a log, an event, a response or a model."""
    _known(name)
    value = _env_value(name)
    if value is not None:
        return value
    with _lock:
        return _read_file().get(name)


def set_secret(name: str, value: str) -> None:
    _known(name)
    cleaned = (value or '').strip()
    if not cleaned:
        raise ValueError('The value is empty.')
    if len(cleaned) > MAX_VALUE_CHARS or any(ord(ch) < 32 or ord(ch) == 127 for ch in cleaned):
        raise ValueError('The value is not a valid key.')
    with _lock:
        values = _read_file()
        values[name] = cleaned
        _write_file(values)


def clear_secret(name: str) -> None:
    _known(name)
    with _lock:
        values = _read_file()
        if name in values:
            del values[name]
            _write_file(values)


def secret_status() -> dict[str, dict]:
    with _lock:
        stored = _read_file()
    status = {}
    for name in KEYS:
        if _env_value(name) is not None:
            status[name] = {'set': True, 'source': 'env'}
        elif name in stored:
            status[name] = {'set': True, 'source': 'app'}
        else:
            status[name] = {'set': False, 'source': None}
    return status
