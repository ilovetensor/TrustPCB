"""Muse runner: a direct, tracked tool loop against Meta's Messages API (POST /v1/messages).

The loop is ours. Each model turn re-reads the tool registry (the allowed set changes with the review
phase), sends the conversation, executes the tool calls the model asked for through `call_tool` (so an
out-of-phase call returns the registry's refusal JSON to the model) and repeats until the model ends its
turn or the turn limit is reached.

Facts this module relies on (Meta Model API docs, checked 2026-10-31):
  * base URL https://api.meta.ai, `Authorization: Bearer $MODEL_API_KEY`, model `muse-spark-1.3`.
  * `max_tokens` is required. `stop_sequences`, `top_k`, `container`, `inference_geo`, unknown top-level
    fields, a named `tool_choice` and `thinking: disabled` are rejected with HTTP 400.
  * `thinking: {type: adaptive}` with `output_config.effort` (low|medium|high|xhigh).
  * Assistant `thinking` and `redacted_thinking` blocks are replayed exactly as returned.
  * Prompt caching is automatic on the server; this module sends no cache markers.
  * The `-contributor` models allow training on the content, so this runner refuses them.

Baseten's hackathon endpoint (https://inference.baseten.co, model `meta-models/muse-spark-open-1026`) speaks the
same Messages format with the same Bearer auth; its responses carry `thinking` blocks before text and tool calls.

Key and provider (first one set wins): MODEL_API_KEY (sent to TRUSTPCB_MUSE_BASE_URL, default api.meta.ai), then
META_API_KEY (api.meta.ai, muse-spark-1.3), then BASETEN_API_KEY (inference.baseten.co, muse-spark-open-1026). The last
two may also be saved in Settings (settings_store); only the chosen key is sent, and only to its own host.
Other configuration (environment): TRUSTPCB_MUSE_BASE_URL (an https URL on ALLOWED_HOSTS), TRUSTPCB_MUSE_MODEL (wins over the profile model),
TRUSTPCB_MUSE_EFFORT (default medium), TRUSTPCB_MUSE_IMAGE_MODE (tool_result|user_message),
TRUSTPCB_MUSE_STREAM (default 1; 0 uses plain responses), TRUSTPCB_MUSE_TIMEOUT_S (read timeout),
TRUSTPCB_DATA_DIR (where conversations are saved, under muse_sessions/).
"""
from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import logging
import os
import random
import re
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from .base import RunRequest, call_tool, result_payload, tool_specs

log = logging.getLogger('trustpcb.muse')

BASE_URL = 'https://api.meta.ai'
ENDPOINT = '/v1/messages'
DEFAULT_MODEL = 'muse-spark-1.3'
# The key is sent only to these hosts, over https.
ALLOWED_HOSTS = ('api.meta.ai', 'inference.baseten.co')
# Named keys and where each one goes. A key saved for one provider is never sent to the other.
PROVIDERS = {
    'META_API_KEY': {'base_url': BASE_URL, 'model': DEFAULT_MODEL},
    'BASETEN_API_KEY': {'base_url': 'https://inference.baseten.co', 'model': 'meta-models/muse-spark-open-1026'},
}
DEFAULT_EFFORT = 'medium'
EFFORTS = ('low', 'medium', 'high', 'xhigh')
MAX_TOKENS = 8000
MAX_RETRIES = 5
IMAGE_MODES = ('tool_result', 'user_message')
# Meta's image guide says images belong in user messages and images on other roles are not processed,
# so an image inside a tool result could be accepted and silently ignored.
DEFAULT_IMAGE_MODE = 'user_message'
STORE_VERSION = 1
REPO_ROOT = Path(__file__).resolve().parents[3]
# A 400 that names the image, the tool result or the media type means images inside tool results are not accepted.
IMAGE_400 = re.compile(r'image|tool_result|tool result|media|base64', re.I)
RETRYABLE_STREAM_ERRORS = ('overloaded_error', 'api_error', 'rate_limit_error')
SESSION_ID = re.compile(r'^[0-9A-Za-z][0-9A-Za-z_-]{7,79}$')


# ---- errors and secrets --------------------------------------------------------------------

class MuseError(RuntimeError):
    """A failure with a message safe to show: secrets are already removed."""


class MuseHTTPError(MuseError):
    def __init__(self, status: int, error_type: str | None, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.status, self.error_type, self.message, self.retry_after = status, error_type, message, retry_after

    @property
    def retryable(self) -> bool:
        return self.status == 429 or self.status >= 500


class MuseStreamError(MuseError):
    def __init__(self, message: str, retryable: bool):
        super().__init__(message)
        self.retryable = retryable


class _Canceled(Exception):
    """Internal: the run was canceled; unwinds to run(), which returns quietly."""


_BEARER = re.compile(r'(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{6,}')
_ENV_KEY = re.compile(r'(?i)((?:MODEL|META|BASETEN)_API_KEY\s*[=:]\s*)[^\s,;"\']+')


def redact(text: Any, secrets: tuple[str, ...] | list[str] = ()) -> str:
    out = str(text)
    for secret in secrets:
        if secret:
            out = out.replace(secret, '[REDACTED]')
    out = _BEARER.sub(r'\1[REDACTED]', out)
    return _ENV_KEY.sub(r'\1[REDACTED]', out)


def _stored(name: str) -> str | None:
    from .. import settings_store
    try:
        return settings_store.get_secret(name)
    except Exception:                               # an unreadable store is the same as no stored key
        return None


def resolve_target() -> dict | None:
    """{'name', 'key', 'base_url', 'model'} for the first key that is set, or None. base_url and model are None for MODEL_API_KEY."""
    key = (os.environ.get('MODEL_API_KEY') or '').strip()
    if key:
        return {'name': 'MODEL_API_KEY', 'key': key, 'base_url': None, 'model': None}
    for name, target in PROVIDERS.items():
        key = (_stored(name) or '').strip()
        if key:
            return {'name': name, 'key': key, **target}
    return None


def configured() -> bool:
    """Whether any Muse key is set (environment or Settings). Never returns or logs the value."""
    return resolve_target() is not None


def resolve_base_url(value: str | None = None) -> str:
    """TRUSTPCB_MUSE_BASE_URL, else `value`, else the default; refuses any host that may not receive the key."""
    url = (os.environ.get('TRUSTPCB_MUSE_BASE_URL') or value or BASE_URL).strip()
    scheme = host = ''
    ok = False
    try:
        parts = urlsplit(url)
        scheme, host = parts.scheme, (parts.hostname or '').lower()
        ok = scheme == 'https' and host in ALLOWED_HOSTS and not parts.username and not parts.password and parts.port in (None, 443)
    except ValueError:
        pass
    if not ok:
        raise MuseError(f'Refusing to send the Muse key to {host or "that URL"}: the base URL must be https on one of {", ".join(ALLOWED_HOSTS)}.')
    return f'https://{host}{parts.path.rstrip("/")}'


def resolve_base_url_for(provider_url: str, override: str | None = None) -> str:
    """The base URL for a named provider key: `override` only when it is on the provider's own host."""
    url = resolve_base_url(provider_url) if not override else resolve_base_url(override)
    if urlsplit(url).hostname != urlsplit(provider_url).hostname:
        raise MuseError(f'Refusing to send this key to {urlsplit(url).hostname}: it belongs to {urlsplit(provider_url).hostname}.')
    return url


# ---- message helpers -----------------------------------------------------------------------

def _blocks(message: dict) -> list[dict]:
    content = message.get('content')
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def _is_image(block: dict) -> bool:
    return block.get('type') == 'image'


def _has_images(message: dict) -> bool:
    for block in _blocks(message):
        if _is_image(block):
            return True
        if block.get('type') == 'tool_result' and any(_is_image(b) for b in _blocks(block)):
            return True
    return False


def _has_tool_result_images(messages: list[dict]) -> bool:
    return any(block.get('type') == 'tool_result' and any(_is_image(b) for b in _blocks(block))
               for m in messages if m.get('role') == 'user' for block in _blocks(m))


def strip_images(messages: list[dict]) -> list[dict]:
    """Messages for storage: image bytes survive only in the latest user message that has any."""
    keep = max((i for i, m in enumerate(messages) if m.get('role') == 'user' and _has_images(m)), default=-1)
    note = {'type': 'text', 'text': '[image removed from saved history]'}
    out = []
    for index, message in enumerate(messages):
        if index == keep or message.get('role') != 'user' or not _has_images(message):
            out.append(message)
            continue
        message = copy.deepcopy(message)
        content = []
        for block in message['content']:
            if isinstance(block, dict) and _is_image(block):
                content.append(dict(note))
            elif isinstance(block, dict) and block.get('type') == 'tool_result' and isinstance(block.get('content'), list):
                block['content'] = [dict(note) if isinstance(b, dict) and _is_image(b) else b for b in block['content']]
                content.append(block)
            else:
                content.append(block)
        message['content'] = content
        out.append(message)
    return out


def images_to_user_message(messages: list[dict]) -> list[dict]:
    """Move images out of tool results into the same user turn, after the results, each introduced by text naming the call.

    The tool_result blocks stay first (the API wants them first); the images follow in text-led groups.
    """
    names: dict[str, str] = {}
    out = []
    for message in messages:
        if message.get('role') == 'assistant':
            for block in _blocks(message):
                if block.get('type') == 'tool_use':
                    names[block.get('id', '')] = block.get('name', 'a tool')
        if message.get('role') != 'user' or not any(b.get('type') == 'tool_result' and any(_is_image(x) for x in _blocks(b)) for b in _blocks(message)):
            out.append(message)
            continue
        results, extras, others = [], [], []
        for block in message['content']:
            if not isinstance(block, dict) or block.get('type') != 'tool_result':
                others.append(block)
                continue
            inner = block.get('content')
            images = [b for b in inner if _is_image(b)] if isinstance(inner, list) else []
            if not images:
                results.append(block)
                continue
            texts = [b.get('text', '') for b in inner if b.get('type') == 'text']
            call_id = block.get('tool_use_id', '')
            note = '(the image from this call follows in this message)'
            results.append({**block, 'content': '\n'.join(t for t in texts + [note] if t)})
            extras.append({'type': 'text', 'text': f'Image returned by tool call {names.get(call_id, "a tool")} (tool_use_id {call_id}):'})
            extras.extend(images)
        out.append({**message, 'content': results + extras + others})
    return out


def close_dangling(messages: list[dict]) -> None:
    """A run that ended after the model asked for tools but before they ran leaves tool_use blocks without
    results, which the API refuses. Answer each with an error result so the conversation stays valid."""
    if messages and messages[-1].get('role') == 'assistant':
        calls = [b for b in _blocks(messages[-1]) if b.get('type') == 'tool_use']
        if calls:
            messages.append({'role': 'user', 'content': [
                {'type': 'tool_result', 'tool_use_id': c['id'], 'content': 'The run ended before this tool ran.', 'is_error': True} for c in calls]})


def add_user_text(messages: list[dict], text: str) -> None:
    block = {'type': 'text', 'text': text}
    if messages and messages[-1].get('role') == 'user':
        last = messages[-1]
        content = last['content'] if isinstance(last.get('content'), list) else [{'type': 'text', 'text': str(last.get('content') or '')}]
        messages[-1] = {**last, 'content': [*content, block]}
    else:
        messages.append({'role': 'user', 'content': [block]})


# ---- saved conversations -------------------------------------------------------------------

class SessionStore:
    """One JSON file per session id under <data dir>/muse_sessions/."""

    def __init__(self, data_dir: Path):
        self.root = Path(data_dir).expanduser() / 'muse_sessions'

    def path(self, session_id: str) -> Path:
        if not SESSION_ID.match(session_id or ''):
            raise MuseError('That Muse session id is not valid.')
        return self.root / f'{session_id}.json'

    def load(self, session_id: str) -> dict | None:
        path = self.path(session_id)
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise MuseError(f'The saved Muse conversation {session_id} could not be read: {type(exc).__name__}') from None
        if not isinstance(data, dict) or not isinstance(data.get('messages'), list):
            raise MuseError(f'The saved Muse conversation {session_id} is damaged.')
        return data

    def save(self, session_id: str, state: dict, messages: list[dict]) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.path(session_id)
        payload = {'version': STORE_VERSION, 'session_id': session_id, 'model': state.get('model'), 'image_mode': state.get('image_mode'),
                   'messages': strip_images(messages)}
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(payload, separators=(',', ':')), encoding='utf-8')
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)


# ---- one model response (plain or streamed) ------------------------------------------------

def _merge_usage(into: dict, new: Any) -> None:
    if not isinstance(new, dict):
        return
    for key, value in new.items():
        if key == 'output_tokens_details' and isinstance(value, dict):
            into.setdefault(key, {}).update({k: v for k, v in value.items() if v is not None})
        elif value is not None:
            into[key] = value


def usage_event(model: str, usage: dict) -> dict:
    out: dict[str, Any] = {'model': model}
    for key in ('input_tokens', 'output_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens'):
        if isinstance(usage.get(key), (int, float)) and not isinstance(usage.get(key), bool):
            out[key] = int(usage[key])
    details = usage.get('output_tokens_details')
    if isinstance(details, dict) and isinstance(details.get('thinking_tokens'), (int, float)):
        out['thinking_tokens'] = int(details['thinking_tokens'])
    return out


class _Turn:
    """Builds one assistant message from stream events, emitting text as it arrives."""

    FLUSH_AT = 48

    def __init__(self, emit):
        self.emit = emit
        self.blocks: dict[int, dict] = {}
        self.partial: dict[int, str] = {}
        self.bad_inputs: set[str] = set()
        self.usage: dict = {}
        self.stop_reason: str | None = None
        self.done = False
        self.buffer = ''

    async def flush(self) -> None:
        if self.buffer:
            text, self.buffer = self.buffer, ''
            await self.emit('text_delta', {'text': text})

    async def feed(self, event: dict) -> None:
        kind = event.get('type')
        if kind == 'message_start':
            _merge_usage(self.usage, (event.get('message') or {}).get('usage'))
        elif kind == 'content_block_start':
            block = dict(event.get('content_block') or {})
            if block.get('type') == 'text':
                block.setdefault('text', '')
            elif block.get('type') == 'thinking':
                block['thinking'] = block.get('thinking') or ''
            elif block.get('type') == 'tool_use':
                block['input'] = block.get('input') or {}
            index = int(event.get('index', len(self.blocks)))
            self.blocks[index] = block
            self.partial[index] = ''
        elif kind == 'content_block_delta':
            index = int(event.get('index', 0))
            block, delta = self.blocks.get(index), event.get('delta') or {}
            if block is None:
                return
            dtype = delta.get('type')
            if dtype == 'text_delta' and block.get('type') == 'text':
                block['text'] += delta.get('text', '')
                self.buffer += delta.get('text', '')
                if len(self.buffer) >= self.FLUSH_AT:
                    await self.flush()
            elif dtype == 'thinking_delta' and block.get('type') == 'thinking':
                block['thinking'] += delta.get('thinking', '')
            elif dtype == 'signature_delta' and block.get('type') == 'thinking':
                block['signature'] = (block.get('signature') or '') + delta.get('signature', '')
            elif dtype == 'input_json_delta' and block.get('type') == 'tool_use':
                self.partial[index] = self.partial.get(index, '') + delta.get('partial_json', '')
        elif kind == 'content_block_stop':
            index = int(event.get('index', 0))
            block = self.blocks.get(index)
            if block is None:
                return
            if block.get('type') == 'text':
                await self.flush()
            elif block.get('type') == 'thinking' and block.get('thinking', '').strip():
                await self.emit('thought', {'text': block['thinking']})
            elif block.get('type') == 'tool_use' and self.partial.get(index):
                try:
                    parsed = json.loads(self.partial[index])
                except ValueError:
                    parsed = None
                if isinstance(parsed, dict):
                    block['input'] = parsed
                else:
                    block['input'] = {}
                    self.bad_inputs.add(block.get('id', ''))
        elif kind == 'message_delta':
            delta = event.get('delta') or {}
            if delta.get('stop_reason'):
                self.stop_reason = delta['stop_reason']
            _merge_usage(self.usage, event.get('usage'))
        elif kind == 'message_stop':
            self.done = True

    async def finish(self) -> dict:
        await self.flush()
        if not self.done and not self.stop_reason:
            raise MuseStreamError('The Muse response stream ended before the message was complete.', retryable=True)
        content = [self.blocks[i] for i in sorted(self.blocks)]
        content = [b for b in content if not (b.get('type') == 'text' and not b.get('text'))]
        for block in content:
            if block.get('type') == 'thinking' and not block.get('signature'):
                block.pop('signature', None)         # a block that never received a signature is replayed without one
        return {'content': content, 'stop_reason': self.stop_reason, 'usage': self.usage, 'bad_inputs': self.bad_inputs}


# ---- the runner ----------------------------------------------------------------------------

class MuseRunner:
    def __init__(self, *, data_dir: Path | str | None = None, transport: httpx.AsyncBaseTransport | None = None,
                 base_url: str | None = None, max_tokens: int = MAX_TOKENS, model: str | None = None, effort: str | None = None,
                 sleep=None):
        self.data_dir = Path(data_dir) if data_dir else Path(os.environ.get('TRUSTPCB_DATA_DIR') or REPO_ROOT / '.trustpcb')
        self.transport, self.base_url, self.max_tokens = transport, base_url, max_tokens
        self.model, self.effort = model, effort
        self._sleep = sleep or asyncio.sleep
        self._cancel = asyncio.Event()
        self._active = False
        self._key = ''
        self.max_retries = MAX_RETRIES
        if model:
            self._check_model(model)

    # ---- configuration -------------------------------------------------------------------

    @staticmethod
    def _check_model(model: str) -> str:
        if 'contributor' in model.lower():
            raise MuseError(f'Refusing model {model}: contributor models allow training on user content. Use a standard model such as {DEFAULT_MODEL}.')
        return model

    def _target(self) -> dict:
        target = resolve_target()
        if target is None:
            raise MuseError('No Muse key is set. Add a Meta or Baseten API key in Settings, or set META_API_KEY, BASETEN_API_KEY or MODEL_API_KEY.')
        key = target['key']
        if not key.isascii() or any(c.isspace() for c in key):
            raise MuseError(f'{target["name"]} contains characters that cannot be sent as a header. Check that it was copied whole.')
        return target

    def _effort(self, request: RunRequest) -> str:
        effort = os.environ.get('TRUSTPCB_MUSE_EFFORT') or self.effort or (request.profile or {}).get('effort') or DEFAULT_EFFORT
        if effort not in EFFORTS:
            raise MuseError(f'TRUSTPCB_MUSE_EFFORT must be one of {", ".join(EFFORTS)}, not {effort!r}.')
        return effort

    def _clean(self, value: Any) -> str:
        return redact(value, (self._key,))

    # ---- cancellation ----------------------------------------------------------------------

    async def cancel(self) -> None:
        self._cancel.set()

    async def close(self) -> None:
        self._cancel.set()

    async def _guard(self, awaitable):
        """Await `awaitable`, giving way at once if cancel() is called."""
        task = asyncio.ensure_future(awaitable)
        waiter = asyncio.ensure_future(self._cancel.wait())
        try:
            done, _ = await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                return task.result()
            raise _Canceled()
        finally:
            waiter.cancel()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    # ---- run -------------------------------------------------------------------------------

    async def run(self, request: RunRequest, emit, interact) -> None:
        if self._active:
            raise RuntimeError('Runner already has an active run.')
        self._active = True
        self._cancel = asyncio.Event()
        self._key = ''
        try:
            await self._run(request, emit)
        except _Canceled:
            return
        except asyncio.CancelledError:
            raise
        except MuseError as exc:
            raise MuseError(self._clean(exc)[:1500]) from None
        except Exception as exc:
            raise MuseError(self._clean(f'{type(exc).__name__}: {exc}')[:1500]) from None
        finally:
            self._active = False

    def _timeout(self, stream: bool) -> httpx.Timeout:
        read = float(os.environ.get('TRUSTPCB_MUSE_TIMEOUT_S') or (180 if stream else 300))
        return httpx.Timeout(connect=15.0, read=read, write=30.0, pool=15.0)

    async def _run(self, request: RunRequest, emit) -> None:
        target = self._target()
        self._key = target['key']
        profile = request.profile or {}
        model = self._check_model(os.environ.get('TRUSTPCB_MUSE_MODEL') or self.model or target['model'] or profile.get('model') or DEFAULT_MODEL)
        # A named key goes only to its own host; TRUSTPCB_MUSE_BASE_URL steers MODEL_API_KEY alone.
        base_url = resolve_base_url(self.base_url) if target['base_url'] is None else resolve_base_url_for(target['base_url'], self.base_url)
        effort = self._effort(request)
        stream = (os.environ.get('TRUSTPCB_MUSE_STREAM') or '1') != '0'
        forced = os.environ.get('TRUSTPCB_MUSE_IMAGE_MODE') or None
        if forced is not None and forced not in IMAGE_MODES:
            raise MuseError(f'TRUSTPCB_MUSE_IMAGE_MODE must be one of {", ".join(IMAGE_MODES)}.')
        store = SessionStore(self.data_dir)
        session_id = request.session_id
        if session_id:
            state = store.load(session_id)
            if state is None:
                raise MuseError(f'Muse session {session_id} cannot be resumed: its saved conversation is missing.')
        else:
            session_id, state = str(uuid.uuid4()), {}
        messages: list[dict] = state.get('messages') or []
        image_mode = forced or (state.get('image_mode') if state.get('image_mode') in IMAGE_MODES else DEFAULT_IMAGE_MODE)
        state = {'model': model, 'image_mode': image_mode}
        await emit('session', {'session_id': session_id})
        close_dangling(messages)
        if image_mode == 'user_message':
            messages = images_to_user_message(messages)
        add_user_text(messages, request.user_text)
        system = [{'type': 'text', 'text': request.system_prompt}] if request.system_prompt else None
        max_turns = request.max_turns
        max_tokens = int(profile.get('max_tokens') or self.max_tokens)
        turns = nudges = 0
        headers = {'Authorization': f'Bearer {self._key}', 'Content-Type': 'application/json', 'User-Agent': 'trustpcb-muse/1'}

        async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=self._timeout(stream), transport=self.transport,
                                         follow_redirects=False) as client:
            try:
                while True:
                    if self._cancel.is_set():
                        raise _Canceled()
                    if max_turns is not None and turns >= max_turns:
                        await emit('status', {'state': 'max_turns', 'max_turns': max_turns, 'num_turns': turns})
                        return
                    specs = tool_specs(request.tools)          # read again every turn: the allowed set follows the phase
                    tools = [{'type': 'custom', 'name': s.name, 'description': s.description, 'input_schema': s.input_schema} for s in specs]
                    while True:
                        body = {'model': model, 'max_tokens': max_tokens, 'messages': messages,
                                'thinking': {'type': 'adaptive'}, 'output_config': {'effort': effort}}
                        if system:
                            body['system'] = system
                        if tools:
                            body['tools'], body['tool_choice'] = tools, {'type': 'auto'}
                        if stream:
                            body['stream'] = True
                        try:
                            response = await self._post(client, body, emit, stream)
                            break
                        except MuseHTTPError as exc:
                            if (exc.status == 400 and forced is None and image_mode == 'tool_result' and _has_tool_result_images(messages)
                                    and IMAGE_400.search(exc.message or '')):
                                image_mode = state['image_mode'] = 'user_message'
                                messages = images_to_user_message(messages)
                                await emit('status', {'state': 'image_fallback', 'message': 'Images in tool results were refused, so they now follow the results in the same turn.'})
                                continue
                            raise
                    turns += 1
                    await emit('usage', usage_event(model, response['usage']))
                    content = response['content']
                    if content:
                        messages.append({'role': 'assistant', 'content': content})
                    calls = [b for b in content if b.get('type') == 'tool_use']
                    stop = response['stop_reason']
                    if calls:
                        nudges = 0
                        try:
                            results = await self._run_tools(request, calls, response['bad_inputs'], emit, messages, image_mode)
                        finally:
                            store.save(session_id, state, messages)
                        messages.append({'role': 'user', 'content': results})
                        store.save(session_id, state, messages)
                        continue
                    if stop == 'tool_use':
                        raise MuseError('The model said it was calling a tool but sent no tool call.')
                    if stop == 'refusal':
                        raise MuseError('The model declined to continue (stop reason: refusal).')
                    if stop == 'max_tokens':
                        nudges += 1
                        if nudges > 2:
                            raise MuseError(f'The model hit the {max_tokens} token output limit three times in a row.')
                        add_user_text(messages, 'Your last reply was cut off by the output limit. Continue briefly with your next step.')
                        store.save(session_id, state, messages)
                        continue
                    store.save(session_id, state, messages)
                    return
            except BaseException:
                # Keep what was done so far so a follow-up can resume (a canceled run included).
                with contextlib.suppress(Exception):
                    close_dangling(messages)
                    store.save(session_id, state, messages)
                raise

    async def _run_tools(self, request, calls, bad_inputs, emit, messages, image_mode) -> list[dict]:
        results: list[dict] = []
        try:
            for call in calls:
                if self._cancel.is_set():
                    raise _Canceled()
                results.append(await self._run_tool(request, call, call.get('id', '') in bad_inputs, emit))
        except BaseException:
            # Answer the calls that did not run, so the saved conversation stays valid.
            for call in calls[len(results):]:
                results.append({'type': 'tool_result', 'tool_use_id': call['id'], 'content': 'The run was stopped before this tool ran.', 'is_error': True})
            messages.append({'role': 'user', 'content': results})
            raise
        if image_mode == 'user_message':
            converted = images_to_user_message([{'role': 'assistant', 'content': calls}, {'role': 'user', 'content': results}])
            return converted[1]['content']
        return results

    async def _run_tool(self, request, call: dict, bad_input: bool, emit) -> dict:
        call_id, name = call['id'], call.get('name', '')
        args = call.get('input') if isinstance(call.get('input'), dict) else {}
        await emit('tool_start', {'tool_call_id': call_id, 'name': name, 'input': args})
        if bad_input:
            payload = {'content': 'The tool arguments were not valid JSON. Send the call again with complete arguments.', 'is_error': True, 'images': []}
            images: list = []
        else:
            try:
                result = await self._guard(call_tool(request.tools, name, args))
                payload = result_payload(result)
                images = list(getattr(result, 'images', None) or [])
            except _Canceled:
                raise
            except asyncio.CancelledError:
                raise
            except KeyError:
                payload, images = {'content': f'There is no tool named {name}.', 'is_error': True, 'images': []}, []
            except Exception as exc:     # a tool failure is a result the model can act on
                payload, images = {'content': self._clean(f'{name} failed: {exc}')[:1500], 'is_error': True, 'images': []}, []
        # A tool must never carry the key back to the model or the event log.
        payload = json.loads(self._clean(json.dumps(payload, default=str)))
        text = payload['content'] if isinstance(payload['content'], str) else json.dumps(payload['content'], default=str)
        await emit('tool_result', {'tool_call_id': call_id, 'name': name, **payload})
        block: dict[str, Any] = {'type': 'tool_result', 'tool_use_id': call_id}
        if images:
            block['content'] = [{'type': 'text', 'text': text or '(image)'},
                                *({'type': 'image', 'source': {'type': 'base64', 'media_type': i.get('mime') or 'image/png', 'data': i['data_b64']}} for i in images)]
        else:
            block['content'] = text or '(no output)'
        if payload.get('is_error'):
            block['is_error'] = True
        return block

    # ---- HTTP ------------------------------------------------------------------------------

    async def _post(self, client: httpx.AsyncClient, body: dict, emit, stream: bool) -> dict:
        """One model response, retrying 429, 5xx, timeouts and dropped connections with backoff and jitter."""
        attempt = 0
        while True:
            try:
                return await self._guard(self._attempt(client, body, emit, stream))
            except _Canceled:
                raise
            except MuseHTTPError as exc:
                error, retryable, wait = exc, exc.retryable, exc.retry_after
            except MuseStreamError as exc:
                error, retryable, wait = exc, exc.retryable, None
            except httpx.TimeoutException:
                error, retryable, wait = MuseError('The Muse API timed out.'), True, None
            except httpx.TransportError as exc:
                error, retryable, wait = MuseError(f'The connection to the Muse API failed ({type(exc).__name__}).'), True, None
            if not retryable or attempt >= self.max_retries:
                raise error
            delay = min(30.0, 0.5 * 2 ** attempt) * random.uniform(0.5, 1.0)
            if wait:
                delay = max(delay, min(float(wait), 120.0))
            attempt += 1
            await emit('status', {'state': 'retrying', 'attempt': attempt, 'max_attempts': self.max_retries, 'wait_s': round(delay, 1), 'reason': self._clean(error)[:200]})
            await self._guard(self._sleep(delay))

    def _http_error(self, response: httpx.Response, raw: bytes) -> MuseHTTPError:
        error_type, message = None, ''
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
        if isinstance(data, dict):
            detail = data.get('error') if isinstance(data.get('error'), dict) else data
            error_type, message = detail.get('type'), str(detail.get('message') or '')
        if not message:
            message = raw[:300].decode('utf-8', 'replace')
        retry_after = None
        try:
            retry_after = float(response.headers.get('retry-after', ''))
        except ValueError:
            pass
        text = f'Muse API error {response.status_code}' + (f' ({error_type})' if error_type else '') + (f': {message}' if message else '')
        if response.status_code == 401:
            text += ' (check MODEL_API_KEY)'
        return MuseHTTPError(response.status_code, error_type, self._clean(text)[:1200], retry_after)

    async def _attempt(self, client: httpx.AsyncClient, body: dict, emit, stream: bool) -> dict:
        if not stream:
            response = await client.post(ENDPOINT, json=body)
            if response.status_code != 200:
                raise self._http_error(response, response.content)
            return await self._plain(response, emit)
        async with client.stream('POST', ENDPOINT, json=body) as response:
            if response.status_code != 200:
                raise self._http_error(response, await response.aread())
            if 'event-stream' not in response.headers.get('content-type', ''):
                return await self._plain(response, emit, await response.aread())
            turn = _Turn(emit)
            name, data = None, []

            async def dispatch():
                nonlocal name, data
                raw, event_name, data, name = '\n'.join(data), name, [], None
                if not raw or raw == '[DONE]':
                    return
                try:
                    event = json.loads(raw)
                except ValueError:
                    return
                if not isinstance(event, dict):
                    return
                event.setdefault('type', event_name)
                if event.get('type') == 'error':
                    detail = event.get('error') if isinstance(event.get('error'), dict) else {}
                    kind = detail.get('type')
                    raise MuseStreamError(self._clean(f'Muse stream error ({kind}): {detail.get("message", "")}')[:600], retryable=kind in RETRYABLE_STREAM_ERRORS)
                await turn.feed(event)

            async for line in response.aiter_lines():
                if line == '':
                    await dispatch()
                elif line.startswith(':'):
                    continue
                else:
                    field, _, value = line.partition(':')
                    value = value[1:] if value.startswith(' ') else value
                    if field == 'data':
                        data.append(value)
                    elif field == 'event':
                        name = value
            await dispatch()
            return await turn.finish()

    async def _plain(self, response: httpx.Response, emit, raw: bytes | None = None) -> dict:
        try:
            data = json.loads(raw if raw is not None else response.content)
        except ValueError:
            raise MuseError('The Muse API returned a response that is not JSON.') from None
        if not isinstance(data, dict) or not isinstance(data.get('content'), list):
            raise MuseError('The Muse API returned a response without content.')
        content = [b for b in data['content'] if isinstance(b, dict) and not (b.get('type') == 'text' and not b.get('text'))]
        for block in content:
            if block.get('type') == 'thinking' and (block.get('thinking') or '').strip():
                await emit('thought', {'text': block['thinking']})
            elif block.get('type') == 'text':
                await emit('text_delta', {'text': block.get('text') or ''})
        return {'content': content, 'stop_reason': data.get('stop_reason'), 'usage': data.get('usage') or {}, 'bad_inputs': set()}


def create_runner(profile: dict | None = None) -> MuseRunner:
    profile = profile or {}
    return MuseRunner(model=profile.get('model') or None)
