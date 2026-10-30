import sqlite3

import pytest

from trustpcb.storage import Store

TABLES = {
    'projects', 'versions', 'artifacts', 'reviews', 'findings', 'finding_history', 'evidence', 'candidates', 'investigations',
    'suspects', 'sim_jobs', 'threads', 'assumptions', 'report_revisions', 'chats', 'turns', 'events',
}


def test_every_spec_table_exists(store):
    with sqlite3.connect(store.database) as db:
        names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert TABLES <= names


def test_reopening_the_store_keeps_data(tmp_path):
    first = Store(tmp_path / 'd')
    project = first.create_project('Keep me')
    assert Store(tmp_path / 'd').get_project(project['id'])['name'] == 'Keep me'


def test_versions_number_per_project(store):
    a, b = store.create_project('A'), store.create_project('B')
    numbers = [store.create_version(a['id'])['number'], store.create_version(a['id'])['number'], store.create_version(b['id'])['number']]
    assert numbers == [1, 2, 1]
    with pytest.raises(ValueError):
        store.create_version('missing')


def test_version_json_columns_roundtrip(store):
    project = store.create_project('A')
    version = store.create_version(project['id'], stackup={'source': 'default', 'layers': [1, 2]}, detection={'board': 'x.kicad_pcb'})
    updated = store.update_version(version['id'], status='ready', stackup={'source': 'board'})
    assert updated['status'] == 'ready' and updated['stackup'] == {'source': 'board'}
    assert updated['detection'] == {'board': 'x.kicad_pcb'}
    with pytest.raises(ValueError):
        store.update_version(version['id'], number=9)


def test_artifacts_stay_inside_the_data_folder(store, seeded, tmp_path):
    _, version, _ = seeded
    path = store.version_dir(version['id']) / 'board.svg'
    path.write_text('<svg/>')
    artifact = store.add_artifact('version', version['id'], 'layer_svg', path, mime='image/svg+xml', meta={'layer': 'F.Cu'})
    assert artifact['path'] == f"versions/{version['id']}/board.svg" and artifact['size'] == 6
    assert store.artifact_path(artifact) == path.resolve()
    assert store.list_artifacts('version', version['id'], 'layer_svg')[0]['meta'] == {'layer': 'F.Cu'}
    outside = tmp_path / 'outside.txt'
    outside.write_text('x')
    with pytest.raises(ValueError):
        store.add_artifact('version', version['id'], 'bad', outside)
    with pytest.raises(ValueError):
        store.add_artifact('version', version['id'], 'bad', '../outside.txt')
    escaped = {**artifact, 'path': '../outside.txt'}
    with pytest.raises(ValueError):
        store.artifact_path(escaped)


def test_review_creates_its_event_chat_and_events_replay(store, seeded):
    _, _, review = seeded
    chat = store.get_chat(review['chat_id'])
    assert chat['kind'] == 'review' and review['status'] == 'queued'
    first = store.append_event(chat['id'], None, 'phase_changed', {'phase': 'intake'})
    second = store.append_event(chat['id'], None, 'budget', {'runs_left': 3})
    assert second['sequence'] > first['sequence']
    assert [e['type'] for e in store.events(chat['id'])] == ['phase_changed', 'budget']
    assert [e['type'] for e in store.events(chat['id'], first['sequence'])] == ['budget']
    assert first['project_id'] == chat['project_id'] and first['payload'] == {'phase': 'intake'}
    assert set(first) == {'id', 'sequence', 'project_id', 'chat_id', 'turn_id', 'type', 'payload', 'created_at'}


def test_threads_have_their_own_event_log(store, seeded):
    _, _, review = seeded
    thread = store.create_thread(review['id'], 'check')
    assert store.get_chat(thread['chat_id'])['kind'] == 'thread' and thread['level'] == 'check'
    store.append_event(thread['chat_id'], None, 'followup_started', {'level': 'check'})
    store.append_event(review['chat_id'], None, 'phase_changed', {})
    assert [e['type'] for e in store.events(thread['chat_id'])] == ['followup_started']
    assert [t['id'] for t in store.list_threads(review['id'])] == [thread['id']]
    assert store.add_thread_tokens(thread['id'], 1200)['tokens_used'] == 1200
    assert store.get_thread_by_chat(thread['chat_id'])['id'] == thread['id']


def test_findings_codes_history_and_counts(store, seeded):
    _, _, review = seeded
    one = store.create_finding(review['id'], 'track_detour|net:VDD|zone:power', 'Detour', 'medium', 'measured', what='long', marks=[{'type': 'path', 'target': 'net:VDD'}])
    two = store.create_finding(review['id'], 'cap_far|ref:C1', 'Far cap', 'high', 'measured')
    assert (one['code'], two['code']) == ('F-001', 'F-002')
    assert one['marks'] == [{'type': 'path', 'target': 'net:VDD'}] and one['class'] == 'measured' and one['needs_recheck'] == 0
    assert store.find_finding(review['id'], 'cap_far|ref:C1')['id'] == two['id']
    assert store.get_finding_by_code(review['id'], 'F-001')['id'] == one['id']
    changed = store.update_finding(one['id'], 'followup', 'new evidence', severity='high', class_='simulated', evidence_ids=['x'])
    assert changed['severity'] == 'high' and changed['class'] == 'simulated'
    history = store.finding_history(one['id'])
    assert [h['source'] for h in history] == ['review', 'followup']
    assert history[0]['before'] is None and history[1]['before']['severity'] == 'medium' and history[1]['after']['severity'] == 'high'
    assert store.severity_counts(review['id']) == {'critical': 0, 'high': 2, 'medium': 0, 'low': 0, 'info': 0}
    store.update_finding(two['id'], 'user', 'not relevant', status='dismissed', dismissed_reason='not relevant')
    assert store.severity_counts(review['id'])['high'] == 1
    assert store.severity_counts(review['id'], open_only=False)['high'] == 2
    with pytest.raises(ValueError):
        store.create_finding(review['id'], 'f', 't', 'catastrophic')
    with pytest.raises(ValueError):
        store.update_finding(one['id'], 'x', None, code='F-999')


def test_evidence_candidates_investigations_suspects(store, seeded):
    _, _, review = seeded
    evidence = store.add_evidence(review['id'], 'measurement', 'measure', {'gap': {'value': 0.31, 'unit': 'mm'}}, artifact_ids=['a'])
    assert evidence['code'] == 'E-001' and evidence['values']['gap']['value'] == 0.31
    assert store.list_evidence(review['id'])[0]['artifact_ids'] == ['a']
    candidate = {'id': 'candidate:track_detour:1', 'rule': 'track_detour', 'title': 'Detour', 'severity_hint': 'low'}
    stored = store.add_candidate(review['id'], candidate)
    assert stored['review_state'] == 'new' and stored['data'] == candidate
    store.set_candidate_state(review['id'], candidate['id'], 'reviewed')
    assert [c['candidate_id'] for c in store.list_candidates(review['id'], 'reviewed')] == [candidate['id']]
    investigation = store.create_investigation(review['id'], 'Does the keepout copper detune the antenna?')
    first, second = store.add_suspect(investigation['id'], 'GND pour'), store.add_suspect(investigation['id'], 'Ground via')
    assert (first['rank'], second['rank']) == (1, 2)
    store.update_suspect(first['id'], status='ruled_out', conclusion='no shift')
    assert store.list_suspects(investigation['id'])[0]['conclusion'] == 'no shift'
    done = store.update_investigation(investigation['id'], status='confirmed', outcome={'finding': 'F-001'})
    assert done['outcome'] == {'finding': 'F-001'}


def test_sim_jobs_find_by_hash(store, seeded):
    _, _, review = seeded
    job = store.create_sim_job('abc123', review_id=review['id'], scenario={'quality': 'draft'})
    assert job['status'] == 'queued' and job['progress'] == 0.0 and job['scenario'] == {'quality': 'draft'}
    store.update_sim_job(job['id'], status='done', progress=1.0, trust='ok', result={'f0_hz': 2.44e9})
    found = store.find_sim_job('abc123', review['id'])
    assert found['trust'] == 'ok' and found['result']['f0_hz'] == 2.44e9
    assert store.find_sim_job('nope') is None
    assert [j['id'] for j in store.list_sim_jobs(status='done')] == [job['id']]


def test_assumption_sources_are_checked(store, seeded):
    _, _, review = seeded
    store.add_assumption(review['id'], 'substrate', {'name': 'FR-4', 'epsilon_r': 4.4}, 'default')
    user = store.add_assumption(review['id'], 'substrate', {'name': 'RO4350B'}, 'user_provided', dependent_evidence_ids=['E-001'])
    assert [a['source'] for a in store.list_assumptions(review['id'])] == ['default', 'user_provided']
    assert store.update_assumption(user['id'], value={'name': 'FR-4'})['value'] == {'name': 'FR-4'}
    with pytest.raises(ValueError):
        store.add_assumption(review['id'], 'x', 1, 'guess')
    with pytest.raises(sqlite3.IntegrityError):
        with store._db() as db:
            db.execute("UPDATE assumptions SET source='guess' WHERE id=?", (user['id'],))


def test_report_revisions_number_up_and_listing_omits_snapshots(store, seeded):
    _, _, review = seeded
    assert store.latest_report_revision(review['id']) is None
    first = store.add_report_revision(review['id'], 'review complete', {'findings': 2})
    second = store.add_report_revision(review['id'], 'follow-up', {'findings': 3})
    assert (first['number'], second['number']) == (1, 2)
    assert store.latest_report_revision(review['id'])['snapshot'] == {'findings': 3}
    assert store.get_report_revision(review['id'], 1)['snapshot'] == {'findings': 2}
    assert all('snapshot' not in row for row in store.list_report_revisions(review['id']))


def test_bind_profile_only_once(store, seeded):
    _, _, review = seeded
    chat = store.bind_chat_profile(review['chat_id'], {'runner': 'scripted', 'model': None, 'effort': None})
    again = store.bind_chat_profile(review['chat_id'], {'runner': 'other', 'model': 'x', 'effort': 'y'})
    assert chat['runner'] == again['runner'] == 'scripted'


def test_recover_interrupted_marks_turns_reviews_and_streams_a_failure(store, seeded):
    _, _, review = seeded
    store.update_review(review['id'], status='running')
    turn = store.create_turn(review['chat_id'], 'req-1', 'go')
    reopened = Store(store.data_dir)
    assert [t['id'] for t in reopened.recover_interrupted()] == [turn['id']]
    assert reopened.get_turn(turn['id'])['status'] == 'interrupted'
    assert reopened.get_chat(review['chat_id'])['status'] == 'interrupted'
    assert reopened.get_review(review['id'])['status'] == 'interrupted'
    last = reopened.events(review['chat_id'])[-1]
    assert last['type'] == 'failed' and last['payload']['interrupted'] is True
    assert reopened.recover_interrupted() == []


def test_duplicate_turn_request_is_rejected(store, seeded):
    _, _, review = seeded
    store.create_turn(review['chat_id'], 'same')
    with pytest.raises(ValueError):
        store.create_turn(review['chat_id'], 'same')
