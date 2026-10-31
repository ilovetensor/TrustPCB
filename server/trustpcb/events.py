"""Event vocabulary and the Server-Sent Events stream.

The agent never talks to the UI: tools write to SQLite, append an ordered event, and the
stream below replays and follows that log. Reconnecting with `after=N` (or Last-Event-ID)
resumes exactly after sequence N.
"""
import asyncio
import json

# Review and follow-up events, plus the runner and session events.
REVIEW_EVENTS = frozenset({
    'phase_changed', 'budget', 'thought', 'candidate_batch', 'brief_ready', 'finding_upserted', 'marks_changed',
    'spotlight', 'investigation_updated', 'suspect_updated', 'sim_queued', 'sim_progress', 'sim_done', 'report_ready',
})
FOLLOWUP_EVENTS = frozenset({
    'followup_started', 'test_proposed', 'test_decided', 'whatif_ready', 'finding_changed', 'assumption_changed', 'report_revised',
})
RUNNER_EVENTS = frozenset({
    'user_message', 'text_delta', 'tool_start', 'tool_result', 'usage', 'status', 'session', 'capabilities',
    'permission', 'question', 'test', 'request_resolved',
})
TERMINAL_EVENTS = frozenset({'completed', 'failed', 'canceled'})
EVENT_TYPES = REVIEW_EVENTS | FOLLOWUP_EVENTS | RUNNER_EVENTS | TERMINAL_EVENTS

KEEPALIVE_SECONDS = 15.0
POLL_SECONDS = 0.25


def sse_frame(event: dict) -> str:
    return f"id: {event['sequence']}\ndata: {json.dumps(event)}\n\n"


async def sse_events(store, chat_id, after, disconnected, poll=POLL_SECONDS, keepalive=KEEPALIVE_SECONDS):
    """Yield SSE text: replay history after `after`, then follow new events until disconnect.

    `disconnected` is an async callable returning True once the client is gone.
    """
    yield ': connected\n\n'  # flush headers even when history is already caught up
    cursor = max(0, after)
    idle = 0.0
    while not await disconnected():
        batch = await asyncio.to_thread(store.events, chat_id, cursor, 500)
        for event in batch:
            cursor = event['sequence']
            yield sse_frame(event)
            idle = 0.0
        if batch:
            continue  # drain a long backlog without sleeping
        idle += poll
        if idle >= keepalive:
            yield ': keepalive\n\n'
            idle = 0.0
        await asyncio.sleep(poll)
