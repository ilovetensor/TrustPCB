"""The running state of one agent run: gates, counters, the event tap and the watchdog.

`ReviewSession` is the gate the tool registry asks before and after every call. It owns the phase
machine, budgets, the loop-prevention ledger, the finding engine and everything the tool handlers
share. The follow-up session (followup.py) reuses it with levels instead of phases.
"""
from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from . import evidence as ev
from .budgets import REFUSAL_STREAK_LIMIT, BudgetConfig, BudgetTracker, Clock, Ledger
from .findings import FindingEngine
from .lab import Lab, scenario_hash
from .machine import PHASE_NAMES, PhaseMachine
from .refine import REFINE_ONLY_TOOLS, RefineState
from .tools import ToolDef, ToolResult, fail

GATE_REFUSALS = ('agent_stopped', 'not_allowed_in_phase', 'invalid_arguments', 'budget_exhausted', 'review_complete', 'unknown_tool', 'level_not_allowed', 'no_further_evidence')
GRACE_AFTER_SUMMARY_S = 3.0


class ReviewSession:
    mode = 'review'

    def __init__(self, *, store, lab: Lab, ctx, review: dict, board: Any, clock: Clock, config: BudgetConfig, goal: dict, rf: dict,
                 previous: dict | None = None, machine: PhaseMachine | None = None, poll_s: float = 2.0):
        self.store, self.lab, self.ctx, self.board, self.clock = store, lab, ctx, board, clock
        self.review = review
        self.review_id = review['id']
        self.version_id = review['version_id']
        self.goal = goal or {}
        self.rf = rf or {}
        self.previous = previous
        self.config = config
        self.budgets = BudgetTracker(config, clock)
        self.machine = machine or PhaseMachine(review.get('depth') or config.name, bool(self.rf.get('simulatable')), self.rf.get('reason', ''))
        self.ledger = Ledger(config)
        self.engine = FindingEngine(store, self.review_id, board, self.context, previous)
        self.poll_s = poll_s
        self.tools: dict[str, ToolDef] = {}
        # shared state of the run
        self.brief: dict | None = None
        self.summary: dict | None = None
        self.summary_failures = 0
        self.candidates: dict[str, dict] = {}
        self.refs: dict[str, dict] = {}              # R1.. -> {evidence_id, hash, run, metrics, quality, label}
        self.reference_quantities: list = []
        self.investigation_codes: dict[str, str] = {}
        self.suspect_codes: dict[str, str] = {}
        self.limits: list[dict] = []                 # budget walls and stop rules that fired
        self.region_views: dict[int, dict] = {}        # phase 3 open-review gate: counted region views
        self.view_unavailable = False                   # the board cannot be drawn: the region view gate gives way
        self.dismissals: list[dict] = []                # {rules, count, reason}: shown in the report
        self.auto_grouped: list[dict] = []              # info-only rule groups grouped by code at the phase 3 gate
        self.layout_reasons: dict[str, str] = {}        # phase 3 layout gate: layout rule group -> the specific reason (60+ characters) no finding is needed
        self.layout_waived: dict | None = None
        self.refine = RefineState()                     # the bounded refine round after a confirm that missed the target
        self.coverage_refusals = 0
        self.partial_refusals = 0                       # refusals of advance_phase for a partial win the finding does not report yet
        self.coverage_waived: dict | None = None
        self.stop_reason: str | None = None
        self.stop_detail: str = ''
        self.finished = False
        self.finished_at: float | None = None
        self.refusal_streak = 0
        self.last_progress = clock.now()
        self.solver_busy = 0
        self.runner: Any = None
        self.agent_task: asyncio.Task | None = None
        self._cancel_task: asyncio.Future | None = None
        self.tool_calls = 0
        self._text = ''
        self._ref_counter = 0
        self._counters = {'I': 0, 'S': 0}

    # ---- context for the checks -----------------------------------------------------------
    @property
    def phase(self) -> int:
        return self.machine.phase

    def context(self) -> list:
        return ev.context_quantities(self.goal, self.store.list_assumptions(self.review_id), self.board, self.brief, self.rf) + list(self.reference_quantities)

    @property
    def budget_key(self):
        return self.phase

    def make_scenario(self, ops: list, material: dict | None, quality: str, setup: dict | None = None) -> dict:
        """The scenario object whose hash is the cache key (spec section 10)."""
        scenario = {'version_id': self.version_id, 'antenna_id': self.rf.get('antenna_id'), 'band': self.rf.get('band_hz') or self.goal.get('band_hz'),
                    'quality': quality, 'whatif_ops': ops, 'material': material, 'region': self.rf.get('region_bbox')}
        if setup:
            scenario['setup'] = setup
        return scenario

    def base_hash(self) -> str:
        return scenario_hash(self.make_scenario([], None, 'screen'))

    def in_band_view(self, m: dict) -> dict:
        """The in-band numbers stated plainly: S11 across the band, then the impedance at the band centre and what it means."""
        band = self.rf.get('band_hz') or self.goal.get('band_hz')
        basis = {'as_built': 'as built (with the board\'s own matching parts)', 'antenna_only': 'antenna alone (the board\'s matching parts are not known)'}.get(m.get('basis'))
        view: dict[str, Any] = {}
        if band:
            view['band_mhz'] = [round(band[0] / 1e6, 1), round(band[1] / 1e6, 1)]
        if m.get('band_worst_s11_db') is not None:
            view['worst_s11_db'] = round(m['band_worst_s11_db'], 2)
        if m.get('band_mean_s11_db') is not None:
            view['mean_s11_db'] = round(m['band_mean_s11_db'], 2)
        if basis:
            view['basis'] = basis
        if m.get('band_worst_accepted_power_pct') is not None:
            view['worst_accepted_power_pct'] = m['band_worst_accepted_power_pct']
            if m.get('band_mean_accepted_power_pct') is not None:
                view['mean_accepted_power_pct'] = m['band_mean_accepted_power_pct']
            view['accepted_power_note'] = ev.ACCEPTED_POWER_NOTE
        if m.get('antenna_only_band_worst_db') is not None and m.get('basis') == 'as_built':
            view['antenna_alone_worst_s11_db'] = m['antenna_only_band_worst_db']
            if m.get('antenna_only_band_worst_accepted_power_pct') is not None:
                view['antenna_alone_worst_accepted_power_pct'] = m['antenna_only_band_worst_accepted_power_pct']
        view['target_s11_db'] = ev.target_db(self.goal, self.rf)
        view['meets_target'] = ev.meets_target(m, self.goal, self.rf)
        view['how_to_read'] = 'Success is the worst S11 across the band, as built. The deepest dip is only a description.'
        return view

    def impedance_view(self, m: dict) -> dict | None:
        if m.get('centre_re') is None or m.get('centre_im') is None:
            return None
        character = ev.reactance_character(m['centre_re'], m['centre_im'])
        seen = ('as seen by the radio, through the matching parts' if m.get('centre_basis') == 'radio_side'
                else 'at the antenna feed, before the matching parts')
        out = {'resistance_ohm': round(m['centre_re'], 1), 'reactance_ohm': round(m['centre_im'], 1), 'character': character, 'seen': seen, 'meaning': ev.CHARACTER_MEANING.get(character)}
        if m.get('centre_f_hz') is not None:
            out['frequency_mhz'] = round(m['centre_f_hz'] / 1e6, 2)
        return out

    def result_view(self, exp) -> dict:
        """What the agent is told about a stored experiment: in-band first, then the impedance, every dip, the first series resonance."""
        item = self.refs.get(exp.ref)
        values = (item['evidence']['values'] if item else {}) or {}
        out = {'ref': exp.ref, 'label': exp.label, 'kind': exp.kind, 'quality': exp.quality, 'trust': values.get('trust'), 'measurements': values.get('measurements'),
               'meets_target': ev.meets_target(exp.metrics, self.goal, self.rf)}
        m = exp.metrics or {}
        out['in_band'] = self.in_band_view(m)
        impedance = self.impedance_view(m)
        if impedance:
            out['band_centre_impedance'] = impedance
        detail = {}
        if m.get('dips'):
            detail['s11_dips'] = [{'frequency_mhz': round(d['f_hz'] / 1e6, 1), 's11_db': d['s11_db']} for d in m['dips']]
            detail['dips_note'] = ('Dips are descriptive. Compare them between runs: a dip that stays put when the antenna geometry changes is not the antenna\'s own resonance; '
                                   'call it a dip of unknown origin. The first series resonance is the length-related indicator.')
        for key, name in (('first_series_resonance_hz', 'first_series_resonance_mhz'),):
            if m.get(key) is not None:
                detail[name] = round(m[key] / 1e6, 1)
        for key in ('antenna_only_band_worst_db', 'antenna_only_band_best_db', 'antenna_only_band_mean_db', 'as_built_band_worst_db', 'as_built_band_best_db', 'as_built_band_mean_db', 'basis',
                    'antenna_only_band_worst_accepted_power_pct', 'antenna_only_band_mean_accepted_power_pct', 'as_built_band_worst_accepted_power_pct', 'as_built_band_mean_accepted_power_pct'):
            if m.get(key) is not None:
                detail[key] = m[key]
        if m.get('plausibility'):
            detail['plausibility_flags'] = m['plausibility']
        if detail:
            out['rf_detail'] = detail
        if m.get('summary_text'):
            out['rf_summary'] = m['summary_text']
        if exp.kind == 'matching':
            out['applied_to'] = exp.source_ref
            out['parts_changed'] = [{'part': p['ref'], 'position': p['position'], 'was': p['was'], 'now': p['now']} for p in exp.parts or []]
            out['solver_s'] = 0
        if exp.quality == 'screen':
            out['compare_only'] = 'Screen runs are for comparing with the base run. Do not quote their absolute values as results.'
        if exp.delta:
            out['vs_base'] = ev.delta_measurements(exp.delta)
        return out

    async def announce_phase(self, change) -> None:
        self.store.update_review(self.review_id, phase=str(change.phase))
        await self.emit('phase_changed', {'phase': change.phase, 'name': PHASE_NAMES[change.phase], 'previous': change.previous,
                                          'skipped': [{'phase': p, 'name': PHASE_NAMES[p], 'reason': r} for p, r in change.skipped]})

    def next_code(self, prefix: str) -> str:
        self._counters[prefix] += 1
        return f'{prefix}{self._counters[prefix]}'

    def next_ref(self) -> str:
        self._ref_counter += 1
        return f'R{self._ref_counter}'

    def add_limit(self, rule: str, detail: str, **extra) -> None:
        self.limits.append({'rule': rule, 'detail': detail, 'at_s': round(self.budgets.elapsed(), 1), 'phase': self.phase, **extra})

    # ---- events ---------------------------------------------------------------------------
    async def emit(self, kind: str, payload: dict) -> None:
        await self.ctx.emit(kind, payload)

    async def emit_budget(self) -> None:
        snapshot = self.budgets.snapshot(self.phase)
        self.store.update_review(self.review_id, budget=snapshot)
        await self.emit('budget', snapshot)

    # ---- gate (called by the registry) ------------------------------------------------------
    def allowed_names(self) -> list[str]:
        if self.finished:
            return [n for n, d in self.tools.items() if d.free]
        names = [n for n, d in self.tools.items() if self.phase in d.phases]
        if self.phase == 5 and not self.refine.active:
            names = [n for n in names if n not in REFINE_ONLY_TOOLS]      # simulations come back only while a refine round is open
        return names

    def phase_label(self) -> str:
        return f'phase {self.phase}, {PHASE_NAMES[self.phase]}'

    def closing_names(self) -> list[str]:
        return [n for n, d in self.tools.items() if self.phase in d.phases and (d.closing or d.free)]

    async def before(self, tool: ToolDef, args: dict) -> ToolResult | None:
        if self.stop_reason and self.stop_reason != 'finished' and not self.finished:
            return fail('agent_stopped', f'The server is ending this run ({self.stop_reason.replace("_", " ")}). Stop now.')
        if self.finished and not tool.free:
            return fail('review_complete', 'The summary is accepted and the review is complete. Stop now.', allowed=self.allowed_names())
        wall = self.budgets.global_exhausted()
        if wall and not tool.closing and not tool.free:
            self.note_wall(wall)
            return fail('budget_exhausted', f'The {wall.replace("_", " ")} budget has ended. Only closing tools still work: {", ".join(self.closing_names())}.',
                        budget=wall, allowed=self.closing_names())
        if not tool.free and not tool.closing and self.budgets.calls_left(self.phase) <= 0:
            self.note_wall(f'phase_{self.phase}_tool_calls')
            return fail('budget_exhausted', f'Phase {self.phase} used all {self.config.phase_calls.get(self.phase)} of its tool calls. '
                        f'Only closing tools still work: {", ".join(self.closing_names())}.', budget='phase_tool_calls', allowed=self.closing_names())
        if tool.image and self.budgets.images_left(self.phase) <= 0:
            self.note_wall('images')
            return fail('budget_exhausted', 'The image budget has ended. Work from the numbers you already have.', budget='images')
        return None

    def note_wall(self, budget: str) -> None:
        if not any(l['rule'] == 'budget_wall' and l.get('budget') == budget for l in self.limits):
            self.add_limit('budget_wall', f'{budget.replace("_", " ")} budget ended', budget=budget)

    async def after(self, tool: ToolDef, args: dict, result: ToolResult) -> None:
        self.last_progress = self.clock.now()
        code = (result.structured or {}).get('error') if result.is_error else None
        if code in GATE_REFUSALS:
            self.budgets.refused += 1
        elif not tool.free:
            self.budgets.add_call(self.phase)
        self.tool_calls += 1
        self.refusal_streak = self.refusal_streak + 1 if result.is_error else 0
        if self.refusal_streak >= REFUSAL_STREAK_LIMIT:
            self.add_limit('loop', f'{self.refusal_streak} refused or failed tool calls in a row')
            self.request_stop('loop', f'{self.refusal_streak} refused or failed tool calls in a row')
        wall = self.budgets.global_exhausted()
        if wall and not self.finished:
            self.note_wall(wall)            # a budget has ended, even if no tool call was refused because of it
        if self.budgets.deadline_reached() and not self.finished and self.stop_reason is None:
            self.add_limit('hard_deadline', 'The wall-clock budget ended')
            self.request_stop('deadline', 'wall-clock budget ended')
        await self.emit_budget()

    @contextlib.asynccontextmanager
    async def solving(self):
        """While a simulation runs the silence watchdog stays quiet."""
        self.solver_busy += 1
        try:
            yield
        finally:
            self.solver_busy -= 1
            self.last_progress = self.clock.now()

    # ---- the event tap between the runner and the app ---------------------------------------
    async def tap(self, kind: str, payload: dict) -> None:
        """Wrap the runner's emit: count usage, turn assistant text into `thought`, add trail lines."""
        if kind == 'text_delta':
            self._text += payload.get('text', '')
            return
        if kind == 'usage':
            self.budgets.add_usage(payload)
        elif kind == 'tool_start':
            await self.flush_thought()
            self.budgets.add_turn()
            definition = self.tools.get(payload.get('name', ''))
            if definition and definition.trail:
                try:
                    payload = {**payload, 'trail': definition.trail(payload.get('input') or {})[:200]}
                except Exception:
                    pass
            self.last_progress = self.clock.now()
        elif kind == 'status' and payload.get('state') == 'max_turns':
            self.add_limit('model_turns', 'The agent reached its turn limit', turns=payload.get('max_turns'))
        elif kind == 'thought':
            self.last_progress = self.clock.now()
        await self.emit(kind, payload)

    async def flush_thought(self) -> None:
        text = self._text.strip()
        self._text = ''
        if text:
            await self.emit('thought', {'text': text[:1500]})

    # ---- stopping the agent -----------------------------------------------------------------
    def request_stop(self, reason: str, detail: str = '') -> None:
        """Ask the agent run to end (rules 8, 9 and the loop guard). The first reason wins; the review then moves to the report."""
        if self.stop_reason is None:
            self.stop_reason, self.stop_detail = reason, detail
        if self._cancel_task is None:
            self._cancel_task = asyncio.ensure_future(self._cancel_agent())

    async def _cancel_agent(self) -> None:
        runner, task = self.runner, self.agent_task
        if runner is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(runner.cancel(), timeout=3)
        if task is not None and not task.done():
            await asyncio.wait({task}, timeout=3)
            if not task.done():
                task.cancel()

    async def settle(self) -> None:
        if self._cancel_task is not None:
            with contextlib.suppress(Exception):
                await self._cancel_task

    async def monitor(self) -> None:
        """Rules 8 and 9 (and the end of a finished review) with an injected clock."""
        while True:
            await self.clock.sleep(self.poll_s)
            now = self.clock.now()
            if self.budgets.deadline_reached():
                self.add_limit('hard_deadline', 'The wall-clock budget ended')
                self.request_stop('deadline', 'wall-clock budget ended')
                return
            if self.finished and self.finished_at is not None and now - self.finished_at >= GRACE_AFTER_SUMMARY_S:
                self.request_stop('finished', '')
                return
            if not self.finished and not self.solver_busy and now - self.last_progress >= self.config.silence_s:
                self.add_limit('silence_watchdog', f'No tool call finished for {int(self.config.silence_s)} seconds')
                self.request_stop('silence', 'no completed tool call for 3 minutes')
                return

    def mark_finished(self) -> None:
        self.finished, self.finished_at = True, self.clock.now()
