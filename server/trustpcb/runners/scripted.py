"""Deterministic runner for tests and demos. It never calls a model.

A script is a list of steps run in order. Step forms:

  {"text": "..."}                         stream text (split into small text_delta events)
  {"thought": "..."}                      same as text, as a plain `thought` event
  {"tool": "name", "args": {...}}         call a registered tool; emits tool_start and tool_result
  {"emit": "event_type", "payload": {}}   emit any event
  {"interact": "kind", "payload": {}}     wait for a user decision; the answer is stored as `last_answer`
  {"if_answer": {"allow": true}, "then": [steps], "else": [steps]}
  {"sleep": 0.5}                          pause (cancel-aware)
  {"usage": {"input_tokens": 1, "output_tokens": 1}}
  {"fail": "message"}                     raise RuntimeError

Each tool call counts as one turn against request.max_turns; at the limit the runner emits a
`status` event {"state": "max_turns"} and stops cleanly, like a real runner.

Script sources, first match wins: constructor argument, request.profile["script"], the JSON file
named by TRUSTPCB_SCRIPT, then a short default.
"""
from __future__ import annotations

import asyncio
import json
import os
import zlib
from pathlib import Path

from .base import RunRequest, call_tool, result_payload

DEFAULT_SCRIPT = [
    {'text': 'Scripted review: no model was called.'},
    {'usage': {'input_tokens': 0, 'output_tokens': 0}},
]


class ScriptedRunner:
    def __init__(self, script: list[dict] | None = None):
        self.script = script
        self.last_answer: dict | None = None
        self.requests: list[RunRequest] = []
        self._canceled = False
        self._wake = asyncio.Event()

    def _load(self, request: RunRequest) -> list[dict]:
        if self.script is not None:
            return self.script
        if request.profile and request.profile.get('script') is not None:
            return request.profile['script']
        path = os.environ.get('TRUSTPCB_SCRIPT')
        if path:
            return json.loads(Path(path).read_text(encoding='utf-8'))
        return DEFAULT_SCRIPT

    async def run(self, request: RunRequest, emit, interact) -> None:
        self._canceled = False
        self._wake = asyncio.Event()
        self.requests.append(request)
        self._turns = 0
        self._limit = request.max_turns
        if request.session_id is None:
            await emit('session', {'session_id': f'scripted-{zlib.crc32(request.user_text.encode()):08x}'})
        await self._steps(self._load(request), request, emit, interact)

    async def _steps(self, steps, request, emit, interact) -> bool:
        """Run steps; False once stopped by cancel or the turn limit."""
        for step in steps:
            if self._canceled:
                return False
            if 'text' in step:
                text = step['text']
                for start in range(0, len(text), 24):
                    await emit('text_delta', {'text': text[start:start + 24]})
            elif 'thought' in step:
                await emit('thought', {'text': step['thought']})
            elif 'tool' in step:
                if self._limit is not None and self._turns >= self._limit:
                    await emit('status', {'state': 'max_turns', 'max_turns': self._limit})
                    return False
                self._turns += 1
                call_id = f'scripted-{self._turns}'
                await emit('tool_start', {'tool_call_id': call_id, 'name': step['tool'], 'input': step.get('args', {})})
                try:
                    result = await call_tool(request.tools, step['tool'], step.get('args', {}))
                    payload = result_payload(result)
                except KeyError:
                    payload = {'content': f"Unknown tool {step['tool']}", 'is_error': True, 'images': []}
                except Exception as exc:  # a tool failure is a result the script can continue past
                    payload = {'content': f'Tool failed: {exc}', 'is_error': True, 'images': []}
                await emit('tool_result', {'tool_call_id': call_id, 'name': step['tool'], **payload})
            elif 'emit' in step:
                await emit(step['emit'], step.get('payload', {}))
            elif 'interact' in step:
                self.last_answer = await interact(step['interact'], step.get('payload', {}))
            elif 'if_answer' in step:
                matches = self.last_answer is not None and all(self.last_answer.get(k) == v for k, v in step['if_answer'].items())
                if not await self._steps(step.get('then', []) if matches else step.get('else', []), request, emit, interact):
                    return False
            elif 'sleep' in step:
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=float(step['sleep']))
                except asyncio.TimeoutError:
                    pass
            elif 'usage' in step:
                await emit('usage', step['usage'])
            elif 'fail' in step:
                raise RuntimeError(step['fail'])
            else:
                raise ValueError(f'Unknown script step: {sorted(step)}')
        return not self._canceled

    async def cancel(self) -> None:
        self._canceled = True
        self._wake.set()

    async def close(self) -> None:
        self._wake.set()
