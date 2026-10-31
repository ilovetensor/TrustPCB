import json
import logging

from support import TOKEN

from trustpcb import settings_store

VALUE = 'api-value-never-echoed-4242'


def test_status_lists_the_three_keys_without_values(api):
    body = api.get('/api/settings/keys').json()
    assert [row['name'] for row in body['keys']] == ['DIGIKEY_CLIENT_ID', 'DIGIKEY_CLIENT_SECRET', 'MOUSER_API_KEY']
    assert all(set(row) == {'name', 'service', 'label', 'get_url', 'set', 'source'} for row in body['keys'])
    assert body['parts_available'] is False and body['read_only'] is False


def test_put_stores_and_never_echoes(api, caplog):
    with caplog.at_level(logging.DEBUG):
        put = api.put('/api/settings/keys/MOUSER_API_KEY', json={'value': VALUE})
        listing = api.get('/api/settings/keys')
    assert put.status_code == 200
    assert VALUE not in put.text and VALUE not in listing.text and VALUE not in caplog.text
    row = next(r for r in listing.json()['keys'] if r['name'] == 'MOUSER_API_KEY')
    assert row['set'] is True and row['source'] == 'app'
    assert settings_store.get_secret('MOUSER_API_KEY') == VALUE
    assert listing.json()['parts_available'] is True


def test_put_replaces_and_delete_removes(api):
    api.put('/api/settings/keys/MOUSER_API_KEY', json={'value': VALUE})
    api.put('/api/settings/keys/MOUSER_API_KEY', json={'value': VALUE + '2'})
    assert settings_store.get_secret('MOUSER_API_KEY') == VALUE + '2'
    done = api.delete('/api/settings/keys/MOUSER_API_KEY')
    assert done.status_code == 200 and settings_store.get_secret('MOUSER_API_KEY') is None
    assert VALUE not in done.text


def test_bad_input_is_refused_without_echoing_it(api):
    for body in ({'value': ' ' * 5}, {'value': 'x' * 600}, {'value': VALUE * 40}, {}):
        reply = api.put('/api/settings/keys/MOUSER_API_KEY', json=body)
        assert reply.status_code in (400, 422)
        assert VALUE not in reply.text
    assert api.put('/api/settings/keys/UNKNOWN', json={'value': 'x'}).status_code == 404
    assert api.delete('/api/settings/keys/PATH').status_code == 404


def test_the_app_token_is_required(api):
    bare = {'X-TrustPCB-Token': 'wrong'}
    assert api.get('/api/settings/keys', headers=bare).status_code == 401
    assert api.put('/api/settings/keys/MOUSER_API_KEY', json={'value': VALUE}, headers=bare).status_code == 401
    assert api.delete('/api/settings/keys/MOUSER_API_KEY', headers=bare).status_code == 401
    assert settings_store.get_secret('MOUSER_API_KEY') is None
    assert TOKEN != 'wrong'


def test_the_demo_refuses_writes_and_shows_nothing(api, monkeypatch):
    settings_store.set_secret('MOUSER_API_KEY', VALUE)
    monkeypatch.setenv('TRUSTPCB_DEMO_READONLY', '1')
    assert api.put('/api/settings/keys/MOUSER_API_KEY', json={'value': 'other-value'}).status_code == 403
    assert api.delete('/api/settings/keys/MOUSER_API_KEY').status_code == 403
    shown = api.get('/api/settings/keys').json()
    assert shown == {'read_only': True, 'keys': [], 'parts_available': False}
    assert settings_store.get_secret('MOUSER_API_KEY') == VALUE


def test_an_environment_key_shows_as_env_and_cannot_be_removed_by_the_app(api, monkeypatch):
    monkeypatch.setenv('MOUSER_API_KEY', 'from-env-5555')
    row = next(r for r in api.get('/api/settings/keys').json()['keys'] if r['name'] == 'MOUSER_API_KEY')
    assert row['source'] == 'env'
    api.delete('/api/settings/keys/MOUSER_API_KEY')
    assert next(r for r in api.get('/api/settings/keys').json()['keys'] if r['name'] == 'MOUSER_API_KEY')['source'] == 'env'
    assert 'from-env-5555' not in json.dumps(api.get('/api/settings/keys').json())
