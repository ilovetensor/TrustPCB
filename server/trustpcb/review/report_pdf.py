"""Printing a report document to PDF in a locked-down headless browser.

The report HTML is built from board text, so the print step treats it as untrusted even though every value is escaped:

  * JavaScript is off for the whole browser context.
  * Every request except the report file itself is aborted, so nothing can reach the network or another local file.
  * The printed copy carries a strict Content-Security-Policy meta tag (the saved HTML report does not need it).
  * The browser starts with a minimal environment, so server secrets are not inherited.
  * At most one export runs at a time and only a few may wait, so repeated requests cannot exhaust the machine.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import tempfile
import weakref
from pathlib import Path

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

from ..pcb.env import child_environment

logger = logging.getLogger('trustpcb')

PRINT_TIMEOUT_S = 90
MAX_WAITING = 4
PRINT_CSP = "default-src 'none'; img-src data:; style-src 'unsafe-inline'; font-src data:; base-uri 'none'; form-action 'none'"
CSP_META = f'<meta http-equiv="Content-Security-Policy" content="{PRINT_CSP}">'


def minimal_env() -> dict[str, str]:
    """The browser's environment: the allow-list shared with the other programs the server starts (no API keys, no tokens)."""
    return child_environment()


def find_browser() -> str | None:
    configured = os.environ.get('TRUSTPCB_BROWSER')
    if configured and Path(configured).exists():
        return configured
    for name in ('chromium', 'chromium-browser', 'google-chrome', 'google-chrome-stable', 'chrome', 'microsoft-edge', 'brave-browser'):
        found = shutil.which(name)
        if found:
            return found
    for app in ('Google Chrome', 'Chromium', 'Microsoft Edge', 'Brave Browser'):
        mac = f'/Applications/{app}.app/Contents/MacOS/{app}'
        if Path(mac).exists():
            return mac
    return None


def with_csp(document: str) -> str:
    """The document with the print Content-Security-Policy as the first thing in its head."""
    if '<head>' not in document:
        raise ValueError('The report document has no head to carry the content security policy.')
    return document.replace('<head>', '<head>' + CSP_META, 1)


# ---- how many exports at once ------------------------------------------------------------------------

class _Gate:
    def __init__(self) -> None:
        try:
            limit = max(1, int(os.environ.get('TRUSTPCB_PDF_CONCURRENCY', '1')))
        except ValueError:
            limit = 1
        self.semaphore = asyncio.Semaphore(limit)
        self.waiting = 0


_gates: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()      # one gate per event loop


@contextlib.asynccontextmanager
async def pdf_slot():
    """Hold a place for one PDF export. Others queue behind it; when too many already wait, the request is refused."""
    loop = asyncio.get_running_loop()
    gate = _gates.get(loop)
    if gate is None:
        gate = _gates[loop] = _Gate()
    if gate.waiting >= MAX_WAITING:
        raise ValueError('Several PDF exports are already waiting. Try again in a minute, or open the HTML report and print it.')
    gate.waiting += 1
    try:
        await gate.semaphore.acquire()
    finally:
        gate.waiting -= 1
    try:
        yield
    finally:
        gate.semaphore.release()


# ---- printing ----------------------------------------------------------------------------------------

async def print_pdf(source: Path, browser: str | None, *, blocked: list[str] | None = None) -> bytes:
    """Open `source` (a local HTML file) with JavaScript off and no other request allowed, and print it to A4.

    Every refused request is logged and, when `blocked` is a list, appended to it."""
    allowed = source.as_uri()

    async def only_the_report(route) -> None:
        request = route.request
        if request.url == allowed and request.resource_type == 'document':
            await route.continue_()
            return
        logger.warning('PDF export refused a request from the report: %s', request.url[:200])
        if blocked is not None:
            blocked.append(request.url)
        await route.abort()

    async with async_playwright() as driver:
        instance = await driver.chromium.launch(executable_path=browser, env=minimal_env())     # None is the browser Playwright installed
        try:
            context = await instance.new_context(java_script_enabled=False, service_workers='block', accept_downloads=False)
            await context.route('**/*', only_the_report)
            page = await context.new_page()
            await page.goto(allowed, wait_until='load')
            return await page.pdf(format='A4', print_background=True, margin={'top': '12mm', 'bottom': '12mm', 'left': '12mm', 'right': '12mm'})
        finally:
            await instance.close()


async def html_to_pdf(document: str, browser: str | None = None) -> bytes:
    """Prints through Playwright, because `--print-to-pdf` on a full browser never returned here."""
    with tempfile.TemporaryDirectory(prefix='trustpcb-pdf-') as tmp:
        source = Path(tmp) / 'report.html'
        source.write_text(with_csp(document), encoding='utf-8')
        try:
            return await asyncio.wait_for(print_pdf(source, browser or find_browser()), timeout=PRINT_TIMEOUT_S)
        except asyncio.TimeoutError:
            raise ValueError('PDF export timed out.') from None
        except PlaywrightError:
            logger.warning('PDF export failed in the browser', exc_info=True)
            raise ValueError('PDF export needs Chrome or Chromium. Run npm run setup, or open the HTML report and print it to PDF instead.') from None

