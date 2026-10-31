"""Printing a report to PDF: JavaScript off, no request but the report itself, a strict policy, a minimal environment and one export at a time."""
import asyncio
import io
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from playwright.async_api import async_playwright
from pypdf import PdfReader

from trustpcb.review import report_pdf


def text_of(pdf: bytes) -> str:
    return ' '.join(page.extract_text() for page in PdfReader(io.BytesIO(pdf)).pages)


@pytest.fixture
def listener():
    """A local web server that records every request it receives."""
    seen: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'NETWORK-REACHED')

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{server.server_address[1]}', seen
    server.shutdown()
    server.server_close()


def hostile_document(url: str, secret_uri: str) -> str:
    return (f'<!doctype html><html><head><meta charset="utf-8"><title>t</title></head><body><p>STATIC-TEXT</p>'
            f'<img src="{url}/img.png" onerror="document.body.append(\'ONERROR-RAN\')">'
            f'<link rel="stylesheet" href="{url}/style.css"><iframe src="{url}/frame"></iframe>'
            f'<iframe src="{secret_uri}"></iframe><object data="{secret_uri}"></object><embed src="{secret_uri}">'
            f'<script src="{url}/script.js"></script>'
            f'<script>document.body.insertAdjacentHTML("beforeend","<p>SCRIPT-RAN</p>");fetch("{url}/fetch");new Image().src="{url}/beacon";</script>'
            f'<style>body{{background:url({url}/bg.png)}}@import url({url}/import.css);</style>'
            '</body></html>')


async def browser_or_skip():
    path = report_pdf.find_browser()
    try:
        await report_pdf.html_to_pdf('<!doctype html><html><head></head><body>probe</body></html>', path)
    except ValueError as exc:
        pytest.skip(str(exc))
    return path


async def test_a_page_cannot_reach_the_network_or_a_local_file_and_scripts_do_not_run(tmp_path, listener):
    url, seen = listener
    path = await browser_or_skip()
    secret = tmp_path / 'secret.txt'
    secret.write_text('FILE-SECRET-CONTENT')
    document = hostile_document(url, secret.as_uri())
    page = tmp_path / 'report.html'
    page.write_text(document, encoding='utf-8')
    blocked: list[str] = []

    pdf = await report_pdf.print_pdf(page, path, blocked=blocked)          # the browser lock alone, without the policy tag
    text = text_of(pdf)
    assert 'STATIC-TEXT' in text
    assert 'SCRIPT-RAN' not in text and 'ONERROR-RAN' not in text
    assert 'FILE-SECRET-CONTENT' not in text and 'NETWORK-REACHED' not in text
    assert seen == []                                                          # nothing arrived at the server
    assert any(u.startswith(url) for u in blocked) and secret.as_uri() in blocked

    pdf = await report_pdf.html_to_pdf(document, path)                         # the full export path, with the policy tag
    text = text_of(pdf)
    assert 'STATIC-TEXT' in text and 'SCRIPT-RAN' not in text and 'FILE-SECRET-CONTENT' not in text
    assert seen == []


async def test_the_policy_tag_alone_stops_loads_and_scripts(tmp_path, listener):
    """Defence in depth: in a browser with JavaScript on and no request filter, the printed document still loads nothing and runs nothing."""
    url, seen = listener
    path = await browser_or_skip()
    secret = tmp_path / 'secret.txt'
    secret.write_text('FILE-SECRET-CONTENT')
    page = tmp_path / 'report.html'
    page.write_text(report_pdf.with_csp(hostile_document(url, secret.as_uri())), encoding='utf-8')
    async with async_playwright() as driver:
        browser = await driver.chromium.launch(executable_path=path, env=report_pdf.minimal_env())
        try:
            tab = await browser.new_page()                                     # JavaScript is on here on purpose
            await tab.goto(page.as_uri(), wait_until='load')
            await tab.wait_for_timeout(300)
            body = await tab.inner_text('body')
        finally:
            await browser.close()
    assert 'STATIC-TEXT' in body and 'SCRIPT-RAN' not in body and 'ONERROR-RAN' not in body and 'FILE-SECRET-CONTENT' not in body
    assert seen == []


def test_the_policy_is_added_as_the_first_thing_in_the_head():
    document = '<!doctype html><html lang="en"><head><meta charset="utf-8"></head><body></body></html>'
    printed = report_pdf.with_csp(document)
    assert printed.startswith('<!doctype html><html lang="en"><head><meta http-equiv="Content-Security-Policy"')
    assert "default-src 'none'" in printed and 'img-src data:' in printed and 'script-src' not in printed
    with pytest.raises(ValueError):
        report_pdf.with_csp('<p>no head</p>')


async def test_inline_images_and_the_report_text_survive_the_lock(tmp_path):
    path = await browser_or_skip()
    pixel = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=='
    document = (f'<!doctype html><html><head><meta charset="utf-8"><style>.s{{width:40px;height:40px}}</style></head><body>'
                f'<p style="color:#145a3b">STYLED-TEXT</p><img class="s" alt="still" src="data:image/png;base64,{pixel}">'
                '<svg viewBox="0 0 100 20" width="200"><text x="1" y="12" font-size="8">SVG-TEXT</text></svg></body></html>')
    page = tmp_path / 'report.html'
    page.write_text(report_pdf.with_csp(document), encoding='utf-8')
    blocked: list[str] = []
    pdf = await report_pdf.print_pdf(page, path, blocked=blocked)
    assert 'STYLED-TEXT' in text_of(pdf) and 'SVG-TEXT' in text_of(pdf)
    assert len(PdfReader(io.BytesIO(pdf)).pages[0].images) == 1                # the inline image was drawn
    assert blocked == []                                                       # nothing legitimate was refused


def test_the_browser_gets_a_minimal_environment(monkeypatch):
    monkeypatch.setenv('MODEL_API_KEY', 'secret-value')
    monkeypatch.setenv('TRUSTPCB_TOKEN', 'secret-token')
    monkeypatch.setenv('PATH', '/usr/bin')
    env = report_pdf.minimal_env()
    assert env['PATH'] == '/usr/bin'
    assert 'MODEL_API_KEY' not in env and 'TRUSTPCB_TOKEN' not in env
    assert set(env) <= set(os.environ)


async def test_only_one_export_runs_at_a_time_and_a_long_queue_is_refused():
    running = peak = done = 0

    async def export():
        nonlocal running, peak, done
        async with report_pdf.pdf_slot():
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.02)
            running -= 1
            done += 1

    tasks = [asyncio.create_task(export()) for _ in range(report_pdf.MAX_WAITING + 3)]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    refused = [r for r in results if isinstance(r, ValueError)]
    assert peak == 1                                                           # never two at once
    assert len(refused) == 2 and done == report_pdf.MAX_WAITING + 1            # one running, MAX_WAITING queued, the rest turned away
    await export()                                                             # and the gate is free again afterwards
