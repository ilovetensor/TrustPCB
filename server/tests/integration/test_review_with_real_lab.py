"""A scripted review (no model) driving the real lab: with the stand-in docker, and with a cached real solver run."""
from __future__ import annotations

import json
import shutil

import pytest
from fastapi.testclient import TestClient

from helpers import GOAL, SIM_CACHE, STEPS, make_test_lab, run_scripted_review, tool_results
from trustpcb.api import create_app
from trustpcb.review.lab_real import RealLab
from trustpcb.rf.queue import SimQueue

HOST = 'http://127.0.0.1:8118'
NRF_BASE_HASH = '9ee1c94d3ed62c5e2eff'          # the nRF base scenario at screen quality (rf-model-2, band 2.4 to 2.4835 GHz)


def payload(result):
    return json.loads(result['content'])


async def test_scripted_review_runs_the_stand_in_solver_and_shows_the_agent_the_detail(store, version, lab, fake_docker):
    wrapper, _ = fake_docker
    queue = SimQueue(store, docker=str(wrapper), poll_s=0.1, timeout_s=30)
    review_lab = make_test_lab(RealLab, store, queue)
    review = await run_scripted_review(store, review_lab, STEPS)
    events = [e['type'] for e in store.events(review['chat_id'])]
    assert {'sim_queued', 'sim_progress', 'sim_done'} <= set(events)
    run = payload(tool_results(store, review, 'run_simulation')[0])
    assert run['ref'] == 'R1' and run['trust']['status'] and run['compare_only'] and 'Antenna alone' in run['rf_summary']
    assert any(k.startswith('antenna_only') for k in run['rf_detail']) and run['measurements']['antenna_only_band_worst_s11']['unit'] == 'dB'
    done = [e['payload'] for e in store.events(review['chat_id']) if e['type'] == 'sim_done'][0]
    assert done['curve']['freq_hz'] and done['quality'] == 'screen'
    views = tool_results(store, review, 'view_result')
    assert len(views) == 2 and not any(v.get('is_error') for v in views)
    await queue.close()


@pytest.mark.slow
async def test_cached_solver_run_through_a_scripted_review_and_the_api(tmp_path, store, version, fake_docker):
    source = SIM_CACHE / NRF_BASE_HASH
    if not (source / 'summary.json').is_file():
        pytest.skip(f'no cached solver output at {source}; copy a finished nRF base run there (see docs/specs/contracts.md) or set TRUSTPCB_SIM_CACHE')
    shutil.copytree(source, store.scenario_dir(NRF_BASE_HASH), dirs_exist_ok=True)
    wrapper, state = fake_docker                       # any attempt to start the solver would be seen here
    queue = SimQueue(store, docker=str(wrapper), poll_s=0.1, timeout_s=30)
    lab = make_test_lab(RealLab, store, queue)
    review = await run_scripted_review(store, lab, STEPS)
    assert queue.stats == {'submitted': 1, 'cache_hits': 1, 'ran': 0} and not (state / 'running').exists(), 'the cached scenario was not found: the model or the board changed'

    run = payload(tool_results(store, review, 'run_simulation')[0])
    assert run['trust']['status'] == 'ok' and run['cached'] is False and run['quality'] == 'screen' and 'Do not quote' in run['compare_only']
    detail = run['rf_detail']
    assert [round(d['frequency_mhz']) for d in detail['s11_dips']] == [1866, 1986, 2185, 2425, 2641]     # every dip, not only the deepest
    assert detail['first_series_resonance_mhz'] == pytest.approx(1673.5, abs=0.1)
    assert detail['antenna_only_band_worst_db'] == pytest.approx(-2.18, abs=0.01) and detail['as_built_band_worst_db'] == pytest.approx(-1.32, abs=0.01) and detail['basis'] == 'as_built'
    assert run['meets_target'] is False and run['measurements']['s11_min']['value'] == pytest.approx(-16.83, abs=0.01)
    assert 'Antenna alone' in run['rf_summary'] and 'As built' in run['rf_summary']
    views = tool_results(store, review, 'view_result')
    assert len(views) == 2 and not any(v.get('is_error') for v in views)
    tuned = payload(tool_results(store, review, 'tune_matching')[0])
    assert tuned['components'] or tuned.get('predicted')

    with TestClient(create_app(data_dir=store.data_dir, token='t'), base_url=HOST, headers={'X-TrustPCB-Token': 't'}) as client:
        sims = client.get(f"/api/reviews/{review['id']}/sims").json()
        assert len(sims) == 1
        sim = sims[0]
        assert sim['label'] == 'base' and sim['kind'] == 'base' and sim['quality'] == 'screen' and sim['trust']['status'] == 'ok' and sim['whatif_ops'] == []
        assert sim['evidence'] and sim['measurements']['s11_min']['unit'] == 'dB' and {'s11.json', 'field.json', 'meta.json', 'summary.json'} <= set(sim['files'])
        for name in sim['files']:
            got = client.get(f"/api/sim/{sim['job_id']}/files/{name}")
            assert got.status_code == 200, name
        meta = client.get(f"/api/sim/{sim['job_id']}/files/meta.json").json()
        assert meta['y_convention']['scenario_y'] == '-board_y'
        assert len(client.get(f"/api/sim/{sim['job_id']}/files/field_xy.bin").content) == 2 * 207 * 257 * 4
        assert client.get(f"/api/sim/{sim['job_id']}/files/scenario.json").status_code == 404      # not on the list
        assert client.get(f"/api/sim/{sim['job_id']}/files/..%2F..%2Ftrustpcb.sqlite3").status_code in (404, 422)
        assert client.get('/api/sim/nope/files/s11.json').status_code == 404
