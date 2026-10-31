import asyncio
import json
import logging

import httpx
import pytest
from support import FAKE_ID, FAKE_MOUSER, FAKE_SECRET, digikey_answer, digikey_product, mouser_answer, mouser_part, token_response

from trustpcb.parts import digikey, mouser
from trustpcb.parts import http as parts_http
from trustpcb.parts.errors import CREDENTIALS_REFUSED, RATE_LIMITED, TRANSPORT, UNREADABLE, PartsError


class Clock:
    def __init__(self):
        self.now, self.slept = 1000.0, []

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def run(coro):
    return asyncio.run(coro)


def make_digikey(handler, clock=None):
    clock = clock or Clock()
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return digikey.DigiKeyClient(FAKE_ID, FAKE_SECRET, http=http, sleep=clock.sleep, clock=clock), clock


def search_ok(request):
    return httpx.Response(200, json=digikey_answer(exact=[digikey_product('ABC123')]), headers={'X-RateLimit-Remaining': '800'})


# ---- DigiKey ----

def test_digikey_token_is_fetched_once_and_cached():
    calls = {'token': 0, 'search': 0}

    def handler(request):
        if request.url.path.endswith('/oauth2/token'):
            calls['token'] += 1
            assert b'client_secret=' + FAKE_SECRET.encode() in request.content          # the secret goes only to DigiKey's token endpoint
            return token_response()
        calls['search'] += 1
        assert request.headers['authorization'] == 'Bearer tok-abc-123' and request.headers['x-digikey-client-id'] == FAKE_ID
        assert FAKE_SECRET not in str(request.url) and FAKE_SECRET.encode() not in request.content
        return search_ok(request)

    async def go():
        client, _ = make_digikey(handler)
        for _ in range(3):
            assert (await client.search('ABC123'))[0].mpn == 'ABC123'
    run(go())
    assert calls == {'token': 1, 'search': 3}


def test_digikey_token_is_renewed_before_it_expires():
    tokens = []

    def handler(request):
        if request.url.path.endswith('/oauth2/token'):
            tokens.append(1)
            return token_response()
        return search_ok(request)

    async def go():
        client, clock = make_digikey(handler)
        await client.search('ABC123')
        clock.now += 600                                  # past expires_in (599) minus the margin
        await client.search('ABC123')
    run(go())
    assert len(tokens) == 2


def test_digikey_401_gets_one_new_token_then_gives_up():
    seen = {'search': 0, 'token': 0}

    def handler(request):
        if request.url.path.endswith('/oauth2/token'):
            seen['token'] += 1
            return token_response()
        seen['search'] += 1
        return httpx.Response(401)

    async def go():
        client, _ = make_digikey(handler)
        with pytest.raises(PartsError) as error:
            await client.search('ABC123')
        return error.value
    error = run(go())
    assert error.code == CREDENTIALS_REFUSED and seen == {'search': 2, 'token': 2}


def test_digikey_refused_credentials_say_so_without_a_value():
    def handler(request):
        return httpx.Response(401, json={'ErrorMessage': f'bad client {FAKE_ID} {FAKE_SECRET}'})

    async def go():
        client, _ = make_digikey(handler)
        with pytest.raises(PartsError) as error:
            await client.search('ABC123')
        return error.value
    error = run(go())
    assert error.code == CREDENTIALS_REFUSED and FAKE_SECRET not in error.message and FAKE_ID not in error.message


def test_digikey_waits_out_a_short_429_and_reports_a_long_one():
    state = {'n': 0}

    def short(request):
        if request.url.path.endswith('/oauth2/token'):
            return token_response()
        state['n'] += 1
        return httpx.Response(429, headers={'Retry-After': '3'}) if state['n'] == 1 else search_ok(request)

    def long(request):
        if request.url.path.endswith('/oauth2/token'):
            return token_response()
        return httpx.Response(429, headers={'Retry-After': '3600'})

    async def go():
        client, clock = make_digikey(short)
        assert (await client.search('ABC123'))
        assert clock.slept == [3.0]
        client2, clock2 = make_digikey(long)
        with pytest.raises(PartsError) as error:
            await client2.search('ABC123')
        assert error.value.code == RATE_LIMITED and error.value.retryable and clock2.slept == []
    run(go())


def test_digikey_stops_when_the_daily_allowance_is_spent():
    seen = []

    def handler(request):
        if request.url.path.endswith('/oauth2/token'):
            return token_response()
        seen.append(1)
        return httpx.Response(200, json=digikey_answer(), headers={'X-RateLimit-Remaining': '0'})

    async def go():
        client, _ = make_digikey(handler)
        await client.search('ABC123')
        with pytest.raises(PartsError) as error:
            await client.search('ABC123')
        assert error.value.code == RATE_LIMITED
    run(go())
    assert len(seen) == 1                                 # the second search sent nothing


def test_digikey_outage_is_retried_then_reported():
    def handler(request):
        if request.url.path.endswith('/oauth2/token'):
            return token_response()
        return httpx.Response(503)

    def flaky(request):
        if request.url.path.endswith('/oauth2/token'):
            return token_response()
        flaky.n += 1
        return httpx.Response(500) if flaky.n == 1 else search_ok(request)
    flaky.n = 0

    async def go():
        client, clock = make_digikey(flaky)
        assert await client.search('ABC123')
        assert clock.slept == [1.5]
        client2, clock2 = make_digikey(handler)
        with pytest.raises(PartsError) as error:
            await client2.search('ABC123')
        assert error.value.retryable and clock2.slept == [1.5, 4.0]
    run(go())


def test_digikey_timeout_error_has_no_cause_with_a_url():
    def handler(request):
        if request.url.path.endswith('/oauth2/token'):
            return token_response()
        raise httpx.ConnectTimeout('timed out', request=request)

    async def go():
        client, _ = make_digikey(handler)
        with pytest.raises(PartsError) as error:
            await client.search('ABC123')
        return error.value
    error = run(go())
    assert error.code == TRANSPORT and error.__cause__ is None and FAKE_ID not in str(error)


def test_digikey_answer_outside_its_shape_is_unreadable():
    def handler(request):
        return token_response() if request.url.path.endswith('/oauth2/token') else httpx.Response(200, json={'Nope': 1})

    async def go():
        client, _ = make_digikey(handler)
        with pytest.raises(PartsError) as error:
            await client.search('ABC123')
        assert error.value.code == UNREADABLE
    run(go())


def test_digikey_listing_fields_are_bounded_and_deduplicated():
    answer = digikey_answer(exact=[digikey_product('ABC123', description='D' * 900)], products=[digikey_product('ABC123'), digikey_product('ABC124')])
    found = digikey.parse_answer(answer)
    assert [item.mpn for item in found] == ['ABC123', 'ABC124']
    assert found[0].exact and not found[1].exact and len(found[0].description) <= 500
    assert found[0].params == {'Package / Case': 'SOT-23-6'} and found[0].source == 'digikey'


@pytest.mark.parametrize('given,expected', [
    ('//mm.digikey.com/a.pdf', 'https://mm.digikey.com/a.pdf'),
    ('http://www.st.com/x.pdf', 'https://www.st.com/x.pdf'),
    ('https://www.ti.com/general/docs/suppproductinfo.tsp?distId=10&gotoUrl=http%3A%2F%2Fwww.ti.com%2Flit%2Fgpn%2Flm2596', 'https://www.ti.com/lit/gpn/lm2596'),
    ('https://www.ti.com/general/docs/suppproductinfo.tsp?gotoUrl=https%3A%2F%2Fevil.example%2Fx.pdf', None),
    ('javascript:alert(1)', None), ('ftp://x/y.pdf', None), ('-', None), (None, None)])
def test_datasheet_links(given, expected):
    assert digikey.datasheet_link(given) == expected


# ---- Mouser ----

def make_mouser(handler, clock=None):
    clock = clock or Clock()
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return mouser.MouserClient(FAKE_MOUSER, http=http, sleep=clock.sleep, clock=clock), clock


def test_mouser_part_search_sends_the_key_as_a_query_parameter_only():
    def handler(request):
        assert request.url.params['apiKey'] == FAKE_MOUSER and FAKE_MOUSER.encode() not in request.content
        body = json.loads(request.content)
        assert body == {'SearchByPartRequest': {'mouserPartNumber': 'ABC123', 'partSearchOptions': 'Exact'}}
        return httpx.Response(200, json=mouser_answer(mouser_part('ABC123', attrs={'Package': 'SOT-23'})))

    async def go():
        client, _ = make_mouser(handler)
        found = await client.by_part_number('ABC123')
        assert found[0].mpn == 'ABC123' and found[0].params == {'Package': 'SOT-23'} and found[0].source == 'mouser'
    run(go())


def test_mouser_numbers_it_cannot_be_asked_are_empty_not_errors():
    async def go():
        client, _ = make_mouser(lambda r: pytest.fail('no request expected'))
        assert await client.by_part_number('ab') == []
        assert await client.by_part_number('a|b|c') == []
    run(go())


def test_mouser_errors_never_carry_the_key_or_the_url():
    def timeout(request):
        raise httpx.ConnectTimeout(f'timeout for {request.url}', request=request)

    async def go():
        client, clock = make_mouser(timeout)
        with pytest.raises(PartsError) as error:
            await client.by_part_number('ABC123')
        return error.value, clock.slept
    error, slept = run(go())
    assert error.code == TRANSPORT and error.__cause__ is None and FAKE_MOUSER not in str(error) and slept == [2.0]


@pytest.mark.parametrize('status,code', [(403, CREDENTIALS_REFUSED), (429, RATE_LIMITED)])
def test_mouser_statuses(status, code):
    async def go():
        client, _ = make_mouser(lambda r: httpx.Response(status))
        with pytest.raises(PartsError) as error:
            await client.by_part_number('ABC123')
        assert error.value.code == code and FAKE_MOUSER not in error.value.message
    run(go())


def test_mouser_invalid_key_reported_in_the_body():
    body = {'Errors': [{'Code': 'Invalid', 'Message': 'Invalid unique identifier', 'ResourceKey': 'InvalidIdentifier', 'PropertyName': 'API Key'}], 'SearchResults': None}

    async def go():
        client, _ = make_mouser(lambda r: httpx.Response(200, json=body))
        with pytest.raises(PartsError) as error:
            await client.by_part_number('ABC123')
        assert error.value.code == CREDENTIALS_REFUSED
    run(go())


def test_mouser_is_paced_to_thirty_calls_a_minute():
    async def go():
        client, clock = make_mouser(lambda r: httpx.Response(200, json=mouser_answer()))
        for _ in range(31):
            await client.by_part_number('ABC123')
        assert len(clock.slept) == 1 and 59 <= clock.slept[0] <= 60
    run(go())


def test_mouser_daily_allowance(monkeypatch):
    monkeypatch.setattr(mouser, 'DAILY_LIMIT', 2)

    async def go():
        client, _ = make_mouser(lambda r: httpx.Response(200, json=mouser_answer()))
        await client.by_part_number('ABC123')
        await client.by_part_number('ABC123')
        with pytest.raises(PartsError) as error:
            await client.by_part_number('ABC123')
        assert error.value.code == RATE_LIMITED
    run(go())


def test_request_log_lines_do_not_show_the_key(caplog):
    parts_http.install_redaction()
    logger = logging.getLogger('httpx')
    logger.setLevel(logging.INFO)
    with caplog.at_level(logging.INFO, logger='httpx'):
        logger.info('HTTP Request: POST https://api.mouser.com/api/v1/search/partnumber?apiKey=%s "HTTP/1.1 200 OK"', FAKE_MOUSER)
    # the filter is on the logger, so the stored record is already rewritten
    assert FAKE_MOUSER not in caplog.text and 'apiKey=<redacted>' in caplog.text
