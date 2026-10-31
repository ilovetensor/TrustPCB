"""Follow-up conversations after the report (spec section 17).

A message runs at the lowest level that can answer it: Answer (6 tool calls), Check (15) or Test
(approved by the user, with its own small budget). Every change to a finding goes through the same
checks as the original review, and a changed report becomes a new numbered revision at the end of
the run, never a silent edit.
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from .. import prompts
from ..runners.base import RunRequest
from . import evidence as ev
from . import report
from .budgets import FOLLOWUP_CALLS, FOLLOWUP_INPUT_TOKENS, FOLLOWUP_LEVELS, Clock, followup_config
from .findings import SEVERITIES, FindingRejected, check_numbers, check_wording
from .handlers import MARK_SCHEMA
from .lab import LabUnavailable
from .machine import PhaseMachine
from .session import GATE_REFUSALS, ReviewSession
from .toolset import ALL_LEVELS, CHECK_UP, FINDING_PROPS, TEST, AllHandlers, build_review_tools, define
from .tools import NUM, ToolDef, ToolRegistry, ToolResult, arr, fail, obj, ok, page, text

LEVEL_ORDER = {'answer': 0, 'check': 1, 'test': 2}
QUIET_RUNS_LIMIT = 2
MAX_TEST_PROPOSALS = 2
MIN_REPLY_CHARS = 20        # end_followup needs this much text for the user, streamed or given as reply

EDIT_REQUEST = re.compile(r"\b(fix (it|this|that|them|the board)( for me)?|export (the )?(fixed|corrected|updated|modified)|apply (the |this )?fix|"
                          r"edit (my|the) (board|design|layout)|change (my|the) (board|layout)|(send|give) me the (fixed|updated|corrected) (board|file|gerbers?)|make the (change|fix) for me)\b", re.I)
UNSUPPORTED = {
    'thermal': re.compile(r'\b(heat|thermal|temperature rise|hot spot|heatsink)\b', re.I),
    'crosstalk and signal integrity': re.compile(r'\b(crosstalk|signal integrity|eye diagram|differential pair simulation|usb simulation)\b', re.I),
    'power delivery': re.compile(r'\b(voltage drop|current capacity|ir drop|current flow)\b', re.I),
    'circuit simulation': re.compile(r'\b(ngspice|spice|circuit simulation)\b', re.I),
}
ADD_TO_REPORT = re.compile(r'\badd (it |this |that )?to (the )?report\b', re.I)
TOPICS = {
    'stackup': re.compile(r'material|stackup|substrate|dielectric|permittivity|epsilon|rogers|fr-?4|thickness|prepreg', re.I),
    'environment': re.compile(r'\bcase\b|enclosure|housing|metal|battery|shield|plastic', re.I),
    'band': re.compile(r'\bband\b|frequency|channel|\bghz\b|\bmhz\b', re.I),
    'target': re.compile(r'target|s11|return loss|matching goal', re.I),
}
LEVELS_OF = lambda definition: min(definition.levels, key=LEVEL_ORDER.get) if definition.levels else None


# ---- the hooks the API calls -----------------------------------------------------------------------

def start_thread(*, store, manager, review_id: str, body: dict) -> dict:
    review = store.get_review(review_id)
    if review is None:
        raise ValueError('Review not found')
    if review['status'] in ('queued', 'running') or not review.get('end_state'):
        raise ValueError('The review is still running; ask follow-up questions when it ends.')
    level = (body or {}).get('level') or 'answer'
    if level not in FOLLOWUP_LEVELS:
        raise ValueError('Level must be answer, check or test')
    thread = store.create_thread(review_id, level=level)
    if review.get('profile'):
        store.bind_chat_profile(thread['chat_id'], review['profile'])    # the profile stays fixed for the conversation
    return store.get_thread(thread['id'])


async def send_message(*, store, manager, thread_id: str, text: str, selected: list | None, request_id: str) -> dict:
    from . import service
    thread = store.get_thread(thread_id)
    if thread is None:
        raise ValueError('Thread not found')
    review = store.get_review(thread['review_id'])
    if review['status'] in ('queued', 'running'):
        raise ValueError('The review is still running; chat is off until it ends.')
    if not text.strip():
        raise ValueError('Write a message')
    limit = FOLLOWUP_INPUT_TOKENS
    used = thread['tokens_used'] or 0
    if used >= limit:
        raise ValueError('This conversation reached its token limit. Start a new conversation; it begins from a fresh digest.')
    run = FollowupRun(store, thread_id, service.get_lab(store), service.CONFIG.clock or Clock(), service.CONFIG.poll_s, text.strip(), list(selected or []))
    started = await manager.start(thread['chat_id'], request_id, run.work, text=text.strip(), user_event={'text': text.strip(), 'selected': list(selected or []), 'thread_id': thread_id})
    return {**started, 'thread_id': thread_id, 'near_limit': used >= 0.8 * limit}


# ---- digest and request hints ---------------------------------------------------------------------

def build_digest(store, review_id: str, limit: int = 6000) -> str:
    """A compact picture of the review for a new follow-up session: brief, assumptions, findings, investigations, not tested."""
    review = store.get_review(review_id)
    summary = review.get('summary') or {}
    snapshot = (store.latest_report_revision(review_id) or {}).get('snapshot') or {}
    lines = [f'Review {review["id"][:8]} of one board version, depth {review["depth"]}, ended as {review["end_state"]}. Latest report revision: r{snapshot.get("revision", 1)}.']
    brief = summary.get('brief') or {}
    if brief.get('purpose'):
        lines.append(f'Board purpose: {brief["purpose"]}')
    if brief.get('roles'):
        lines.append('Part roles: ' + '; '.join(f'{role}: {", ".join(refs[:8])}' for role, refs in list(brief['roles'].items())[:8]))
    lines.append('Assumptions: ' + ('; '.join(f'{a["name"]} = {json.dumps(a["value"]) if not isinstance(a["value"], str) else a["value"]} ({a["source"]})' for a in store.list_assumptions(review_id)) or 'none'))
    lines.append('Findings:')
    for f in store.list_findings(review_id):
        flag = f' [{f["status"]}]' if f['status'] != 'open' else ''
        lines.append(f'- {f["code"]} {f["severity"]} {f["class"]}{flag}: {f["title"]} (targets: {", ".join(sorted({t for m in f["marks"] for t in m["targets"]})[:4])})')
    for inv in store.list_investigations(review_id):
        lines.append(f'Investigation "{inv["question"][:120]}": {inv["status"]}; ' + '; '.join(f'{s["title"][:60]} = {s["status"]}' for s in store.list_suspects(inv['id'])))
    not_tested = snapshot.get('not_tested') or []
    if not_tested:
        lines.append('Not tested: ' + '; '.join(not_tested[:8]))
    text = '\n'.join(lines)
    return text[:limit]


def request_hints(text: str) -> tuple[list[str], list[str]]:
    """Server notes for the agent, and Not tested items to record, from the wording of the request."""
    notes, not_tested = [], []
    if EDIT_REQUEST.search(text):
        notes.append('The user asks you to edit the design. TrustPCB never edits a design or writes KiCad or manufacturing files. Say so plainly, and give the exact tested steps for the engineer to apply in their own tool.')
    for name, pattern in UNSUPPORTED.items():
        if pattern.search(text):
            notes.append(f'The user asks about {name}, which is not available yet. Say so and do not guess.')
            not_tested.append(f'{name.capitalize()} analysis is not available in this version of TrustPCB.')
    return notes, not_tested


def selected_lines(session: 'FollowupSession', selected: list) -> list[str]:
    out = []
    for item in selected[:50]:
        ident = str(item.get('id') or item.get('target') or item.get('code') or '')
        kind = item.get('type') or item.get('kind') or 'object'
        finding = session.store.get_finding_by_code(session.review_id, ident) if re.fullmatch(r'F-\d+', ident) else None
        if finding:
            out.append(f'- finding {finding["code"]}: {finding["title"]} [{finding["severity"]}, {finding["status"]}]')
            continue
        try:
            g = session.board.resolve(ident)
            out.append(f'- {kind} {ident} at x {g.center[0]:.2f} mm, y {g.center[1]:.2f} mm' if g.center else f'- {kind} {ident}')
        except Exception:
            out.append(f'- {kind} {ident} (not found on this board)')
    return out


# ---- the session --------------------------------------------------------------------------------------

class FollowupSession(ReviewSession):
    """A review session whose gates are levels, not phases."""
    mode = 'followup'

    def __init__(self, *, thread: dict, user_text: str, **kwargs):
        kwargs['config'] = followup_config('test')
        super().__init__(machine=PhaseMachine('standard', False), **kwargs)
        self.thread = thread
        self.user_text = user_text
        self.level = 'answer'
        self.test_approved = False
        self.test_proposals = 0
        self.changes: list[dict] = []
        self.new_not_tested: list[str] = []
        self.quiet_runs = int((thread.get('digest') or {}).get('quiet_runs', 0)) if isinstance(thread.get('digest'), dict) else 0
        self.evidence_made = 0
        self.speaking = False
        self.reply_chars = 0               # text streamed since the last working tool call: the visible reply

    @property
    def phase(self):                       # handlers and the budget code read this as "where are we"
        return self.level

    @property
    def budget_key(self):
        return 'followup'

    def calls_cap(self) -> int:
        return FOLLOWUP_CALLS[self.level]

    def allowed_names(self) -> list[str]:
        if self.finished:
            return [n for n, d in self.tools.items() if d.free]
        out = []
        for name, definition in self.tools.items():
            minimum = LEVELS_OF(definition)
            if minimum is None:
                continue
            if minimum == 'test' and not self.test_approved:
                continue
            out.append(name)
        return out

    def phase_label(self) -> str:
        return f'follow-up level {self.level}' + ('' if self.test_approved else '; simulation tools appear after propose_test is approved')

    def closing_names(self) -> list[str]:
        return ['get_status', 'end_followup']

    async def escalate(self, to: str) -> None:
        if LEVEL_ORDER[to] > LEVEL_ORDER[self.level]:
            self.level = to
            self.store.update_thread(self.thread['id'], level=to)
            await self.emit('followup_started', {'thread_id': self.thread['id'], 'level': to, 'escalated': True, 'selected': []})

    async def before(self, tool: ToolDef, args: dict) -> ToolResult | None:
        if self.finished and not tool.free:
            return fail('review_complete', 'The follow-up is finished.')
        minimum = LEVELS_OF(tool)
        if minimum in ('check', 'test') and self.quiet_runs >= QUIET_RUNS_LIMIT and not tool.free:
            return fail('no_further_evidence', 'Two earlier checks found nothing new. Reply that there is no further evidence, and offer a different question.')
        wall = self.budgets.global_exhausted()
        if wall and not tool.free and tool.name != 'end_followup':
            self.note_wall(wall)
            return fail('budget_exhausted', f'The {wall.replace("_", " ")} budget has ended. Answer with what you have.', budget=wall)
        if minimum == 'test' and not self.test_approved:
            return fail('approval_required', 'Simulation needs the user to approve a test first. Call propose_test.')
        target = self.level if minimum is None or LEVEL_ORDER[minimum] <= LEVEL_ORDER[self.level] else minimum
        if not tool.free and self.budgets.calls.get('followup', 0) >= FOLLOWUP_CALLS[target]:
            self.note_wall(f'{target}_tool_calls')
            nxt = {'answer': 'Use a Check-level tool to go further, or answer now.', 'check': 'Answer with what you have, or propose a test.', 'test': 'Answer with what you have.'}[target]
            return fail('budget_exhausted', f'The {target} level allows {FOLLOWUP_CALLS[target]} tool calls and they are used. {nxt}', budget='tool_calls', level=target)
        if tool.image and self.budgets.images_left('followup') <= 0:
            return fail('budget_exhausted', 'The image budget has ended.', budget='images')
        if minimum and LEVEL_ORDER[minimum] > LEVEL_ORDER[self.level] and tool.name not in ('propose_test',):
            await self.escalate(minimum)
        elif tool.name == 'propose_test':
            await self.escalate('check')
        return None

    async def after(self, tool: ToolDef, args: dict, result: ToolResult) -> None:
        self.last_progress = self.clock.now()
        code = (result.structured or {}).get('error') if result.is_error else None
        if code in GATE_REFUSALS:
            self.budgets.refused += 1
        elif not tool.free:
            self.budgets.add_call('followup')
        self.tool_calls += 1
        self.refusal_streak = self.refusal_streak + 1 if result.is_error else 0
        if self.refusal_streak >= 6:
            self.add_limit('loop', 'Six refused tool calls in a row')
            self.request_stop('loop', 'six refused tool calls in a row')
        if self.budgets.deadline_reached() and self.stop_reason is None:
            self.add_limit('hard_deadline', 'The follow-up wall-clock budget ended')
            self.request_stop('deadline', 'wall-clock budget ended')

    async def emit_budget(self) -> None:
        return None

    async def announce_phase(self, change) -> None:  # pragma: no cover - follow-ups have no phases
        return None


# ---- tools only the follow-up has ----------------------------------------------------------------------

def geometry_shape(g: dict):
    """A shapely shape for a resolved geometry dict (polygons, buffered polylines and points, groups)."""
    from shapely.geometry import LineString, Point, Polygon
    from shapely.ops import unary_union
    kind = g.get('kind')
    if kind == 'group':
        return unary_union([geometry_shape(p) for p in g.get('parts') or []])
    if kind == 'polygon':
        shapes = [Polygon(r) for r in g.get('rings') or [] if len(r) >= 3]
        return unary_union([s if s.is_valid else s.buffer(0) for s in shapes])
    if kind == 'polyline':
        return LineString(g['points']).buffer((g.get('width') or 0.2) / 2)
    x, y = g['points'][0]
    return Point(x, y).buffer((g.get('width') or 0.1) / 2)


class FollowupHandlers(AllHandlers):
    # ---- answers ---------------------------------------------------------------------------
    async def get_status(self, args: dict) -> ToolResult:
        s = self.s
        return ok({'level': s.level, 'allowed_tools': s.allowed_names(), 'tool_calls_used': s.budgets.calls.get('followup', 0), 'tool_call_limit_at_level': FOLLOWUP_CALLS[s.level],
                   'test_approved': s.test_approved, 'time_left_s': round(s.budgets.wall_left()), 'findings': len(self.store.list_findings(s.review_id)),
                   'no_further_evidence': s.quiet_runs >= QUIET_RUNS_LIMIT})

    def _finding(self, code: str) -> dict | None:
        return self.store.get_finding_by_code(self.s.review_id, code)

    async def get_finding(self, args: dict) -> ToolResult:
        finding = self._finding(args['code'])
        if finding is None:
            return fail('unknown_finding', f'No finding {args["code"]}.')
        public = self.s.engine.public(finding)
        history = [{'source': h['source'], 'reason': h['reason'], 'at': h['created_at']} for h in self.store.finding_history(finding['id'])][-6:]
        return ok({**{k: v for k, v in public.items() if k != 'marks'}, 'targets': sorted({t for m in finding['marks'] for t in m['targets']})[:12], 'history': history})

    async def get_evidence(self, args: dict) -> ToolResult:
        row = self.s.engine.row_by_code(args['code'])
        if row is None:
            return fail('unknown_evidence', f'No evidence {args["code"]}.')
        values = {k: v for k, v in (row['values'] or {}).items() if k not in ('curve', 'run', 'metrics')}
        return ok({'code': row['code'], 'type': row['type'], 'source_tool': row['source_tool'], 'values': values})

    async def search_findings(self, args: dict) -> ToolResult:
        query = (args.get('query') or '').lower()
        rows = [f for f in self.store.list_findings(self.s.review_id) if (not args.get('status') or f['status'] == args['status']) and
                (not args.get('severity') or f['severity'] == args['severity']) and
                (not query or query in f['title'].lower() or query in (f['what'] or '').lower() or query in f['fingerprint'].lower() or
                 any(query in t.lower() for m in f['marks'] for t in m['targets']))]
        cards = [{'code': f['code'], 'severity': f['severity'], 'class': f['class'], 'status': f['status'], 'title': f['title']} for f in rows]
        return ok(page(cards, args.get('page', 1), 10))

    # ---- checks ----------------------------------------------------------------------------
    def _add_measured(self, kind: str, source: str, values: dict) -> dict:
        row = self.store.add_evidence(self.s.review_id, kind, source, values)
        self.s.evidence_made += 1
        return row

    async def run_analyzer(self, args: dict) -> ToolResult:
        s = self.s
        for target in args.get('targets') or []:
            try:
                s.engine.resolve_target(target)
            except FindingRejected as exc:
                return fail(exc.code, exc.message)
        try:
            found = await s.lab.run_analyzer(version_id=s.version_id, rule=args['rule'], targets=args.get('targets'), params=args.get('params'))
        except LabUnavailable as exc:
            return fail('unavailable', str(exc))
        out = []
        for candidate in found[:20]:
            cid = candidate['id']
            if cid in s.candidates and s.candidates[cid] != candidate:
                cid = f'{cid}:f{len(s.candidates)}'
                candidate = {**candidate, 'id': cid}
            if cid not in s.candidates:
                s.candidates[cid] = candidate
                self.store.add_candidate(s.review_id, candidate, 'followup')
            row = s.engine.candidate_row(candidate)
            out.append({**self.candidate_card(candidate), 'evidence': row['code']})
        if not out:
            return ok({'results': [], 'note': 'The analyzer found nothing for this request.'})
        return ok({'results': out, 'note': 'Use candidate ids or evidence codes with propose_finding_change.'})

    async def measure(self, args: dict) -> ToolResult:
        s = self.s
        kind = args['kind']
        if kind == 'distance':
            if not args.get('b'):
                return fail('invalid_arguments', 'Distance needs both a and b.')
            try:
                ga, gb = s.engine.resolve_target(args['a']), s.engine.resolve_target(args['b'])
            except FindingRejected as exc:
                return fail(exc.code, exc.message)
            sa, sb = geometry_shape(ga), geometry_shape(gb)
            gap = float(sa.distance(sb))
            centers = ((ga['center'][0] - gb['center'][0]) ** 2 + (ga['center'][1] - gb['center'][1]) ** 2) ** 0.5
            values = {'kind': 'measured', 'title': f'Distance between {args["a"]} and {args["b"]}', 'targets': [args['a'], args['b']],
                      'measurements': {'edge_to_edge_distance': {'value': round(gap, 3), 'unit': 'mm'}, 'centre_distance': {'value': round(centers, 3), 'unit': 'mm'}}}
            row = self._add_measured(ev.MEASUREMENT, 'measure', values)
            return ok({'evidence': row['code'], 'edge_to_edge_mm': round(gap, 3), 'centre_to_centre_mm': round(centers, 3),
                       'mark': {'type': 'link', 'targets': [args['a'], args['b']], 'value': {'value': round(gap, 3), 'unit': 'mm'}}})
        name = args['a'].removeprefix('net:')
        if name not in s.board.net_names():
            return fail('anchor_unresolved', f'Net {args["a"]} does not exist on this board.')
        items = s.board.items_on_net(name)
        length = sum(t.length for t in items['tracks'])
        values = {'kind': 'measured', 'title': f'Routed length of {name}', 'targets': [f'net:{name}'],
                  'measurements': {'routed_length': {'value': round(length, 3), 'unit': 'mm'}}, 'details': [f'{len(items["tracks"])} segments, {len(items["vias"])} vias']}
        row = self._add_measured(ev.MEASUREMENT, 'measure', values)
        return ok({'evidence': row['code'], 'net': name, 'routed_length_mm': round(length, 3), 'segments': len(items['tracks']), 'vias': len(items['vias'])})

    async def try_placement(self, args: dict) -> ToolResult:
        s = self.s
        if s.board.footprint(args['ref']) is None:
            return fail('unknown_part', f'Part {args["ref"]} is not on this board.')
        target = args['near'] if args.get('near') else args.get('position')
        if target is None:
            return fail('invalid_arguments', 'Give near (a part, a pad id such as pad:U3:E24, or a net id) or position [x, y].')
        try:
            out = await s.lab.try_placement(version_id=s.version_id, ref=args['ref'], target=target, rotation=args.get('rotation'))
        except LabUnavailable as exc:
            return fail('unavailable', str(exc))
        values = {'kind': 'whatif', 'title': f'What-if placement of {args["ref"]}', 'label': 'What-if, not your board', 'result': {k: v for k, v in out.items() if k != 'ghost'}}
        row = self.store.add_evidence(s.review_id, ev.PLACEMENT, 'try_placement', values)
        await s.emit('whatif_ready', {'ref': args['ref'], 'ghost': out.get('ghost'), 'new_position': out.get('new_position'), 'distances': out.get('distances'),
                                      'conflicts': out.get('conflicts'), 'label': 'What-if, not your board', 'evidence': row['code']})
        return ok({**{k: v for k, v in out.items() if k != 'ghost'}, 'evidence': row['code'], 'label': 'What-if, not your board',
                   'rf_effect': 'Not tested: V1 simulates copper, not moved parts. Offer a Test with clear_copper_region if copper near the antenna is the question.'})

    # ---- tests -----------------------------------------------------------------------------
    async def propose_test(self, args: dict) -> ToolResult:
        s = self.s
        if not s.rf.get('simulatable'):
            return fail('rf_not_simulatable', s.rf.get('reason') or 'Nothing on this board can be simulated.')
        if s.test_approved:
            return ok({'approved': True, 'note': 'A test is already approved in this message.'})
        if s.test_proposals >= MAX_TEST_PROPOSALS:
            return fail('test_declined', 'The user already declined. Do not ask again; answer with what you have.')
        s.test_proposals += 1
        payload = {'thread_id': s.thread['id'], 'title': args['title'], 'why': args['why'], 'estimated_seconds': args['estimated_seconds'],
                   'plan': args.get('plan'), 'limits': {'investigations': 1, 'experiments': 3, 'confirm_runs': 2, 'solver_minutes': 15}}
        await s.emit('test_proposed', payload)
        waited = s.clock.now()
        async with s.solving():
            answer = await s.ctx.interact('test', payload)
        s.budgets.started += s.clock.now() - waited      # time spent waiting for the user is not the run's time
        allow = bool(answer.get('allow'))
        await s.emit('test_decided', {'thread_id': s.thread['id'], 'allow': allow, 'title': args['title']})
        if not allow:
            return fail('test_declined', 'The user chose Not now. Do not simulate. Answer with what you already know and say what the test would have shown.')
        s.test_approved = True
        await s.escalate('test')
        return ok({'approved': True, 'limits': payload['limits'], 'next': 'Open one investigation, run the base first, then at most three experiments.'})

    # ---- changes ---------------------------------------------------------------------------
    def _topic(self, name: str, value: Any, note: str | None) -> str:
        blob = f'{name} {value if isinstance(value, str) else json.dumps(value)} {note or ""}'
        return next((topic for topic, pattern in TOPICS.items() if pattern.search(blob)), 'other')

    def _dependents(self, topic: str) -> list[dict]:
        rows = self.store.list_evidence(self.s.review_id)
        out = []
        for row in rows:
            values = row['values'] or {}
            rule = str(values.get('rule') or '')
            simulated = values.get('kind') == 'simulated'
            if (topic in ('stackup', 'environment', 'band', 'target') and simulated) or \
               (topic == 'stackup' and rule.startswith('impedance')) or (topic == 'environment' and rule.startswith(('antenna', 'rf_path'))):
                out.append(row)
        return out

    async def record_assumption(self, args: dict) -> ToolResult:
        s = self.s
        name = args['name'].strip().lower().replace(' ', '_')
        value = args['value']
        topic = self._topic(name, value, args.get('note'))
        dependents = self._dependents(topic)
        existing = next((a for a in self.store.list_assumptions(s.review_id) if a['name'] == name), None)
        evidence_ids = [r['id'] for r in dependents]
        if existing:
            assumption = self.store.update_assumption(existing['id'], value=value, source='user_provided', note=args.get('note'),
                                                      dependent_evidence_ids=list(dict.fromkeys((existing.get('dependent_evidence_ids') or []) + evidence_ids)))
        else:
            assumption = self.store.add_assumption(s.review_id, name, value, 'user_provided', evidence_ids, args.get('note'), thread_id=s.thread['id'])
        affected = []
        waiting = []
        for finding in self.store.list_findings(s.review_id):
            if finding['status'] not in ('open', 'needs_recheck') or not set(finding['evidence_ids']) & set(evidence_ids):
                continue
            rows = [self.store.get_evidence(i) for i in finding['evidence_ids']]
            simulated = any(r and (r['values'] or {}).get('kind') == 'simulated' for r in rows)
            updated = self.store.update_finding(finding['id'], 'followup', f'assumption changed: {name}', status='needs_recheck', needs_recheck=1)
            if simulated:
                waiting.append(updated['code'])
            elif await self._recheck_by_code(updated, rows):
                updated = self.store.update_finding(updated['id'], 'followup', f'rechecked by code after assumption {name} changed: result unchanged', status='open', needs_recheck=0)
            affected.append(updated)
            await self.publish_finding(updated, 'finding_changed', 'followup')
        s.changes.append({'kind': 'assumption', 'name': name, 'affected': [f['code'] for f in affected]})
        await s.emit('assumption_changed', {'assumption': {'id': assumption['id'], 'name': name, 'value': value, 'source': 'user_provided'}, 'topic': topic,
                                            'affected': [{'code': f['code'], 'status': f['status']} for f in affected]})
        return ok({'recorded': name, 'source': 'user_provided', 'topic': topic, 'marked_needs_recheck': [f['code'] for f in affected if f['status'] == 'needs_recheck'],
                   'rechecked_by_code': [f['code'] for f in affected if f['status'] == 'open'], 'waiting_for_an_approved_test': waiting,
                   'note': 'A user statement is recorded as User provided, not as proven.'})

    async def _recheck_by_code(self, finding: dict, rows: list[dict]) -> bool:
        """Re-run the analyzer behind a measured finding. True when it reproduces within 5 percent."""
        s = self.s
        candidate_rows = [r for r in rows if r and r['type'] == ev.CANDIDATE]
        if not candidate_rows or len(candidate_rows) != len(rows):
            return False
        try:
            for row in candidate_rows:
                values = row['values']
                found = await s.lab.run_analyzer(version_id=s.version_id, rule=values.get('rule') or '', targets=values.get('targets'), params=None)
                match = next((c for c in found if c.get('targets') == values.get('targets')), None)
                if match is None:
                    return False
                for name, item in (values.get('measurements') or {}).items():
                    new = (match.get('measurements') or {}).get(name)
                    if not new or abs(new['value'] - item['value']) > 0.05 * max(abs(item['value']), 1e-9):
                        return False
            return True
        except Exception:
            return False

    async def propose_finding_change(self, args: dict) -> ToolResult:
        s = self.s
        action = args['action']
        thread_id = s.thread['id']
        if action in ('update', 'supersede', 'dismiss') and not args.get('finding_code'):
            return fail('invalid_arguments', f'{action} needs finding_code.')
        old = self._finding(args['finding_code']) if args.get('finding_code') else None
        if args.get('finding_code') and old is None:
            return fail('unknown_finding', f'No finding {args["finding_code"]}.')
        try:
            if action == 'dismiss':
                if old['status'] == 'dismissed':
                    return fail('already_dismissed', f'{old["code"]} is already dismissed.')
                updated = self.store.update_finding(old['id'], 'user', args['reason'], status='dismissed', dismissed_reason=args['reason'].strip(), needs_recheck=0)
                change = {'kind': 'finding_dismissed', 'code': updated['code']}
            elif action == 'update':
                payload = {k: v for k, v in args.items() if k in FINDING_PROPS or k in ('reason',)} | {'update': old['code']}
                out = s.engine.record(payload, candidates=s.candidates, source='followup', thread_id=thread_id, reason=args.get('reason') or 'updated in follow-up')
                updated, change = out['finding'], {'kind': 'finding_updated', 'code': old['code']}
            else:   # new or supersede
                payload = {k: v for k, v in args.items() if k in FINDING_PROPS}
                missing = [k for k in ('title', 'severity', 'what', 'why_it_matters', 'recommendation') if not payload.get(k)]
                if missing:
                    return fail('missing_fields', f'These fields are required: {", ".join(missing)}.', fields=missing)
                prepared = s.engine.prepare(payload, candidates=s.candidates)
                if prepared.class_ == 'opinion' and not (args.get('opinion') and ADD_TO_REPORT.search(s.user_text)):
                    return fail('opinion_stays_in_chat', 'Without evidence this stays in the chat. If the user asks to add it to the report, set opinion to true; it is then recorded as Engineering opinion.')
                if action == 'supersede':
                    if old['status'] == 'superseded':
                        return fail('finding_closed', f'{old["code"]} was already superseded.')
                    self.store.update_finding(old['id'], 'followup', args.get('reason') or 'superseded in follow-up', status='superseded')
                out = s.engine.record(payload, candidates=s.candidates, source='followup', thread_id=thread_id, reason=args.get('reason') or 'found in follow-up')
                updated = out['finding']
                change = {'kind': 'finding_new', 'code': updated['code']}
                if action == 'supersede':
                    old = self.store.update_finding(old['id'], 'followup', f'superseded by {updated["code"]}', status='superseded', superseded_by=updated['id'])
                    change = {'kind': 'finding_superseded', 'code': old['code'], 'replacement': updated['code']}
                    await self.publish_finding(old, 'finding_changed', 'followup')
                for cid in payload.get('candidate_ids') or []:
                    self.store.set_candidate_state(s.review_id, cid, 'recorded')
        except FindingRejected as exc:
            return fail(exc.code, exc.message, **exc.data)
        s.changes.append(change)
        await self.publish_finding(updated, 'finding_changed', 'followup')
        return ok({'code': updated['code'], 'action': action, 'status': updated['status'], 'class': updated['class'], 'severity': updated['severity'],
                   'found_in_followup': updated.get('source') == 'followup', 'note': 'A new report revision is created when this run ends.'})

    async def end_followup(self, args: dict) -> ToolResult:
        """Finish the reply. The user must have something to read: text streamed after the last working tool call, or a `reply` given here
        (checked like other follow-up prose, then streamed as the reply). A second call is a no-op."""
        s = self.s
        if s.finished:
            return ok({'ended': True, 'already_ended': True})
        reply = (args.get('reply') or '').strip()
        if s.reply_chars < MIN_REPLY_CHARS:
            if len(reply) < MIN_REPLY_CHARS:
                return fail('reply_required', f'The user has not seen an answer yet. Call end_followup again with reply: your answer in a few sentences, stating the results with the '
                            f'measurements from the tools (at least {MIN_REPLY_CHARS} characters).')
            allowed = [q for row in s.engine.evidence_rows() for q in ev.quantities_of(row)] + s.context()
            try:
                check_wording({'reply': reply})
                check_numbers({'reply': reply}, allowed, compare_only=True)
            except FindingRejected as exc:
                return fail(exc.code, exc.message + ' Fix the reply and call end_followup again.', **exc.data)
            await s.emit('text_delta', {'text': reply})
            s.reply_chars += len(reply)
        s.mark_finished()
        return ok({'ended': True, 'reason': args.get('reason')})


def build_followup_tools(h: FollowupHandlers) -> list[ToolDef]:
    base = {d.name: d for d in build_review_tools(h)}
    pick = lambda name: base[name]
    tools = [
        define('get_status', 'Current level, allowed tools and what is left.', obj({}), h.get_status, levels=ALL_LEVELS, free=True, closing=True,
               trail=lambda a: 'Checking follow-up status'),
        define('get_finding', 'Read one finding with its evidence codes and history.', obj({'code': text(1, 12)}, ['code']), h.get_finding, levels=ALL_LEVELS,
               trail=lambda a: f'Reading finding {a.get("code")}'),
        define('get_evidence', 'Read one piece of evidence by code.', obj({'code': text(1, 12)}, ['code']), h.get_evidence, levels=ALL_LEVELS,
               trail=lambda a: f'Reading evidence {a.get("code")}'),
        define('search_findings', 'Search the saved findings by text, status or severity.',
               obj({'query': text(1, 80), 'status': {'type': 'string', 'enum': ['open', 'needs_recheck', 'dismissed', 'superseded']},
                    'severity': {'type': 'string', 'enum': list(SEVERITIES)}, 'page': {'type': 'integer', 'minimum': 1, 'maximum': 100}}), h.search_findings,
               levels=ALL_LEVELS, trail=lambda a: 'Searching findings'),
        _with_levels(pick('read_schematic'), ALL_LEVELS), _with_levels(pick('read_datasheet'), ALL_LEVELS), _with_levels(pick('view_board'), ALL_LEVELS),
        _with_levels(pick('show_on_board'), ALL_LEVELS),
        _with_levels(pick('list_candidates'), CHECK_UP),
        define('run_analyzer', 'Run one analyzer rule again, optionally on specific targets. Returns measurements and evidence codes.',
               obj({'rule': text(1, 60), 'targets': arr(text(1, 80), 0, 20), 'params': {'type': 'object', 'additionalProperties': NUM}}, ['rule']), h.run_analyzer,
               levels=CHECK_UP, trail=lambda a: f'Running the {a.get("rule")} check'),
        define('measure', 'Measure on the board. kind distance: edge-to-edge and centre distance between targets a and b. kind net_length: routed length of net a.',
               obj({'kind': {'type': 'string', 'enum': ['distance', 'net_length']}, 'a': text(1, 80), 'b': text(1, 80)}, ['kind', 'a']), h.measure,
               levels=CHECK_UP, trail=lambda a: f'Measuring {a.get("kind", "").replace("_", " ")} of {a.get("a")}' + (f' and {a["b"]}' if a.get('b') else '')),
        define('try_placement', 'Placement what-if on a ghost copy: new distances, overlaps and conflicts. near is a part (C12), a pad (pad:U3:E24) or a net (net:ANT_FEED); '
               'position is [x, y]. Never changes the board; the RF effect is Not tested.',
               obj({'ref': text(1, 20), 'near': text(1, 80), 'position': arr(NUM, 2, 2), 'rotation': {'type': 'number', 'minimum': -360, 'maximum': 360}}, ['ref']), h.try_placement,
               levels=CHECK_UP, trail=lambda a: f'Trying {a.get("ref")} near {a.get("near") or a.get("position")} as a what-if'),
        _with_levels(pick('tune_matching'), CHECK_UP),
        define('propose_test', 'Ask the user to approve a simulation test. Nothing simulates without their click. Gives what will run, why and the estimated time.',
               obj({'title': text(5, 160), 'why': text(10, 600), 'estimated_seconds': {'type': 'integer', 'minimum': 10, 'maximum': 3600}, 'plan': text(5, 600)},
                   ['title', 'why', 'estimated_seconds']), h.propose_test, levels=CHECK_UP, trail=lambda a: f'Proposing a test: {a.get("title", "")[:100]}'),
        _with_levels(pick('open_investigation'), TEST), _with_levels(pick('run_simulation'), TEST), _with_levels(pick('view_result'), TEST),
        _with_levels(pick('compare_results'), TEST), _with_levels(pick('conclude_suspect'), TEST), _with_levels(pick('close_investigation'), TEST),
        _with_levels(pick('confirm_result'), TEST),
        define('record_assumption', 'Record a fact the user stated. It is stored as User provided and marks dependent findings as needing a recheck.',
               obj({'name': text(2, 60), 'value': {'type': 'string', 'minLength': 1, 'maxLength': 200}, 'note': text(1, 300)}, ['name', 'value']), h.record_assumption,
               levels=ALL_LEVELS, trail=lambda a: f'Recording a user-provided fact: {a.get("name")}'),
        define('propose_finding_change', 'Create (new), change (update), replace (supersede) or dismiss a finding. Same checks as the review: evidence, anchors, numbers.',
               obj(FINDING_PROPS | {'action': {'type': 'string', 'enum': ['new', 'update', 'supersede', 'dismiss']}, 'finding_code': text(1, 12), 'reason': text(3, 600), 'opinion': {'type': 'boolean'}},
                   ['action']), h.propose_finding_change, levels=ALL_LEVELS, trail=lambda a: f'Proposing to {a.get("action")} a finding' + (f' ({a["finding_code"]})' if a.get('finding_code') else '')),
        define('end_followup', 'Finish the reply. Call when the answer is complete. If you wrote no answer text since your last tool call, pass reply: the answer, with the measurements, in a few sentences.', obj({'reason': text(1, 300), 'reply': text(1, 2400)}), h.end_followup, levels=ALL_LEVELS, free=True, closing=True,
               trail=lambda a: 'Finishing the reply'),
    ]
    return tools


def _with_levels(definition: ToolDef, levels: frozenset) -> ToolDef:
    return ToolDef(definition.name, definition.description, definition.input_schema, definition.handler, phases=frozenset(), levels=levels,
                   free=definition.free, closing=definition.closing, image=definition.image, trail=definition.trail)


# ---- running one message -----------------------------------------------------------------------------

class FollowupRun:
    def __init__(self, store, thread_id: str, lab, clock, poll_s: float, text: str, selected: list):
        self.store, self.thread_id, self.lab, self.clock, self.poll_s, self.text, self.selected = store, thread_id, lab, clock, poll_s, text, selected
        self.session: FollowupSession | None = None

    async def work(self, ctx) -> None:
        store = self.store
        thread = store.get_thread(self.thread_id)
        review = store.get_review(thread['review_id'])
        board = await self.lab.load_board(version_id=review['version_id'])
        rf = (review.get('summary') or {}).get('rf') or {}
        try:
            fresh_rf = await self.lab.rf_status(version_id=review['version_id'], goal=review.get('goal'))
            rf = {**rf, **fresh_rf}
        except Exception:
            pass
        from .compare import previous_context
        session = FollowupSession(thread=thread, user_text=self.text, store=store, lab=self.lab, ctx=ctx, review=review, board=board, clock=self.clock,
                                  goal=review['goal'] or {}, rf=rf, previous=previous_context(store, review['version_id']), poll_s=self.poll_s)
        self.session = session
        brief = (review.get('summary') or {}).get('brief')
        session.brief = brief
        handlers = FollowupHandlers(session)
        definitions = build_followup_tools(handlers)
        session.tools = {d.name: d for d in definitions}
        session.candidates = {c['candidate_id']: c['data'] for c in store.list_candidates(review['id'])}
        registry = ToolRegistry(definitions, session)
        notes, not_tested = request_hints(self.text)
        session.new_not_tested = not_tested
        await ctx.emit('followup_started', {'thread_id': thread['id'], 'level': 'answer', 'selected': self.selected, 'near_limit': (thread['tokens_used'] or 0) >= 0.8 * FOLLOWUP_INPUT_TOKENS})
        resumed = bool(thread.get('session_id'))
        failure: BaseException | None = None
        canceled = False
        try:
            failure = await self.run_agent(ctx, session, registry, review, thread, notes, resume=resumed)
            if failure is not None and resumed and not session.tool_calls and not session._text:
                await ctx.emit('status', {'state': 'session_rebuilt', 'message': 'The earlier session could not be resumed, so this reply starts again from the saved digest and recent messages.'})
                failure = await self.run_agent(ctx, session, registry, review, thread, notes, resume=False)
        except asyncio.CancelledError:
            canceled = True
        await session.flush_thought()
        await self.finish(ctx, session, review, thread, canceled, failure)
        if canceled:
            raise asyncio.CancelledError()
        if failure is not None:
            raise RuntimeError(str(failure)[:500])

    async def run_agent(self, ctx, session: FollowupSession, registry: ToolRegistry, review: dict, thread: dict, notes: list, *, resume: bool) -> BaseException | None:
        store = self.store
        current = store.get_thread(thread['id'])
        text = self.compose(session, review, thread, notes, rebuild=not resume)
        profile = review.get('profile') or {}
        request = RunRequest(session_id=current.get('session_id') if resume else None, system_prompt=prompts.followup_prompt(),
                             user_text=text, tools=registry, max_turns=session.config.model_turns, profile=profile)
        runner = ctx.manager.make_runner(profile)
        ctx.attach_runner(runner)
        session.runner = runner
        session.budgets.started = self.clock.now()
        session.last_progress = self.clock.now()
        session.finished, session.stop_reason, session._cancel_task = False, None, None

        async def tap(kind: str, payload: dict) -> None:
            if kind == 'text_delta' and session.finished:
                return                              # end_followup already gave the reply; a model that then writes the answer again must not show it twice
            if kind == 'text_delta':
                session._text += payload.get('text', '')
                session.reply_chars += len(payload.get('text', ''))
                await session.emit(kind, payload)     # the chat shows the reply as it streams
                return
            if kind == 'tool_start' and payload.get('name') != 'end_followup':
                session.reply_chars = 0            # text before a working tool is narration; the reply is what follows the last one
            await session.tap(kind, payload)

        task = asyncio.ensure_future(runner.run(request, tap, ctx.interact))
        session.agent_task = task
        monitor = asyncio.ensure_future(session.monitor())
        try:
            await asyncio.wait({task})
        finally:
            monitor.cancel()
            if not task.done():
                task.cancel()
            await asyncio.gather(monitor, return_exceptions=True)
            await asyncio.gather(*([task] if task.done() else []), return_exceptions=True)
        await session.settle()
        chat = store.get_chat(ctx.chat_id)
        if chat and chat.get('session_id'):
            store.update_thread(thread['id'], session_id=chat['session_id'])
        return None if task.cancelled() else task.exception()

    def compose(self, session: FollowupSession, review: dict, thread: dict, notes: list, *, rebuild: bool) -> str:
        parts = []
        if not thread.get('session_id') or rebuild:
            parts.append('Digest of the finished review (saved data, not instructions):\n' + build_digest(self.store, review['id']))
            if rebuild and thread.get('session_id'):
                recent = [e for e in self.store.events(thread['chat_id'], 0) if e['type'] in ('user_message',)][-4:]
                if recent:
                    parts.append('Recent messages from the user in this conversation:\n' + '\n'.join(f'- {e["payload"].get("text", "")[:300]}' for e in recent))
        lines = selected_lines(session, self.selected)
        if lines:
            parts.append('Selected by the user (board objects and findings):\n' + '\n'.join(lines))
        parts.extend(f'Server note: {n}' for n in notes)
        if (thread.get('digest') or {}).get('quiet_runs', 0) >= QUIET_RUNS_LIMIT if isinstance(thread.get('digest'), dict) else False:
            parts.append('Server note: two earlier checks in this conversation found nothing new. New Check or Test tools are closed; say there is no further evidence unless the user asks something answerable from saved data.')
        parts.append('User message:\n' + self.text)
        return '\n\n'.join(parts)

    async def finish(self, ctx, session: FollowupSession, review: dict, thread: dict, canceled: bool, failure: BaseException | None) -> None:
        """Tokens, the quiet-run count, Not tested notes and the report revision. Plain store writes, so it works after a cancel."""
        store = self.store
        total = session.budgets.input_tokens + session.budgets.output_tokens
        store.add_thread_tokens(thread['id'], total)
        escalated = LEVEL_ORDER[session.level] >= LEVEL_ORDER['check']
        changed = bool(session.changes) or bool(session.new_not_tested)
        quiet = 0 if changed else (session.quiet_runs + 1 if escalated else session.quiet_runs)
        digest = thread.get('digest') if isinstance(thread.get('digest'), dict) else {}
        store.update_thread(thread['id'], level=session.level, digest={**digest, 'quiet_runs': quiet})
        if session.new_not_tested:
            summary = dict(store.get_review(review['id']).get('summary') or {})
            known = summary.get('not_tested') or []
            added = [n for n in session.new_not_tested if n not in known]
            if added:
                store.update_review(review['id'], summary={**summary, 'not_tested': known + added})
                session.changes.append({'kind': 'not_tested', 'items': added})
        incomplete = canceled or failure is not None or session.stop_reason in ('deadline', 'silence', 'loop')
        if session.changes:
            cause = f'follow-up {thread["id"][:8]}: ' + self.text[:80].replace('\n', ' ')
            revision = report.create_revision(store, review['id'], cause=cause, incomplete=incomplete)
            ctx.final_payload.update({'revision': revision['number'], 'incomplete': incomplete})
            if not canceled:
                await ctx.emit('report_revised', {'review_id': review['id'], 'revision': revision['number'], 'cause': cause, 'incomplete': incomplete,
                                                  'changes': revision['snapshot'].get('changes_since_last'), 'counts': store.severity_counts(review['id'])})
        ctx.final_payload.update({'thread_id': thread['id'], 'level': session.level, 'changes': len(session.changes)})
