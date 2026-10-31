"""scripts/seed-demo.sh: a demo stands for Muse work unless the caller says it is private."""
import json
import sqlite3
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[3] / 'scripts' / 'seed-demo.sh'


def data_dir(path: Path, *runners: str, status: str = 'complete') -> Path:
    path.mkdir()
    db = sqlite3.connect(path / 'trustpcb.sqlite3')
    db.execute('CREATE TABLE reviews (id TEXT, status TEXT, profile TEXT)')
    for index, runner in enumerate(runners):
        db.execute('INSERT INTO reviews VALUES (?, ?, ?)', (str(index), status, json.dumps({'runner': runner})))
    db.commit()
    db.close()
    return path


def seed(*args) -> subprocess.CompletedProcess:
    return subprocess.run(['bash', str(SCRIPT), *map(str, args)], capture_output=True, text=True, timeout=30)


def test_muse_reviews_are_seeded(tmp_path):
    source, target = data_dir(tmp_path / 'source', 'muse_api', 'muse_api'), tmp_path / 'target'
    done = seed(source, target)
    assert done.returncode == 0, done.stderr
    assert '2 review(s) found' in done.stdout and (target / 'trustpcb.sqlite3').exists()


def test_a_review_from_another_runner_is_refused(tmp_path):
    source, target = data_dir(tmp_path / 'source', 'muse_api', 'scripted'), tmp_path / 'target'
    done = seed(source, target)
    assert done.returncode != 0 and '1 review(s) did not run on Muse' in done.stderr
    assert not (target / 'trustpcb.sqlite3').exists()


def test_a_review_without_a_recorded_runner_is_refused(tmp_path):
    source = data_dir(tmp_path / 'source')
    db = sqlite3.connect(source / 'trustpcb.sqlite3')
    db.execute("INSERT INTO reviews VALUES ('0', 'complete', NULL)")
    db.commit()
    db.close()
    assert seed(source, tmp_path / 'target').returncode != 0


def test_a_private_demo_may_allow_any_runner(tmp_path):
    source, target = data_dir(tmp_path / 'source', 'scripted'), tmp_path / 'target'
    done = seed('--allow-any-runner', source, target)
    assert done.returncode == 0, done.stderr
    assert (target / 'trustpcb.sqlite3').exists()


def test_a_running_review_is_still_refused_first(tmp_path):
    source = data_dir(tmp_path / 'source', 'muse_api', status='running')
    done = seed('--allow-any-runner', source, tmp_path / 'target')
    assert done.returncode != 0 and 'still running' in done.stderr
