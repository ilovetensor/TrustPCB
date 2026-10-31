"""One active run per project: turn lock, durable events, cancel, and interactive waiters.

A "run" is any async job tied to a chat (a review or a follow-up thread). The review machine and
the follow-up handler live in `trustpcb.review`; they start work with `SessionManager.start`
and get a `TurnContext` to emit events, ask the user, and drive a runner.

    async def work(ctx: TurnContext):
        await ctx.emit('phase_changed', {...})
        await ctx.run_agent(RunRequest(...))          # or ctx.attach_runner(my_runner)

    await manager.start(chat_id, request_id, work, text='user text')

Rules enforced here:
  * one run at a time per project (SessionConflict, HTTP 409);
  * a repeated request_id on the same chat returns the original turn and starts nothing;
  * exactly one terminal event (completed | failed | canceled) per turn, written by the manager;
  * after cancel, further emits from the run are dropped;
  * a restart marks unfinished turns interrupted (Store.recover_interrupted).
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from .events import TERMINAL_EVENTS

logger = logging.getLogger('trustpcb')


class SessionConflict(ValueError):
    """A request conflicts with a live or expired session operation."""


INTERACTION_KINDS = ('permission', 'question', 'test')


@dataclass
class ActiveTurn:
    chat_id: str
    project_id: str
    turn_id: str
    task: asyncio.Task | None = None
    canceled: bool = False
    pending: set[str] = field(default_factory=set)
    runners: list[Any] = field(default_factory=list)


class TurnContext:
    """What a run sees: persistent emit, user interaction, and runner handling."""

    def __init__(self, manager: 'SessionManager', current: ActiveTurn):
        self.manager = manager
        self.store = manager.store
        self._current = current
        self.chat_id = current.chat_id
        self.project_id = current.project_id
        self.turn_id = current.turn_id
        self.final_payload: dict = {}  # merged into the terminal event

    @property
    def canceled(self) -> bool:
        return self._current.canceled

    async def emit(self, kind: str, payload: dict) -> None:
        if self._current.canceled:
            return
        if kind in TERMINAL_EVENTS:
            return  # the manager owns exactly one terminal event
        if kind == 'session' and payload.get('session_id'):
            self.store.update_chat(self.chat_id, session_id=payload['session_id'])
        self.store.append_event(self.chat_id, self.turn_id, kind, payload)

    async def interact(self, kind: str, payload: dict) -> dict:
        """Persist a request event, then wait for POST /api/requests/{id}/respond."""
        if kind not in INTERACTION_KINDS:
            raise ValueError(f'Unsupported interaction kind: {kind}')
        current = self._current
        if current.canceled:
            raise asyncio.CancelledError()
        request_id = uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.manager.requests[request_id] = current, kind, future
        current.pending.add(request_id)
        self.store.update_chat(self.chat_id, status=f'waiting_{kind}')
        await self.emit(kind, {**payload, 'request_id': request_id})
        try:
            result = await future
            if current.canceled:
                raise asyncio.CancelledError()
            self.store.update_chat(self.chat_id, status='running')
            return result
        finally:
            current.pending.discard(request_id)
            self.manager.requests.pop(request_id, None)

    def attach_runner(self, runner) -> None:
        """Register a runner so cancel() reaches it and it is closed when the turn ends."""
        self._current.runners.append(runner)

    async def run_agent(self, request) -> None:
        """Create the runner for request.profile, run it, and always close it."""
        runner = self.manager.make_runner(request.profile)
        self.attach_runner(runner)
        await runner.run(request, self.emit, self.interact)


class SessionManager:
    def __init__(self, store, runner_factory: Callable[[dict], Any] | None = None):
        self.store = store
        self.runner_factory = runner_factory
        self.turns: dict[str, ActiveTurn] = {}  # by project id
        self.requests: dict[str, tuple[ActiveTurn, str, asyncio.Future]] = {}
        self._lock = asyncio.Lock()

    @property
    def active(self) -> bool:
        return bool(self.turns)

    def active_for_project(self, project_id: str) -> ActiveTurn | None:
        return self.turns.get(project_id)

    def make_runner(self, profile: dict | None):
        if self.runner_factory is not None:
            return self.runner_factory(profile or {})
        from .runners import load_runner
        return load_runner(profile)

    async def start(
        self,
        chat_id: str,
        request_id: str,
        work: Callable[[TurnContext], Awaitable[None]],
        *,
        text: str = '',
        user_event: dict | None = None,
        meta: dict | None = None,
    ) -> dict:
        """Start `work` as the single active run of the chat's project; idempotent per request_id.

        `user_event`, when given, is stored as the turn's first `user_message` event payload
        (request_id is added). Returns {"turn_id", "duplicate"}.
        """
        async with self._lock:
            chat = self.store.get_chat(chat_id)
            if not chat:
                raise ValueError('Chat not found')
            if not request_id or len(request_id) > 200:
                raise ValueError('A valid request_id is required')
            existing = self.store.find_turn(chat_id, request_id)
            if existing:
                return {'turn_id': existing['id'], 'duplicate': True}
            project_id = chat['project_id']
            if project_id in self.turns:
                raise SessionConflict('A run is already active in this project')
            turn = self.store.create_turn(chat_id, request_id, text, meta)
            current = ActiveTurn(chat_id, project_id, turn['id'])
            self.turns[project_id] = current
            try:
                self.store.update_chat(chat_id, status='running')
                if user_event is not None:
                    self.store.append_event(chat_id, turn['id'], 'user_message', {**user_event, 'request_id': request_id})
                current.task = asyncio.create_task(self._execute(current, work))
            except BaseException:
                self.turns.pop(project_id, None)
                self.store.finish_turn(turn['id'], 'failed')
                raise
            return {'turn_id': turn['id'], 'duplicate': False}

    async def send(self, chat_id: str, text: str, request_id: str, build_request: Callable[[TurnContext], Awaitable[Any]], **kwargs) -> dict:
        """Convenience for chat-style turns: user message in, one runner run out.

        `build_request(ctx)` returns the RunRequest (system prompt, tools, budget) for this turn.
        """
        if not text.strip():
            raise ValueError('Write a message')

        async def work(ctx: TurnContext):
            await ctx.run_agent(await build_request(ctx))

        return await self.start(chat_id, request_id, work, text=text, user_event={'text': text, **kwargs.pop('user_event', {})}, **kwargs)

    async def _execute(self, current: ActiveTurn, work) -> None:
        ctx = TurnContext(self, current)
        status, payload = 'completed', {}
        try:
            if current.canceled:
                raise asyncio.CancelledError()
            await work(ctx)
        except asyncio.CancelledError:
            status = 'canceled'
        except Exception as exc:
            status, payload = 'failed', {'message': str(exc)[:2000] or type(exc).__name__}
        finally:
            if current.canceled:
                status, payload = 'canceled', {}
            for request_id in list(current.pending):
                pending = self.requests.pop(request_id, None)
                if pending and not pending[2].done():
                    pending[2].cancel()
            for runner in current.runners:
                try:
                    await runner.close()
                except BaseException:     # closing is best effort and must not stop the turn from finishing, even when cancelled
                    logger.warning('A runner did not close cleanly after the turn ended', exc_info=True)
            if status == 'completed':
                payload = {**ctx.final_payload}
            try:
                self.store.finish_turn(current.turn_id, status)
                self.store.update_chat(current.chat_id, status=status)
            finally:
                # Release the project before announcing the end, so a client that reacts
                # to the terminal event can start the next run at once.
                self.turns.pop(current.project_id, None)
            self.store.append_event(current.chat_id, current.turn_id, status, payload)

    async def respond(self, request_id: str, allow: bool | None = None, answers: dict | None = None) -> dict:
        entry = self.requests.get(request_id)
        if not entry or entry[0].canceled or entry[2].done():
            raise SessionConflict('This request has expired')
        current, kind, future = entry
        if kind in ('permission', 'test'):
            if not isinstance(allow, bool):
                raise ValueError('This request needs allow=true or false')
            self.store.append_event(current.chat_id, current.turn_id, 'request_resolved', {'request_id': request_id, 'kind': kind, 'allow': allow})
            future.set_result({'allow': allow})
        elif kind == 'question':
            if not isinstance(answers, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in answers.items()):
                raise ValueError('Question response needs a text answer map')
            self.store.append_event(current.chat_id, current.turn_id, 'request_resolved', {'request_id': request_id, 'kind': kind})
            future.set_result({'answers': answers})
        else:
            raise ValueError('Unsupported interactive request')
        return {'status': 'received'}

    async def cancel(self, chat_id: str) -> dict:
        current = next((turn for turn in self.turns.values() if turn.chat_id == chat_id), None)
        if not current:
            return {'status': 'idle'}
        current.canceled = True
        # Let a just-created task enter its try/finally; otherwise asyncio can cancel it
        # before cleanup ever becomes active.
        await asyncio.sleep(0)
        for request_id in list(current.pending):
            pending = self.requests.pop(request_id, None)
            if pending and not pending[2].done():
                pending[2].cancel()
        for runner in list(current.runners):
            try:
                await asyncio.wait_for(runner.cancel(), timeout=3)
            except (Exception, asyncio.CancelledError):
                pass
        if current.task and not current.task.done():
            current.task.cancel()
        if current.task:
            try:
                await asyncio.wait_for(asyncio.shield(current.task), timeout=5)
            except (Exception, asyncio.CancelledError):
                pass
        return {'status': 'canceled'}

    async def shutdown(self) -> None:
        for current in list(self.turns.values()):
            await self.cancel(current.chat_id)
