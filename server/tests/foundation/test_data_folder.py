"""One backend per data folder: a second app on the same folder is refused and can never stop the first one's solver or touch its running work."""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.foundation.conftest import HOST, TOKEN
from trustpcb.api import create_app
from trustpcb.datalock import LOCK_NAME, DataFolderInUse, DataLock, holds, read_holder
from trustpcb.rf.queue import SimQueue
from trustpcb.storage import Store

FAKE = Path(__file__).resolve().parents[1] / 'rf_model' / 'fake_docker.py'
HOLDER = '''
import sys, time
from trustpcb.datalock import DataLock
DataLock(sys.argv[1], port=8118).acquire()
print("held", flush=True)
time.sleep(60)
'''


def test_lock_records_pid_and_start_and_refuses_a_second_owner(tmp_path):
    first = DataLock(tmp_path, port=8118).acquire()
    info = read_holder(tmp_path)
    assert info['pid'] == os.getpid() and info['port'] == 8118 and info['started'] and holds(tmp_path)
    with pytest.raises(DataFolderInUse) as refused:
        DataLock(tmp_path).acquire()
    assert str(os.getpid()) in str(refused.value) and 'TRUSTPCB_DATA_DIR' in str(refused.value) and 'already using' in str(refused.value)
    assert first.acquire() is first                      # taking it again in the owner is a no-op
    first.release()
    assert not holds(tmp_path) and read_holder(tmp_path) is None
    DataLock(tmp_path).acquire().release()               # free again


def test_a_lock_left_by_a_dead_process_is_stale_and_taken_over(tmp_path):
    """A holder in another process; killing it (no clean release) leaves the file behind. The next start takes it over and says so."""
    child = subprocess.Popen([sys.executable, '-c', HOLDER, str(tmp_path)], stdout=subprocess.PIPE, text=True,
                             env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[2])})
    try:
        assert child.stdout.readline().strip() == 'held'
        with pytest.raises(DataFolderInUse) as refused:
            DataLock(tmp_path).acquire()
        assert str(child.pid) in str(refused.value)      # the message names the process that holds it
        assert not holds(tmp_path)
        os.kill(child.pid, signal.SIGKILL)               # dies without releasing
        child.wait(10)
    finally:
        if child.poll() is None:
            os.kill(child.pid, signal.SIGKILL)
    assert read_holder(tmp_path)['pid'] == child.pid     # the file is still there: stale
    taken = DataLock(tmp_path).acquire()
    assert taken.took_over['pid'] == child.pid and read_holder(tmp_path)['pid'] == os.getpid()
    taken.release()


@pytest.fixture
def docker_env(tmp_path, monkeypatch):
    state = tmp_path / 'state'
    state.mkdir()
    (state / 'orphans').write_text('trustpcb-ems-firstapp\n')       # a solver container of the folder, as the first app's would be
    monkeypatch.setenv('FAKE_DOCKER_STATE', str(state))
    wrapper = tmp_path / 'docker'
    wrapper.write_text(f'#!/bin/sh\nexec {sys.executable} {FAKE} "$@"\n')
    wrapper.chmod(0o755)
    return state, str(wrapper)


async def test_the_container_sweep_and_job_recovery_act_only_for_the_owner(tmp_path, docker_env):
    state, docker = docker_env
    store = Store(tmp_path / 'data')
    running = store.create_sim_job('h1', status='running')
    queue = SimQueue(store, docker=docker)
    assert await queue.kill_orphans() == 0 and not list(state.glob('kill-*')) and not (state / 'ps_args.json').exists()   # no lock: docker is not even asked
    assert queue.recover_interrupted() == 0 and store.get_sim_job(running['id'])['status'] == 'running'
    lock = DataLock(store.data_dir).acquire()
    assert await queue.kill_orphans() == 1 and (state / 'kill-trustpcb-ems-firstapp').exists()
    assert queue.recover_interrupted() == 1 and store.get_sim_job(running['id'])['status'] == 'interrupted'
    lock.release()


def test_a_second_app_on_the_same_folder_is_refused_and_leaves_the_first_one_alone(tmp_path, docker_env, monkeypatch):
    state, docker = docker_env
    monkeypatch.setenv('PATH', f'{Path(docker).parent}:' + os.environ['PATH'])        # `docker` is the stand-in for both apps
    data = tmp_path / 'data'
    first_app = create_app(data_dir=data, token=TOKEN)
    with TestClient(first_app, base_url=HOST, headers={'X-TrustPCB-Token': TOKEN}) as first:
        store = first.app.state.store
        project = store.create_project('p')
        review = store.create_review(store.create_version(project['id'])['id'])
        turn = store.create_turn(review['chat_id'], 'r1')             # a run of the first app, still going
        store.update_review(review['id'], status='running')
        assert (state / 'kill-trustpcb-ems-firstapp').exists()          # the owner swept leftovers at its own start
        (state / 'kill-trustpcb-ems-firstapp').unlink()
        (state / 'ps_args.json').unlink(missing_ok=True)
        kills_before = set(state.glob('kill-*'))
        with pytest.raises(DataFolderInUse):
            with TestClient(create_app(data_dir=data, token=TOKEN), base_url=HOST):
                pass
        assert set(state.glob('kill-*')) == kills_before and not (state / 'ps_args.json').exists()   # the refused app asked docker for nothing and stopped nothing
        assert store.get_turn(turn['id'])['status'] == 'running' and store.get_review(review['id'])['status'] == 'running'
        assert first.get('/api/health').status_code == 200 and holds(data)
        assert not [e for e in store.events(review['chat_id']) if e['type'] == 'failed']
        # the read-only demo of the same folder takes no lock and changes nothing
        monkeypatch.setenv('TRUSTPCB_DEMO_READONLY', '1')
        with TestClient(create_app(data_dir=data, token=TOKEN), base_url=HOST, headers={'X-TrustPCB-Token': TOKEN}) as demo:
            assert demo.get('/api/projects').status_code == 200
        monkeypatch.delenv('TRUSTPCB_DEMO_READONLY')
        assert store.get_turn(turn['id'])['status'] == 'running' and holds(data)
    assert not holds(data)
    # the first app is gone without finishing the run: the next owner marks it interrupted, once, and sweeps the folder's containers
    with TestClient(create_app(data_dir=data, token=TOKEN), base_url=HOST, headers={'X-TrustPCB-Token': TOKEN}):
        assert store.get_turn(turn['id'])['status'] == 'interrupted' and store.get_review(review['id'])['status'] == 'interrupted'
        assert (state / 'kill-trustpcb-ems-firstapp').exists()
