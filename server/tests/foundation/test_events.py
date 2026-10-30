import asyncio

from trustpcb.events import EVENT_TYPES, sse_events


def collect_disconnect_after(count):
    state = {'calls': 0}

    async def disconnected():
        state['calls'] += 1
        return state['calls'] > count
    return disconnected


async def drain(generator):
    return [item async for item in generator]


def parse(frames):
    out = []
    for frame in frames:
        if frame.startswith('id: '):
            lines = frame.strip().split('\n')
            out.append((int(lines[0][4:]), lines[1][6:]))
    return out


def test_spec_event_types_are_known():
    for name in ('phase_changed', 'finding_upserted', 'sim_done', 'report_ready', 'test_proposed', 'report_revised',
                 'text_delta', 'tool_start', 'tool_result', 'usage', 'completed', 'failed', 'canceled', 'status'):
        assert name in EVENT_TYPES


async def test_replay_all_then_resume_after_cursor(store, seeded):
    _, _, review = seeded
    chat = review['chat_id']
    for index in range(5):
        store.append_event(chat, None, 'budget', {'n': index})
    frames = await drain(sse_events(store, chat, 0, collect_disconnect_after(2), poll=0.001))
    assert frames[0] == ': connected\n\n'
    events = parse(frames)
    assert [e[0] for e in events] == sorted(e[0] for e in events) and len(events) == 5
    resumed = parse(await drain(sse_events(store, chat, events[2][0], collect_disconnect_after(2), poll=0.001)))
    assert [e[0] for e in resumed] == [events[3][0], events[4][0]]


async def test_follows_new_events_and_sends_keepalive(store, seeded):
    _, _, review = seeded
    chat = review['chat_id']

    async def writer():
        await asyncio.sleep(0.05)
        store.append_event(chat, None, 'thought', {'text': 'late'})

    task = asyncio.create_task(writer())
    frames = await drain(sse_events(store, chat, 0, collect_disconnect_after(40), poll=0.005, keepalive=0.02))
    await task
    assert any(f == ': keepalive\n\n' for f in frames)
    assert len(parse(frames)) == 1 and '"late"' in parse(frames)[0][1]


async def test_streams_are_per_chat(store, seeded):
    _, _, review = seeded
    thread = store.create_thread(review['id'])
    store.append_event(review['chat_id'], None, 'budget', {})
    store.append_event(thread['chat_id'], None, 'followup_started', {})
    only_thread = parse(await drain(sse_events(store, thread['chat_id'], 0, collect_disconnect_after(2), poll=0.001)))
    assert len(only_thread) == 1 and '"followup_started"' in only_thread[0][1]
