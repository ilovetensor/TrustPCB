"""The phase state machine (spec section 6). Phases only move forward and the server owns the moves.

0 Intake and 1 Baseline are code. 2 Understand, 3 Review, 4 Investigate and 5 Confirm use the
agent. 6 Report is code, with the agent writing the summary when it is still running.
"""
from __future__ import annotations

from dataclasses import dataclass, field

PHASE_NAMES = {0: 'intake', 1: 'baseline', 2: 'understand', 3: 'review', 4: 'investigate', 5: 'confirm', 6: 'report'}


class PhaseError(ValueError):
    """An illegal phase move (backwards, or out of order)."""


@dataclass
class PhaseChange:
    previous: int
    phase: int
    skipped: list = field(default_factory=list)   # [(phase, reason)]


class PhaseMachine:
    def __init__(self, depth: str, rf_simulatable: bool, rf_reason: str = ''):
        self.depth = depth
        self.rf_simulatable = rf_simulatable
        self.rf_reason = rf_reason
        self.phase = 2
        self.history: list[PhaseChange] = []
        self.improved = False      # set when an investigation closes with an improvement

    @property
    def name(self) -> str:
        return PHASE_NAMES[self.phase]

    def _skip_reason(self, phase: int) -> str | None:
        if phase == 4:
            if self.depth == 'quick':
                return 'Quick reviews stop after phase 3.'
            if not self.rf_simulatable:
                return self.rf_reason or 'Nothing on this board can be simulated.'
        if phase == 5:
            if self.depth == 'quick':
                return 'Quick reviews stop after phase 3.'
            if not self.improved:
                return 'No investigation found an improvement to confirm.'
        return None

    def next_phase(self, frm: int | None = None) -> tuple[int, list]:
        """The phase that follows `frm` and the phases skipped on the way."""
        frm = self.phase if frm is None else frm
        skipped: list = []
        to = frm + 1
        while to < 6:
            reason = self._skip_reason(to)
            if reason is None:
                break
            skipped.append((to, reason))
            to += 1
        return to, skipped

    def advance(self) -> PhaseChange:
        if self.phase >= 6:
            raise PhaseError('The review is already in the report phase.')
        to, skipped = self.next_phase()
        change = PhaseChange(self.phase, to, skipped)
        self.phase = to
        self.history.append(change)
        return change

    def force_report(self) -> PhaseChange | None:
        """Move straight to phase 6 (agent ended, budget ended, deadline). None when already there."""
        if self.phase >= 6:
            return None
        change = PhaseChange(self.phase, 6, [(p, 'The review ended before this phase.') for p in range(self.phase + 1, 6)])
        self.phase = 6
        self.history.append(change)
        return change
