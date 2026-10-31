"""Where a stored key must never end up: the repository, the demo copy, a child program, the model, a log."""
import json
import re
import sqlite3
import subprocess
from pathlib import Path

from support import FAKE_ID, FAKE_MOUSER, FAKE_SECRET

from trustpcb import settings_store
from trustpcb.pcb.env import child_environment
from trustpcb.rf.queue import docker_environment
from trustpcb.review.report_pdf import minimal_env

ROOT = Path(__file__).resolve().parents[3]
SERVER = ROOT / 'server' / 'trustpcb'
NAMES = list(settings_store.KEYS)


def test_the_secrets_file_and_env_files_are_git_ignored():
    ignore = (ROOT / '.gitignore').read_text().splitlines()
    assert 'secrets.json' in ignore and '.env' in ignore
    if (ROOT / '.git').exists():
        for path in ('secrets.json', '.trustpcb/secrets.json', 'anywhere/else/secrets.json', '.env'):
            assert subprocess.run(['git', 'check-ignore', '-q', path], cwd=ROOT).returncode == 0, path


def test_env_example_has_empty_values_only():
    for line in (ROOT / '.env.example').read_text().splitlines():
        for name in NAMES:
            if re.match(rf'#?\s*{name}\s*=', line):
                assert line.split('=', 1)[1].strip() == '', line


def seeded_source(path: Path, nested: bool = True) -> Path:
    path.mkdir()
    db = sqlite3.connect(path / 'trustpcb.sqlite3')
    db.execute('CREATE TABLE reviews (id TEXT, status TEXT, profile TEXT)')
    db.execute('INSERT INTO reviews VALUES (?, ?, ?)', ('1', 'complete', json.dumps({'runner': 'muse_api'})))
    db.commit()
    db.close()
    (path / 'secrets.json').write_text(json.dumps({'MOUSER_API_KEY': FAKE_MOUSER}))
    (path / '.secrets-abc.tmp').write_text(FAKE_SECRET)
    if nested:
        (path / 'parts').mkdir()
        (path / 'parts' / 'secrets.json').write_text(FAKE_ID)
        (path / 'parts' / 'note.txt').write_text('kept')
    return path


def test_the_demo_seed_script_never_copies_the_keys(tmp_path):
    source, target = seeded_source(tmp_path / 'source'), tmp_path / 'target'
    done = subprocess.run(['bash', str(ROOT / 'scripts' / 'seed-demo.sh'), str(source), str(target)], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    assert (target / 'parts' / 'note.txt').read_text() == 'kept'               # other files are copied
    copied = [p for p in target.rglob('*') if p.is_file()]
    assert not [p for p in copied if p.name == 'secrets.json' or p.name.startswith('.secrets-')]
    for path in copied:
        if path.suffix != '.sqlite3':
            assert FAKE_MOUSER not in path.read_text(errors='ignore') and FAKE_SECRET not in path.read_text(errors='ignore')
    assert (source / 'secrets.json').exists()                                    # the source is untouched


def test_child_programs_get_no_keys(keys, monkeypatch):
    monkeypatch.setenv('TRUSTPCB_TOKEN', 'launch-token')
    for environment in (child_environment(), docker_environment(), minimal_env()):
        assert not set(NAMES) & set(environment)
        assert not {FAKE_ID, FAKE_SECRET, FAKE_MOUSER} & set(environment.values())


def test_only_the_settings_store_the_parts_package_and_the_api_name_the_keys():
    allowed = {SERVER / 'settings_store.py', SERVER / 'api.py'}
    offenders = []
    for path in SERVER.rglob('*.py'):
        if SERVER / 'parts' in path.parents or path in allowed:
            continue
        if any(name in path.read_text(encoding='utf-8') for name in NAMES):
            offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, f'{offenders} read or name a supplier key; the model, solver, KiCad and browser code must not'


def test_get_secret_is_called_only_by_the_supplier_code():
    callers = []
    for path in SERVER.rglob('*.py'):
        if path.name == 'settings_store.py':
            continue
        if 'get_secret(' in path.read_text(encoding='utf-8'):
            callers.append(path.relative_to(SERVER).as_posix())
    assert set(callers) == {'parts/catalog.py'}, callers


def test_the_api_routes_never_call_get_secret():
    text = (SERVER / 'api.py').read_text(encoding='utf-8')
    assert 'get_secret' not in text
