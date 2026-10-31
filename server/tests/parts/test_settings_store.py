import json
import logging
import os
import stat

import pytest

from trustpcb import settings_store as store

VALUE = 'stored-value-xyz-9999'


def test_set_get_clear_round_trip():
    assert store.get_secret('MOUSER_API_KEY') is None
    store.set_secret('MOUSER_API_KEY', f'  {VALUE}  ')
    assert store.get_secret('MOUSER_API_KEY') == VALUE
    store.clear_secret('MOUSER_API_KEY')
    assert store.get_secret('MOUSER_API_KEY') is None
    assert not store.secrets_path().exists()          # the file goes with its last key


def test_the_file_is_private_and_atomic():
    store.set_secret('DIGIKEY_CLIENT_ID', VALUE)
    mode = stat.S_IMODE(os.stat(store.secrets_path()).st_mode)
    assert mode == 0o600
    assert not [p for p in store.secrets_path().parent.iterdir() if p.name.startswith('.secrets-')]    # no temporary file left behind


def test_a_loosened_file_is_closed_again_on_read():
    store.set_secret('DIGIKEY_CLIENT_ID', VALUE)
    os.chmod(store.secrets_path(), 0o644)
    assert store.get_secret('DIGIKEY_CLIENT_ID') == VALUE
    assert stat.S_IMODE(os.stat(store.secrets_path()).st_mode) == 0o600


def test_environment_wins_and_is_never_cleared(monkeypatch):
    store.set_secret('MOUSER_API_KEY', VALUE)
    monkeypatch.setenv('MOUSER_API_KEY', 'from-the-environment')
    assert store.get_secret('MOUSER_API_KEY') == 'from-the-environment'
    assert store.secret_status()['MOUSER_API_KEY'] == {'set': True, 'source': 'env'}
    store.clear_secret('MOUSER_API_KEY')
    assert os.environ['MOUSER_API_KEY'] == 'from-the-environment'
    assert store.get_secret('MOUSER_API_KEY') == 'from-the-environment'


def test_blank_environment_value_counts_as_unset(monkeypatch):
    monkeypatch.setenv('MOUSER_API_KEY', '   ')
    assert store.secret_status()['MOUSER_API_KEY'] == {'set': False, 'source': None}


def test_status_has_only_set_and_source():
    store.set_secret('DIGIKEY_CLIENT_SECRET', VALUE)
    status = store.secret_status()
    assert status['DIGIKEY_CLIENT_SECRET'] == {'set': True, 'source': 'app'}
    assert status['DIGIKEY_CLIENT_ID'] == {'set': False, 'source': None}
    assert VALUE not in json.dumps(status)


@pytest.mark.parametrize('bad', ['', '   ', 'a' * 513, 'two\nlines', 'tab\there', 'nul\x00'])
def test_invalid_values_are_refused(bad):
    with pytest.raises(ValueError) as error:
        store.set_secret('MOUSER_API_KEY', bad)
    assert bad.strip() == '' or bad not in str(error.value)
    assert store.get_secret('MOUSER_API_KEY') is None


def test_unknown_names_are_refused():
    for call in (lambda: store.get_secret('PATH'), lambda: store.set_secret('PATH', 'x'), lambda: store.clear_secret('PATH')):
        with pytest.raises(store.UnknownKey):
            call()


def test_a_damaged_file_means_no_stored_key(caplog):
    store.set_secret('MOUSER_API_KEY', VALUE)
    store.secrets_path().write_text('{not json')
    with caplog.at_level(logging.WARNING):
        assert store.get_secret('MOUSER_API_KEY') is None
    assert VALUE not in caplog.text


def test_other_names_in_the_file_are_ignored():
    store.secrets_path().parent.mkdir(parents=True, exist_ok=True)
    store.secrets_path().write_text(json.dumps({'MODEL_API_KEY': 'nope', 'MOUSER_API_KEY': VALUE}))
    assert store.secret_status()['MOUSER_API_KEY']['source'] == 'app'
    store.set_secret('DIGIKEY_CLIENT_ID', 'x1')
    assert 'MODEL_API_KEY' not in json.loads(store.secrets_path().read_text())
