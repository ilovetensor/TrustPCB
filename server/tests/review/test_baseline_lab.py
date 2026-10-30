"""The PCB-lane lab on the real sample board: candidates from the analyzers and a board image."""
import shutil

import pytest

from trustpcb.review.lab import BaselineLab, LabUnavailable
from trustpcb.storage import Store


@pytest.fixture
def lab(tmp_path):
    store = Store(tmp_path / 'data')
    project = store.create_project('nRF')
    version = store.create_version(project['id'])
    shutil.copytree(__file__.rsplit('/server/', 1)[0] + '/samples/nrf52840-node', store.version_dir(version['id']) / 'project')
    return BaselineLab(store), version['id']


async def test_candidates_come_from_the_analyzers_and_resolve_on_the_board(lab):
    lab, version_id = lab
    candidates = await lab.list_candidates(version_id=version_id)
    assert candidates and {c['source'] for c in candidates} == {'analyzer'}
    board = await lab.load_board(version_id=version_id)
    assert all(board.resolve(c['id']) for c in candidates[:5])                  # candidate ids are valid targets
    rule = candidates[0]['rule']
    again = await lab.run_analyzer(version_id=version_id, rule=rule)
    assert again and all(c['rule'] == rule for c in again)


async def test_board_images_and_unavailable_rf(lab):
    lab, version_id = lab
    image = await lab.view_board(version_id=version_id, view='top', width=400)
    assert image['mime'] == 'image/png' and len(image['data_b64']) > 100
    status = await lab.rf_status(version_id=version_id)
    assert status['simulatable'] is False and 'not installed' in status['reason']
    with pytest.raises(LabUnavailable):
        await lab.run_scenario()
