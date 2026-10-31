"""Runs owned by the server: turn lock, durable events, cancel, review slots and interactive waiters.

A "run" is any async job tied to a chat (a review or a follow-up thread). The review machine and
the follow-up handler live in `trustpcb.review`; they start work with `SessionManager.start`
and get a `TurnContext` to emit events, ask the user, and drive a runner.

    async def work(ctx: TurnContext):
        await ctx.emit('phase_changed', {...})
        await ctx.run_agent(RunRequest(...))          # or ctx.attach_runner(my_runner)

    await manager.start(chat_id, request_id, work, text='user text')

Rules enforced here:
  * one run at a time per chat; runs of different chats, in the same project or in others, go side by side;
  * at most `max_reviews` review runs work at once (TRUSTPCB_MAX_REVIEWS, default 3); a review that calls
    `ctx.wait_for_slot()` first waits in a visible queue (a `queued` event with its place) instead of failing;
  * a run belongs to the server, not to a browser: nothing a client does ends it except `cancel`;
  * a repeated request_id on the same chat returns the original turn and starts nothing;
  * exactly one terminal event (completed | failed | canceled) per turn, written by the manager;
  * after cancel, further emits from the run are dropped;
  * a restart marks unfinished turns interrupted (Store.recover_interrupted), and so does a stop of the
    app itself (`shutdown`): an app that stops is not a user pressing Stop.
"""
from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from .events import TERMINAL_EVENTS

logger = logging.getLogger('trustpcb')


class SessionConflict(ValueError):
    """A request conflicts with a live or expired session operation."""


INTERACTION_KINDS = ('permission', 'question', 'test')
DEFAULT_MAX_REVIEWS = 3
INTERRUPTED_MESSAGE = 'The app stopped while this run was in progress, so it was interrupted. Everything saved before that is kept; start the review again to continue from the stored simulations.'


def max_reviews_setting() -> int:
    """TRUSTPCB_MAX_REVIEWS: how many reviews work at once (default 3). A value that is not a whole number of at least 1 is ignored."""
    raw = os.environ.get('TRUSTPCB_MAX_REVIEWS', '').strip()
    if not raw:
        return DEFAULT_MAX_REVIEWS
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value < 1:
        logger.warning('TRUSTPCB_MAX_REVIEWS=%r is not a whole number of at least 1, so the default of %d is used.', raw, DEFAULT_MAX_REVIEWS)
        return DEFAULT_MAX_REVIEWS
    return value


@dataclass
class ActiveTurn:
    chat_id: str
    project_id: str
    turn_id: str
    kind: str = 'review'                # the chat's kind: review | thread
    task: asyncio.Task | None = None
    canceled: bool = False
    interrupted: bool = False           # stopped because the app is stopping, not because a user pressed Stop
    pending: set[str] = field(default_factory=set)
    runners: list[Any] = field(default_factory=list)
    slot: bool = False                  # holds one of the review slots
    queued: bool = False
    admission: asyncio.Future | None = None
    announced: int | None = None        # the queue place last written to the event log
    waiting_solver: int = 0             # simulations of this run waiting for the solver


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

    @property
    def interrupted(self) -> bool:
        """True when the run is ending because the app is stopping (not Stop): it must leave its state as it is and write no report."""
        return self._current.interrupted

    async def wait_for_slot(self) -> None:
        """Wait for one of the review slots (TRUSTPCB_MAX_REVIEWS). Returns at once when one is free; otherwise the run waits in the
        visible queue, in order. Raises CancelledError if the run is stopped while it waits."""
        await self.manager.acquire_slot(self._current)

    def set_solver_wait(self, waiting: bool) -> None:
        """Say that one of this run's simulations is waiting for (or has got) the solver, so lists can show 'Waiting for the solver'."""
        self._current.waiting_solver = max(0, self._current.waiting_solver + (1 if waiting else -1))

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
    def __init__(self, store, runner_factory: Callable[[dict], Any] | None = None, max_reviews: int | None = None):
        self.store = store
        self.runner_factory = runner_factory
        self.max_reviews = max(1, max_reviews) if max_reviews is not None else max_reviews_setting()
        self.turns: dict[str, ActiveTurn] = {}  # by chat id
        self.requests: dict[str, tuple[ActiveTurn, str, asyncio.Future]] = {}
        self.waiting: list[ActiveTurn] = []     # review runs waiting for a slot, in order
        self.stopping = False
        self._lock = asyncio.Lock()

    @property
    def active(self) -> bool:
        return bool(self.turns)

    def active_for_chat(self, chat_id: str) -> ActiveTurn | None:
        return self.turns.get(chat_id)

    def active_for_project(self, project_id: str) -> ActiveTurn | None:
        """Any live run of the project (a project can have several); None when it has none."""
        return next((turn for turn in self.turns.values() if turn.project_id == project_id), None)

    def working_reviews(self) -> int:
        return sum(1 for turn in self.turns.values() if turn.slot)

    def run_info(self, chat_id: str) -> dict | None:
        """What a list needs to show about a live run: running or queued, its place in the queue, and whether it waits for the solver."""
        turn = self.turns.get(chat_id)
        if turn is None:
            return None
        place = self.waiting.index(turn) + 1 if turn in self.waiting else None
        return {'state': 'queued' if turn.queued else 'running', 'position': place, 'waiting_for_solver': turn.waiting_solver > 0, 'turn_id': turn.turn_id}

    def make_runner(self, profile: dict | None):
        if self.runner_factory is not None:
            return self.runner_factory(profile or {})
        from .runners import load_runner
        return load_runner(profile)

    # ---- review slots -----------------------------------------------------------------------------

    async def acquire_slot(self, current: ActiveTurn) -> None:
        if current.canceled:
            raise asyncio.CancelledError()
        if current.slot:
            return
        if self.working_reviews() < self.max_reviews and not self.waiting:
            current.slot = True
            return
        current.queued = True
        current.admission = asyncio.get_running_loop().create_future()
        self.waiting.append(current)
        self.store.update_chat(current.chat_id, status='queued')
        self._announce_queue()
        try:
            await current.admission
        finally:
            current.queued = False
            if current in self.waiting:          # stopped while waiting
                self.waiting.remove(current)
                self._announce_queue()
        if not current.canceled:
            self.store.update_chat(current.chat_id, status='running')

    def _admit(self) -> None:
        """Hand free slots to the waiting reviews, first come first served, and tell the rest where they stand now."""
        while self.waiting and self.working_reviews() < self.max_reviews and not self.stopping:
            nxt = self.waiting.pop(0)
            nxt.slot = True
            if nxt.admission is not None and not nxt.admission.done():
                nxt.admission.set_result(None)
        self._announce_queue()

    def _announce_queue(self) -> None:
        for place, turn in enumerate(self.waiting, 1):
            if turn.announced == place or turn.canceled:
                continue
            turn.announced = place
            self.store.append_event(turn.chat_id, turn.turn_id, 'queued', {'position': place, 'ahead': place - 1 + self.working_reviews(), 'working': self.working_reviews(), 'limit': self.max_reviews,
                                                                          'message': f'Waiting for a free review slot: {self.working_reviews()} of {self.max_reviews} reviews are working, {place - 1} waiting ahead.'})

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
        """Start `work` as the single active run of the chat; idempotent per request_id.

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
            if self.stopping:
                raise SessionConflict('The app is shutting down; start this again once it is back.')
            project_id = chat['project_id']
            if chat_id in self.turns:
                raise SessionConflict('A run is already active in this chat')
            turn = self.store.create_turn(chat_id, request_id, text, meta)
            current = ActiveTurn(chat_id, project_id, turn['id'], kind=chat.get('kind') or 'review')
            self.turns[chat_id] = current
            try:
                self.store.update_chat(chat_id, status='running')
                if user_event is not None:
                    self.store.append_event(chat_id, turn['id'], 'user_message', {**user_event, 'request_id': request_id})
                current.task = asyncio.create_task(self._execute(current, work))
            except BaseException:
                self.turns.pop(chat_id, None)
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
            if current.interrupted:
                status, payload = 'interrupted', {'message': INTERRUPTED_MESSAGE, 'interrupted': True}
            elif current.canceled:
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
                self.store.close_unfinished_rows(current.chat_id, status)
            finally:
                # Release the chat (and its review slot) before announcing the end, so a client that reacts
                # to the terminal event can start the next run at once.
                self.turns.pop(current.chat_id, None)
                if current in self.waiting:
                    self.waiting.remove(current)
                if current.slot:
                    current.slot = False
                self._admit()
            # An interrupted run is announced as a failure that says so: the stream's terminal events stay completed | failed | canceled.
            self.store.append_event(current.chat_id, current.turn_id, 'failed' if status == 'interrupted' else status, payload)

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

    async def cancel(self, chat_id: str, *, interrupt: bool = False) -> dict:
        """Stop the run of a chat. The only thing that ends a run on purpose: a closed tab, a reload or a page change never gets here.

        `interrupt` is for the app stopping: the run ends as interrupted, not as stopped by the user."""
        current = self.turns.get(chat_id)
        if not current:
            return {'status': 'idle'}
        current.canceled = True
        current.interrupted = current.interrupted or interrupt
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
        return {'status': 'interrupted' if interrupt else 'canceled'}

    async def shutdown(self) -> None:
        """The app is stopping: end every run as interrupted. Their stored events, findings and simulations stay; no report is written."""
        self.stopping = True
        await asyncio.gather(*(self.cancel(current.chat_id, interrupt=True) for current in list(self.turns.values())), return_exceptions=True)
