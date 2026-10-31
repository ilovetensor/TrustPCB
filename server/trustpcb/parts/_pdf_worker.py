"""Runs in its own process (`python -I _pdf_worker.py <pdf> <out.json>`): reads a PDF page by page with pypdf and writes the text as JSON.

It imports nothing from TrustPCB and gets an environment with no keys. CPU time, memory and output size are limited here, and the
parent kills it after a wall-clock timeout, so a hostile PDF cannot hold the server.
"""
import json
import sys
import time

MAX_PAGES = 900
MAX_PAGE_CHARS = 40000
MAX_SECONDS = 90


def limit() -> None:
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (MAX_SECONDS + 15, MAX_SECONDS + 15))
        resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024 * 1024, 64 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_AS, (3 * 1024 ** 3, 3 * 1024 ** 3))
    except (ImportError, ValueError, OSError):
        pass                                                    # not every system supports every limit; the parent's timeout still applies


def main() -> int:
    limit()
    source, target = sys.argv[1], sys.argv[2]
    out = {'pages': [], 'page_count': 0, 'truncated': False, 'error': None}
    try:
        from pypdf import PdfReader
        reader = PdfReader(source, strict=False)
        if reader.is_encrypted and not reader.decrypt(''):
            out['error'] = 'The PDF is password protected.'
        else:
            total = len(reader.pages)
            out['page_count'] = total
            started = time.monotonic()
            for index in range(min(total, MAX_PAGES)):
                if time.monotonic() - started > MAX_SECONDS:
                    out['truncated'] = True
                    break
                try:
                    text = reader.pages[index].extract_text() or ''
                except Exception:
                    text = ''
                out['pages'].append(text[:MAX_PAGE_CHARS])
            if total > MAX_PAGES:
                out['truncated'] = True
    except Exception as error:
        out['error'] = f'The PDF could not be read ({type(error).__name__}).'
    with open(target, 'w', encoding='utf-8') as handle:
        json.dump(out, handle)
    return 0


if __name__ == '__main__':
    sys.exit(main())
