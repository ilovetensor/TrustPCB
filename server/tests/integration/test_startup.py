"""App start-up wiring: the real lab is installed, unfinished jobs are marked interrupted, and nothing needs Docker to start."""
from fastapi.testclient import TestClient

from trustpcb.api import create_app
from trustpcb.review import service
from trustpcb.review.lab import BaselineLab
from trustpcb.review.lab_real import RealLab
from trustpcb.storage import Store

HOST = 'http://127.0.0.1:8118'


def client(tmp_path):
    return TestClient(create_app(data_dir=tmp_path / 'data', token='t'), base_url=HOST, headers={'X-TrustPCB-Token': 't'})


def test_the_real_lab_is_installed_for_the_life_of_the_app(tmp_path, monkeypatch):
    monkeypatch.setenv('TRUSTPCB_EMS_IMAGE', 'my-ems:9')
    assert service.CONFIG.lab is None
    with client(tmp_path) as c:
        assert c.get('/api/health').status_code == 200
        assert isinstance(service.CONFIG.lab, RealLab) and service.CONFIG.lab.image == 'my-ems:9'
        assert isinstance(service.get_lab(service.CONFIG.lab.store), RealLab)
    assert service.CONFIG.lab is None


def test_unfinished_jobs_are_marked_interrupted_at_start(tmp_path):
    store = Store(tmp_path / 'data')
    queued = store.create_sim_job('aaaa', status='queued')
    running = store.create_sim_job('bbbb', status='running')
    done = store.create_sim_job('cccc', status='done')
    with client(tmp_path):
        pass
    assert store.get_sim_job(queued['id'])['status'] == 'interrupted' and store.get_sim_job(running['id'])['status'] == 'interrupted'
    assert store.get_sim_job(done['id'])['status'] == 'done'
    assert 'restarted' in store.get_sim_job(running['id'])['error']


def test_an_installed_lab_is_kept_and_baseline_can_be_forced(tmp_path, monkeypatch):
    mine = BaselineLab(Store(tmp_path / 'other'))
    service.configure(lab=mine)
    with client(tmp_path):
        assert service.CONFIG.lab is mine
    service.configure()
    monkeypatch.setenv('TRUSTPCB_LAB', 'baseline')
    with client(tmp_path):
        assert service.CONFIG.lab is None
