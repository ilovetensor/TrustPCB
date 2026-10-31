"""The refine round (spec sections 6 to 8): one bounded second attempt after a confirm that missed the target.

When Confirm ends with the target not met (`held_smaller`, `not_held`, or `held_after_retune` still short of the target) and the
budget allows, `advance_phase` in phase 5 does not go to the report. It opens a refine round on the same investigation:

  * the phase stays 5 (events stay forward-only: `phase_changed` carries the substep `refine`);
  * up to `REFINE_SCREENS` screen what-ifs compared with the base, each with a reason drawn from confirm evidence;
  * up to `REFINE_MATCHING` matching experiments on them (zero solver time);
  * one confirm of the new best candidate, geometry and matching together.

The server enforces the limits: at most one round per Standard review, two per Deep, none on Quick. Every stop rule and budget
wall still applies. After the round, phase 6 always runs. On Standard the agent cannot decline while solver budget allows.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import evidence as ev
from .budgets import (DEFAULT_CONFIRM_S, DEFAULT_SCREEN_S, REFINE_EXTRA_CALLS, REFINE_EXPERIMENT_ALLOWANCE, REFINE_MATCHING, REFINE_SCREENS)
from .findings import FindingRejected, check_numbers, check_wording
from .tools import ToolResult, fail, ok

REFINE_ONLY_TOOLS = ('run_simulation',)       # phase 5 tools that exist only while a refine round is open
MIN_REASON_CHARS = 30
MIN_DECLINE_CHARS = 40
RESERVE_S = 60.0                              # wall-clock margin kept for the report when judging whether a round fits
CODE = re.compile(r'\bE-\d{3}\b')


@dataclass
class RefineState:
    active: bool = False
    round: int = 0                             # rounds started so far (the number of the open round while active)
    inv: str | None = None                     # investigation id of the open round
    suspect: str | None = None                 # suspect id created for the open round
    key: str | None = None                     # 'refine1': the confirm slot of the open round
    screens: int = 0                           # real (non-cached) screen runs in the open round
    matching: int = 0                          # real matching experiments in the open round
    shots: list = field(default_factory=list)  # screens of the open round: {ref, label, reason, ops}
    declined: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    history: list = field(default_factory=list)


class RefineMixin:
    """Mixed into Handlers; `self.s` is the session and `self.store` its store."""

    # ---- estimates and eligibility ------------------------------------------------------------
    def solver_estimates(self) -> tuple[float, float]:
        """(screen_s, confirm_s): the longest timed run of each quality so far, else the spec figures."""
        screen = confirm = 0.0
        for item in self.s.refs.values():
            exp = item.get('exp')
            if exp is None or exp.kind == 'matching' or not exp.metrics:
                continue
            seconds = float(exp.metrics.get('solve_s') or 0)
            if exp.quality == 'confirm':
                confirm = max(confirm, seconds)
            else:
                screen = max(screen, seconds)
        return screen or DEFAULT_SCREEN_S, confirm or DEFAULT_CONFIRM_S

    def refine_budget_allows(self) -> tuple[bool, str]:
        """Is there room for one combined candidate: a screen run and a confirm run, with time left for the report?"""
        s = self.s
        wall = s.budgets.global_exhausted()
        if wall:
            return False, f'the {wall.replace("_", " ")} budget has ended'
        screen_s, confirm_s = self.solver_estimates()
        need = screen_s + confirm_s
        if s.config.solver_s and s.budgets.solver_left() < need:
            return False, f'{round(s.budgets.solver_left() / 60)} minutes of solver time are left and a screen run plus a confirm run needs about {round(need / 60)}'
        if s.config.soft_wall_s - s.budgets.elapsed() < need + RESERVE_S:
            return False, 'not enough wall-clock time is left for a screen run and a confirm run'
        return True, ''

    @staticmethod
    def latest_confirmation(inv) -> dict | None:
        return list(inv.refine_confirmations.values())[-1] if inv.refine_confirmations else inv.confirmation

    @staticmethod
    def confirmation_meets_target(confirmation: dict | None) -> bool:
        if not confirmation:
            return False
        free = (confirmation.get('matching_at_confirm') or {}).get('free') or {}
        return bool(confirmation.get('meets_target') or free.get('meets_target'))

    def refine_pick(self):
        """The investigation a refine round would work on: improved, confirmed, target still not met, and not refined to the limit."""
        s = self.s
        if s.config.refine_rounds <= 0 or s.refine.round >= s.config.refine_rounds:
            return None, 'no refine round is left' if s.config.refine_rounds else 'this review depth has no refine round'
        for inv in s.ledger.investigations.values():
            conf = self.latest_confirmation(inv)
            if inv.outcome != 'improved' or conf is None or conf.get('outcome') == 'unconfirmed':
                continue
            if self.confirmation_meets_target(conf) or len(inv.refine_suspects) >= s.config.refine_rounds:
                continue
            return inv, ''
        return None, 'confirm did not end short of the target'

    # ---- advance_phase in phase 5 --------------------------------------------------------------
    async def refine_gate(self, args: dict) -> ToolResult | None:
        """Called by advance_phase in phase 5. None lets the phase advance; a result keeps the review in phase 5."""
        s = self.s
        ended = None
        if s.refine.active:
            inv = s.ledger.investigations[s.refine.inv]
            gaps = self.refine_gaps(inv)
            if gaps:
                s.budgets.add_call(5)            # a refusal counts against the phase budget, so it cannot loop
                return fail('refine_incomplete', 'The refine round is not finished: ' + '; '.join(gaps) + '. Leaving now would skip the second attempt the budget still allows.',
                            missing=gaps, phase_5_calls_left=s.budgets.calls_left(5))
            ended = await self.end_refine(inv, 'The agent closed the round.')
        inv, why = self.refine_pick()
        if inv is None:
            return None
        allows, budget_why = self.refine_budget_allows()
        decline = (args.get('decline_refine') or '').strip()
        if decline:
            if s.config.name == 'standard' and allows:
                s.budgets.add_call(5)
                return fail('refine_required', 'The target was not met at confirm quality and the solver budget allows another attempt. On Standard you must try at least one combined '
                            'candidate (a what-if screen with matching on it, then a confirm). Call advance_phase again without decline_refine to open the refine round.')
            problem = self.check_confirm_reason(decline, MIN_DECLINE_CHARS, 'decline_refine')
            if problem is not None:
                return problem
            s.refine.declined.append({'investigation': inv.code, 'reason': decline})
            s.add_limit('refine_declined', decline, investigation=inv.code)
            self.store_round(inv, {'round': None, 'declined': decline, 'result': 'declined'})
            return None
        if not allows:
            s.refine.skipped.append({'investigation': inv.code, 'reason': budget_why})
            s.add_limit('refine_skipped', f'No refine round was possible: {budget_why}.', investigation=inv.code)
            self.store_round(inv, {'round': None, 'skipped': budget_why, 'result': 'skipped'})
            return None
        started = await self.start_refine(inv)
        if ended is not None:
            started.structured['previous_round'] = {'round': ended['round'], 'result': ended['result']}
            started = ok(started.structured)
        return started

    def refine_gaps(self, inv) -> list[str]:
        """What is missing before the open round may end, unless a budget wall has ended it anyway."""
        s = self.s
        if s.budgets.global_exhausted() or s.budgets.calls_left(5) <= 0:
            return []
        suspect = s.ledger.suspects[s.refine.suspect]
        screen_s, confirm_s = self.solver_estimates()
        tried = [e for e in suspect.experiments if e.kind == 'whatif']
        trusted = [e for e in tried if e.ok]
        gaps = []
        if not trusted and (not s.config.solver_s or s.budgets.solver_left() >= screen_s) and s.refine.screens < REFINE_SCREENS:
            gaps.append('run at least one screen what-if with run_simulation (a geometry change, with a reason that cites confirm evidence)')
        elif trusted and not any(e.kind == 'matching' and e.ops for e in suspect.experiments) and not inv.matching_unavailable and s.refine.matching < REFINE_MATCHING:
            gaps.append('run tune_matching on your best refine what-if, so geometry and matching are tried together')
        candidate = s.ledger.refine_candidate(inv, suspect.id)
        if candidate is not None and s.refine.key not in inv.confirms:
            geometry_run = bool(candidate.ops)
            if not geometry_run or not s.config.solver_s or s.budgets.solver_left() >= confirm_s:
                gaps.append('confirm the new best candidate with confirm_result (which: refine)')
        return gaps

    async def start_refine(self, inv) -> ToolResult:
        s = self.s
        s.refine.round += 1
        key = f'refine{s.refine.round}'
        confirmation = self.latest_confirmation(inv)
        title = f'Refine round {s.refine.round}: combine a geometry change with matching'
        row = self.store.add_suspect(inv.id, title, rank=90 + s.refine.round, data={'why': 'The confirm run missed the target.', 'test': 'Screen what-ifs and matching on them, then one confirm.'})
        suspect = s.ledger.add_suspect(inv, row['id'], s.next_code('S'), title)
        inv.refine_suspects.append(suspect.id)
        inv.refining, inv.good_enough = True, False
        inv.extra_experiments += REFINE_EXPERIMENT_ALLOWANCE
        s.budgets.bonus_calls[5] = s.budgets.bonus_calls.get(5, 0) + REFINE_EXTRA_CALLS
        s.budgets.bonus_confirm_runs += 1
        s.refine.active, s.refine.inv, s.refine.suspect, s.refine.key = True, inv.id, suspect.id, key
        s.refine.screens = s.refine.matching = 0
        s.refine.shots = []
        self.persist_suspect(suspect)
        await self.publish_suspect(suspect)
        await s.emit('phase_changed', {'phase': 5, 'name': 'confirm', 'previous': 5, 'substep': 'refine', 'round': s.refine.round, 'rounds': s.config.refine_rounds,
                                       'investigation': inv.code, 'skipped': []})
        await self.publish_investigation(inv, note=f'Refine round {s.refine.round} opened: the confirm run missed the target.')
        from .. import prompts
        data = {'refine_round_started': True, 'round': s.refine.round, 'of': s.config.refine_rounds, 'phase': 5, 'name': 'confirm', 'substep': 'refine',
                'investigation': inv.code, 'suspect': suspect.code,
                'why': confirmation.get('statement'), 'confirm_outcome': confirmation.get('outcome'),
                'confirm_evidence': {k: confirmation.get(k) for k in ('rows', 'matching_at_confirm', 'confirm_gain_db', 'free_gain_db', 'meets_target')},
                'allowed': {'screen_what_ifs': REFINE_SCREENS, 'matching_experiments': REFINE_MATCHING, 'confirm_runs': 1},
                'brief': prompts.phase_brief('5_refine', **self.phase_vars())}
        return ok(data)

    async def end_refine(self, inv, ended_by: str) -> dict | None:
        """Close the open round: the refine suspect, the stored record and the event note. Returns the round's record."""
        s = self.s
        if not s.refine.active:
            return None
        suspect = s.ledger.suspects[s.refine.suspect]
        key = s.refine.key
        candidate = s.ledger.refine_candidate(inv, suspect.id)
        confirm = inv.refine_confirmations.get(key)
        if confirm is not None:
            result = 'target_met' if self.confirmation_meets_target(confirm) else 'target_not_met'
        elif candidate is not None:
            result = 'candidate_not_confirmed'
        elif s.refine.screens or s.refine.matching:
            result = 'no_candidate'
        else:
            result = 'not_tried'
        if not suspect.closed:
            status = {'target_met': 'confirmed', 'target_not_met': 'inconclusive', 'candidate_not_confirmed': 'inconclusive', 'no_candidate': 'inconclusive', 'not_tried': 'not_tested'}[result]
            reason = {'target_met': 'The combined candidate met the target at confirm quality.',
                      'target_not_met': 'The combined candidate was confirmed and still missed the target.',
                      'candidate_not_confirmed': 'The best candidate was not confirmed before the round ended.',
                      'no_candidate': 'No refine candidate improved the worst in-band S11 by 1 dB against the base.',
                      'not_tried': 'The round ended before any candidate was tried.'}[result]
            s.ledger.close_suspect(suspect, status, reason, s.base_hash())
            suspect.reason = reason
        await self.publish_suspect(suspect)
        record = {'round': s.refine.round, 'screens': list(s.refine.shots), 'matching_experiments': s.refine.matching, 'candidate': candidate and candidate.ref,
                  'candidate_change': candidate and {'ops': candidate.ops, 'parts': [{k: p.get(k) for k in ('ref', 'position', 'was', 'now')} for p in candidate.parts or []], 'experiment': candidate.kind},
                  'confirmation': confirm, 'result': result, 'ended_by': ended_by}
        s.refine.history.append(record)
        inv.refining = False
        s.refine.active = False
        self.store_round(inv, record)
        await self.restate_after_confirm(inv)
        await self.publish_investigation(inv, note=f'Refine round {record["round"]} ended: {result.replace("_", " ")}.')
        return record

    def store_round(self, inv, record: dict) -> None:
        existing = (self.store.get_investigation(inv.id) or {}).get('outcome') or {}
        rounds = list(existing.get('refine_rounds') or []) + [record]
        self.store.update_investigation(inv.id, outcome={**existing, 'refine_rounds': rounds})

    # ---- checks used by the tools while a round is open ------------------------------------------
    def confirm_codes(self) -> list[str]:
        return [r['code'] for r in self.s.engine.evidence_rows() if (r['values'] or {}).get('quality') == 'confirm']

    def check_confirm_reason(self, reason: str, minimum: int, name: str) -> ToolResult | None:
        """A reason must be specific, cite confirm evidence by code, and quote only numbers the evidence has."""
        s = self.s
        if len(reason.strip()) < minimum:
            return fail('reason_too_short', f'{name} needs a specific reason of at least {minimum} characters, drawn from the confirm evidence.')
        codes = self.confirm_codes()
        if not any(c in codes for c in CODE.findall(reason)):
            return fail('reason_needs_confirm_evidence', f'{name} must cite confirm evidence by its code (for example {codes[-1] if codes else "E-004"}). Confirm evidence codes: {", ".join(codes[-8:]) or "none"}.',
                        confirm_evidence=codes[-8:])
        allowed = [q for row in s.engine.evidence_rows() if (row['values'] or {}).get('quality') == 'confirm' for q in ev.quantities_of(row)] + s.context()
        try:
            check_wording({name: reason})
            check_numbers({name: reason}, allowed, compare_only=True)
        except FindingRejected as exc:
            return fail(exc.code, exc.message, **exc.data)
        return None

    def refine_suspect(self):
        return self.s.ledger.suspects.get(self.s.refine.suspect) if self.s.refine.active else None

    def refine_run_checks(self, args: dict) -> ToolResult | None:
        """run_simulation in phase 5: only inside an open round, with a change, a reason, a free screen slot and solver time for it."""
        s = self.s
        if not s.refine.active:
            return fail('not_allowed_in_phase', 'Simulations in phase 5 are only possible inside a refine round.')
        if not (args.get('whatif_ops') or []):
            return fail('refine_needs_change', 'A refine screen must change the geometry (whatif_ops). The base is already known; tune_matching tries matching.')
        problem = self.check_confirm_reason(args.get('reason') or '', MIN_REASON_CHARS, 'reason')
        if problem is not None:
            return problem
        if s.refine.screens >= REFINE_SCREENS:
            return fail('budget_exhausted', f'The refine round allows {REFINE_SCREENS} screen what-ifs and they are used. Run tune_matching on the best one, then confirm it.', budget='refine_screens')
        screen_s, _ = self.estimate_pair()
        if s.config.solver_s and s.budgets.solver_left() < screen_s:
            s.note_wall('solver_time')
            return fail('budget_exhausted', f'Only {round(s.budgets.solver_left() / 60)} minutes of solver time are left and a screen run needs about {round(screen_s / 60)}. '
                        'Use tune_matching on a run you have, or advance_phase.', budget='solver_time')
        return None

    def estimate_pair(self) -> tuple[float, float]:
        return self.solver_estimates()
