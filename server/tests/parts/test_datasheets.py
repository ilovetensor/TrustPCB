import asyncio
import os
import stat

import httpx
import pytest
from support import SHEET_PAGES, make_pdf

from trustpcb.parts import datasheets
from trustpcb.parts.errors import REQUEST_FAILED, TRANSPORT, UNREADABLE, PartsError

PDF = make_pdf(SHEET_PAGES)
URL = 'https://example.com/ds/ab1234.pdf'


def fetch(url=URL):
    return asyncio.run(datasheets.fetch(url))


def failure(url=URL):
    with pytest.raises(PartsError) as error:
        fetch(url)
    return error.value


def pdf_handler(request):
    return httpx.Response(200, content=PDF, headers={'content-type': 'application/pdf'})


def test_a_pdf_is_stored_by_digest_privately_and_reused(fake_http):
    seen = fake_http(pdf_handler)
    first = fetch()
    assert first.path.name == f'{first.sha256}.pdf' and first.path.read_bytes() == PDF and not first.cached
    assert stat.S_IMODE(os.stat(first.path).st_mode) == 0o600
    second = fetch()
    assert second.cached and second.path == first.path and len(seen) == 1             # the second call made no request


def test_http_links_are_upgraded_to_https(fake_http):
    seen = fake_http(pdf_handler)
    fetch('http://example.com/ds/ab1234.pdf')
    assert str(seen[0].url).startswith('https://')


@pytest.mark.parametrize('url', ['ftp://example.com/a.pdf', 'file:///etc/passwd', 'https://user:pw@example.com/a.pdf', 'https://example.com:8443/a.pdf', 'javascript:alert(1)', '', 'https:///a.pdf'])
def test_only_plain_https_links_are_fetched(url, fake_http):
    seen = fake_http(pdf_handler)
    assert failure(url).code == REQUEST_FAILED and not seen


@pytest.mark.parametrize('host', ['127.0.0.1', '10.0.0.5', '192.168.1.1', '169.254.169.254', '[::1]', 'localhost'])
def test_local_and_private_addresses_are_never_fetched(host, fake_http, monkeypatch):
    async def resolve(name):
        return ['127.0.0.1']                                   # what the name `localhost` and similar resolve to
    monkeypatch.setattr(datasheets, '_resolve', resolve)
    seen = fake_http(pdf_handler)
    assert failure(f'https://{host}/a.pdf').code == REQUEST_FAILED and not seen


def test_a_redirect_into_the_local_network_is_refused(fake_http, monkeypatch):
    async def resolve(name):
        return ['10.1.2.3'] if name == 'internal.example' else ['93.184.216.34']
    monkeypatch.setattr(datasheets, '_resolve', resolve)
    seen = fake_http(lambda r: httpx.Response(302, headers={'location': 'https://internal.example/secret'}))
    error = failure()
    assert error.code == REQUEST_FAILED and len(seen) == 1


def test_redirects_are_followed_a_few_times_only(fake_http):
    state = {'n': 0}

    def handler(request):
        state['n'] += 1
        if state['n'] <= 2:
            return httpx.Response(301, headers={'location': '/next'})
        return pdf_handler(request)
    fake_http(handler)
    assert fetch().size == len(PDF)
    fake_http(lambda r: httpx.Response(302, headers={'location': '/again'}))
    assert 'redirects' in failure('https://example.com/other.pdf').message


def test_a_web_page_is_not_a_datasheet(fake_http):
    fake_http(lambda r: httpx.Response(200, content=b'<html>login</html>', headers={'content-type': 'text/html; charset=utf-8'}))
    error = failure()
    assert error.code == UNREADABLE and 'not a PDF' in error.message


def test_a_page_served_as_a_download_is_still_checked_by_its_bytes(fake_http):
    fake_http(lambda r: httpx.Response(200, content=b'<html>not a pdf</html>', headers={'content-type': 'application/octet-stream'}))
    assert failure().code == UNREADABLE


def test_size_limit_by_header_and_by_stream(fake_http, monkeypatch):
    monkeypatch.setattr(datasheets, 'MAX_BYTES', 1000)
    fake_http(lambda r: httpx.Response(200, content=b'%PDF-' + b'x' * 5000, headers={'content-type': 'application/pdf', 'content-length': '5005'}))
    assert 'larger' in failure().message
    fake_http(lambda r: httpx.Response(200, content=b'%PDF-' + b'x' * 5000, headers={'content-type': 'application/pdf'}))
    assert 'larger' in failure('https://example.com/b.pdf').message


def test_a_refused_browser_request_is_retried_plainly_once(fake_http):
    seen = fake_http(lambda r: httpx.Response(403) if 'Mozilla' in r.headers.get('user-agent', '') else pdf_handler(r))
    assert fetch().size == len(PDF) and len(seen) == 2
    fake_http(lambda r: httpx.Response(403))
    error = failure('https://example.com/c.pdf')
    assert 'automated downloads' in error.message


def test_unreachable_hosts_say_so_without_the_url(fake_http):
    def handler(request):
        raise httpx.ConnectError('boom', request=request)
    fake_http(handler)
    error = failure()
    assert error.code == TRANSPORT and error.retryable and 'http' not in error.message and error.__cause__ is None


def test_an_unknown_host_is_a_clear_failure(fake_http, monkeypatch):
    async def resolve(name):
        raise OSError('no such host')
    monkeypatch.setattr(datasheets, '_resolve', resolve)
    fake_http(pdf_handler)
    assert failure().code == TRANSPORT
