"""Page-by-page text from a stored datasheet, read by pypdf in a separate process (see `_pdf_worker.py`) and cached beside the PDF."""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from ..pcb.env import child_environment
from .datasheets import DatasheetFile
from .errors import UNREADABLE, PartsError

WORKER = Path(__file__).with_name('_pdf_worker.py')
WALL_TIMEOUT_S = 150.0
VERSION = 1


@dataclass
class SheetText:
    pages: list[str] = field(default_factory=list)       # pages[0] is page 1
    page_count: int = 0
    truncated: bool = False

    @property
    def characters(self) -> int:
        return sum(len(page.strip()) for page in self.pages)


async def extract(sheet: DatasheetFile) -> SheetText:
    """The text of every page. Raises PartsError when the file cannot be read or has no text layer."""
    cached = sheet.path.with_suffix('.text.json')
    try:
        data = json.loads(cached.read_text(encoding='utf-8'))
        if data.get('version') == VERSION:
            return _checked(data)
    except (OSError, ValueError):
        pass
    with tempfile.TemporaryDirectory(prefix='sheet-') as scratch:        # an empty, private working folder for the child
        target = Path(scratch) / 'out.json'
        process = await asyncio.create_subprocess_exec(
            sys.executable, '-I', str(WORKER), str(sheet.path), str(target), cwd=scratch, env=child_environment(),
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        try:
            await asyncio.wait_for(process.wait(), WALL_TIMEOUT_S)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise PartsError(UNREADABLE, 'Reading the datasheet took too long and was stopped.') from None
        try:
            data = json.loads(target.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            raise PartsError(UNREADABLE, 'The datasheet could not be read (the reader stopped without an answer).') from None
    if not isinstance(data, dict) or data.get('error'):
        raise PartsError(UNREADABLE, str((data or {}).get('error') or 'The datasheet could not be read.')[:200])
    text = _checked(data)
    try:
        cached.write_text(json.dumps({**data, 'version': VERSION}), encoding='utf-8')
    except OSError:
        pass
    return text


def _checked(data: dict) -> SheetText:
    pages = [page for page in data.get('pages', []) if isinstance(page, str)]
    text = SheetText(pages=pages, page_count=int(data.get('page_count') or len(pages)), truncated=bool(data.get('truncated')))
    if not text.pages:
        raise PartsError(UNREADABLE, 'The PDF has no pages.')
    if text.characters < 200:
        raise PartsError(UNREADABLE, 'The PDF has no text layer (it looks like scanned images), so nothing could be read from it.')
    return text
