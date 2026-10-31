#!/usr/bin/env python3
"""G0 check for the muse_api runner: one small tool turn, then a tool that returns a small PNG.

Needs MODEL_API_KEY in the environment; without it this prints why and exits 2. It makes at most a
handful of short requests (low effort, small max_tokens) and prints what worked. It prints no key.

    MODEL_API_KEY=... python3 scripts/muse-smoke.py [--mode tool_result|user_message|both] [--plain]

The image check asks the model for the colour of a solid-colour square. A 200 reply that names the wrong
colour (or says it sees no image) means images inside tool results are accepted but not seen: rerun with
--mode user_message and set TRUSTPCB_MUSE_IMAGE_MODE=user_message for real reviews.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import os
import struct
import sys
import tempfile
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))

from trustpcb.review.tools import ToolDef, ToolRegistry, ToolResult  # noqa: E402
from trustpcb.runners.base import RunRequest  # noqa: E402
from trustpcb.runners.muse import MuseError, MuseRunner  # noqa: E402


def png(rgb: tuple[int, int, int], size: int = 24) -> str:
    """A solid-colour PNG, base64 encoded."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack('>I', len(data)) + body + struct.pack('>I', zlib.crc32(body) & 0xFFFFFFFF)
    rows = b''.join(b'\x00' + bytes(rgb) * size for _ in range(size))
    raw = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', size, size, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(rows)) + chunk(b'IEND', b'')
    return base64.b64encode(raw).decode()


class Gate:
    def allowed_names(self):
        return ['add_numbers', 'show_swatch']

    def phase_label(self):
        return 'smoke'

    async def before(self, tool, args):
        return None

    async def after(self, tool, args, result):
        return None


def registry() -> ToolRegistry:
    async def add_numbers(args):
        return ToolResult(text=str(args.get('a', 0) + args.get('b', 0)))

    async def show_swatch(args):
        return ToolResult(text='A colour swatch.', images=[{'mime': 'image/png', 'data_b64': png((220, 20, 20))}])

    nums = {'type': 'object', 'properties': {'a': {'type': 'integer'}, 'b': {'type': 'integer'}}, 'required': ['a', 'b'], 'additionalProperties': False}
    none = {'type': 'object', 'properties': {}, 'required': [], 'additionalProperties': False}
    return ToolRegistry([ToolDef('add_numbers', 'Add two integers.', nums, add_numbers, phases=frozenset({0})),
                         ToolDef('show_swatch', 'Return a small image of a solid colour square.', none, show_swatch, phases=frozenset({0}), image=True)], Gate())


async def attempt(label: str, prompt: str, image_mode: str | None, stream: bool, max_turns: int) -> tuple[bool, str, list]:
    os.environ['TRUSTPCB_MUSE_STREAM'] = '1' if stream else '0'
    if image_mode:
        os.environ['TRUSTPCB_MUSE_IMAGE_MODE'] = image_mode
    else:
        os.environ.pop('TRUSTPCB_MUSE_IMAGE_MODE', None)
    events: list[tuple[str, dict]] = []

    async def emit(kind, payload):
        events.append((kind, payload))

    async def interact(kind, payload):
        return {}

    with tempfile.TemporaryDirectory() as scratch:
        runner = MuseRunner(data_dir=scratch, max_tokens=700, effort='low')
        request = RunRequest(None, 'You are a terse test assistant. Use the tools when asked.', prompt, registry(), max_turns, {})
        try:
            await runner.run(request, emit, interact)
        except MuseError as exc:
            return False, f'{label}: FAILED - {exc}', events
    text = ''.join(p.get('text', '') for k, p in events if k == 'text_delta')
    return True, text, events


def totals(events) -> str:
    usage = [p for k, p in events if k == 'usage']
    return f'{len(usage)} responses, {sum(u.get("input_tokens", 0) for u in usage)} in / {sum(u.get("output_tokens", 0) for u in usage)} out tokens'


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--mode', choices=['tool_result', 'user_message', 'both', 'auto'], default='both',
                        help='where the image goes; auto lets the runner fall back by itself')
    parser.add_argument('--plain', action='store_true', help='use plain responses instead of streaming')
    args = parser.parse_args()
    if not (os.environ.get('MODEL_API_KEY') or '').strip():
        print('MODEL_API_KEY is not set, so nothing was called. Export it and run this again.')
        return 2
    stream = not args.plain
    failures = 0

    ok, text, events = await attempt('tool turn', 'Use add_numbers with a=17 and b=25, then state the sum in one short sentence.', None, stream, 4)
    sums = [p for k, p in events if k == 'tool_result' and p.get('name') == 'add_numbers']
    worked = ok and bool(sums) and '42' in text
    print(f'[{"ok" if worked else "FAIL"}] tool turn: tool called={bool(sums)}, answer mentions 42={"42" in text}, {totals(events)}')
    if not worked:
        failures += 1
        print('       ', text[:300])

    modes = {'both': ['tool_result', 'user_message'], 'auto': [None]}.get(args.mode, [args.mode])
    for mode in modes:
        label = f'image ({mode or "auto"})'
        ok, text, events = await attempt(label, 'Call show_swatch, then say in one word what colour the square is.', mode, stream, 4)
        fell_back = any(k == 'status' and p.get('state') == 'image_fallback' for k, p in events)
        sees = ok and 'red' in text.lower()
        print(f'[{"ok" if sees else "FAIL"}] {label}: model said {text[:80]!r}, fallback used={fell_back}, {totals(events)}')
        if not sees:
            failures += 1
    print('done' if not failures else f'{failures} check(s) failed')
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
