"""Budgets and loop prevention (spec section 8), enforced by the server.

`BudgetTracker` counts wall clock, solver time, turns, tool calls per phase, images and tokens.
`Ledger` keeps the per-investigation state behind the loop rules: cached duplicate scenarios,
closed suspects that stay closed, the no-progress rule, the good-enough rule and the repair limit.
Everything reads time from an injected clock so tests never wait.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field, replace
from typing import Any

DEPTHS = ('quick', 'standard', 'deep')

# Rule 5: two experiments in a row that move the in-band result (worst and mean) less than this close the suspect.
NO_PROGRESS_DB = 0.5
NO_PROGRESS_HZ = 5e6             # and the first series resonance (else the deepest dip) less than this
MIN_SUSPECTS_TESTED = 2          # Standard: an investigation may not close without a result until this many suspects were tested

DUPLICATE_CLOSE_AT = 3           # rule 3: the third repeat of an identical request closes the suspect
REPAIRS_PER_SCENARIO = 1         # rule 7
REPAIRS_PER_INVESTIGATION = 2
SILENCE_SECONDS = 180.0          # rule 8
MIN_REGION_VIEWS = {'quick': 1, 'standard': 2, 'deep': 2}   # phase 3 open-review gate: region views before leaving the phase
MIN_LAYOUT_FINDINGS = {'quick': 1, 'standard': 2, 'deep': 2}   # phase 3 layout judgement gate: findings from layout groups or region-view opinions
LAYOUT_RULES = ('zones', 'noise_proximity', 'track_jitter', 'track_detour', 'track_angle', 'edge_track', 'via_count')
MIN_LAYOUT_REASON = 60           # a layout group with no finding needs a specific reason of at least this many characters
REFINE_SCREENS = 3               # screen what-ifs in one refine round
REFINE_MATCHING = 3              # matching experiments (zero solver time) in one refine round
REFINE_EXTRA_CALLS = 14          # phase 5 tool calls a refine round adds
REFINE_EXPERIMENT_ALLOWANCE = REFINE_SCREENS + REFINE_MATCHING
DEFAULT_SCREEN_S = 240.0         # solver estimates before any run was timed (spec section 8)
DEFAULT_CONFIRM_S = 420.0
REFUSAL_STREAK_LIMIT = 6         # extra safety: this many refused calls in a row end the agent
IMPROVEMENT_DB = 1.0             # a what-if or matching change counts as an improvement when the worst in-band S11 gains at least this much
SCREEN_CONFIRM_DB = 3.0          # screen and confirm must agree within these limits (in-band worst S11, then the first series resonance)
SCREEN_CONFIRM_RES_REL = 0.06


# ---- judging a change by the in-band result (a delta is evidence.delta: s11_db is the change in worst in-band S11) ----

def gain_db(d: dict | None) -> float:
    """In-band improvement in dB (positive is better) from a delta."""
    return -((d or {}).get('s11_db') or 0.0)


def is_improvement(d: dict | None) -> bool:
    return gain_db(d) >= IMPROVEMENT_DB


def rank_key(d: dict | None) -> tuple[float, float]:
    """Order candidates by in-band worst gain, then by mean gain."""
    return (round(gain_db(d), 2), -((d or {}).get('band_mean_db') or 0.0))


def moved_little(d: dict | None, db: float, hz: float, *, frequency: bool = True) -> bool:
    """No-progress test: the in-band worst and mean moved under `db` and, for a change to the antenna, the first series resonance
    (else the deepest dip) under `hz`. A matching change moves the resonance of the whole network by design, so only the band counts for it."""
    d = d or {}
    freq = d.get('series_res_hz', d.get('f_hz', 0.0)) if frequency else 0.0
    return abs(d.get('s11_db', 0.0)) < db and abs(d.get('band_mean_db', 0.0)) < db and abs(freq) < hz


class Clock:
    """Real time. Tests pass a fake with the same two members."""

    def now(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


@dataclass(frozen=True)
class BudgetConfig:
    name: str
    wall_s: float
    solver_s: float
    model_turns: int
    investigations: int
    experiments_per_investigation: int
    confirm_runs: int
    images: int
    input_tokens: int
    output_tokens: int
    phase_calls: dict = field(default_factory=dict)       # phase number (or level name) -> tool calls
    phase_images: dict = field(default_factory=dict)      # phase number -> images
    suspects_per_investigation: int = 3
    confirm_runs_per_investigation: int = 2
    silence_s: float = SILENCE_SECONDS
    reserve_s: float | None = None                        # time before the hard deadline when only closing tools work
    full_investigation: bool = False                      # Standard: no closing without a result while budget allows and the method is incomplete
    refine_rounds: int = 0                                # bounded refine rounds after a confirm that missed the target (Standard 1, Deep 2, Quick none)

    @property
    def soft_wall_s(self) -> float:
        reserve = self.reserve_s if self.reserve_s is not None else min(60.0, self.wall_s * 0.15)
        return max(0.0, self.wall_s - reserve)

    def with_(self, **changes) -> 'BudgetConfig':
        return replace(self, **changes)


_PHASE_CALLS = {
    'quick': {2: 10, 3: 60, 4: 0, 5: 0, 6: 4},
    'standard': {2: 10, 3: 80, 4: 40, 5: 8, 6: 4},
    'deep': {2: 10, 3: 80, 4: 90, 5: 14, 6: 4},
}

CONFIGS: dict[str, BudgetConfig] = {
    'quick': BudgetConfig('quick', 6 * 60, 0, 90, 0, 0, 0, 8, 3_000_000, 30_000, _PHASE_CALLS['quick'], {3: 8}),
    'standard': BudgetConfig('standard', 60 * 60, 40 * 60, 160, 1, 5, 2, 14, 4_000_000, 40_000, _PHASE_CALLS['standard'], {3: 8}, full_investigation=True, refine_rounds=1),
    'deep': BudgetConfig('deep', 120 * 60, 90 * 60, 220, 3, 6, 4, 20, 6_000_000, 60_000, _PHASE_CALLS['deep'], {3: 8}, refine_rounds=2),
}


def config_for(depth: str) -> BudgetConfig:
    if depth not in CONFIGS:
        raise ValueError(f'Review depth must be one of {", ".join(DEPTHS)}')
    return CONFIGS[depth]


FOLLOWUP_LEVELS = ('answer', 'check', 'test')
FOLLOWUP_CALLS = {'answer': 6, 'check': 15, 'test': 25}
FOLLOWUP_INPUT_TOKENS = 1_500_000


def followup_config(level: str = 'test', *, input_tokens: int = FOLLOWUP_INPUT_TOKENS) -> BudgetConfig:
    """Limits of a follow-up message at its highest level (spec section 17)."""
    if level not in FOLLOWUP_LEVELS:
        raise ValueError('Level must be answer, check or test')
    test = level == 'test'
    return BudgetConfig(
        name=f'followup_{level}', wall_s=20 * 60, solver_s=15 * 60 if test else 0, model_turns=40, investigations=1 if test else 0,
        experiments_per_investigation=3 if test else 0, confirm_runs=2 if test else 0, images=6, input_tokens=input_tokens, output_tokens=60_000,
        phase_calls=dict(FOLLOWUP_CALLS), confirm_runs_per_investigation=2, reserve_s=60.0)


class BudgetTracker:
    def __init__(self, config: BudgetConfig, clock: Clock | None = None):
        self.config = config
        self.clock = clock or Clock()
        self.started = self.clock.now()
        self.solver_used = 0.0
        self.input_tokens = 0
        self.output_tokens = 0
        self.turns = 0
        self.images = 0
        self.calls: dict[Any, int] = {}
        self.bonus_calls: dict[Any, int] = {}        # tool calls a refine round adds to a phase
        self.bonus_confirm_runs = 0                  # confirm runs the refine rounds add
        self.images_by_phase: dict[Any, int] = {}
        self.investigations = 0
        self.confirm_runs = 0
        self.experiments = 0
        self.duplicates = 0
        self.refused = 0

    # ---- time -------------------------------------------------------------------------
    def elapsed(self) -> float:
        return max(0.0, self.clock.now() - self.started)

    def wall_left(self) -> float:
        return max(0.0, self.config.wall_s - self.elapsed())

    def soft_wall_reached(self) -> bool:
        return self.elapsed() >= self.config.soft_wall_s

    def deadline_reached(self) -> bool:
        return self.elapsed() >= self.config.wall_s

    # ---- counters ---------------------------------------------------------------------
    def solver_left(self) -> float:
        return max(0.0, self.config.solver_s - self.solver_used)

    def add_solver(self, seconds: float) -> None:
        self.solver_used += max(0.0, float(seconds or 0))

    def add_usage(self, payload: dict) -> None:
        """Token counts from a `usage` event; flat or nested under `usage`."""
        usage = payload.get('usage') if isinstance(payload.get('usage'), dict) else payload
        # Cached input costs about a tenth of fresh input, so it counts at that weight.
        for key, weight in (('input_tokens', 1), ('cache_creation_input_tokens', 1), ('cache_read_input_tokens', 0.1)):
            value = usage.get(key)
            if isinstance(value, (int, float)):
                self.input_tokens += int(value * weight)
        value = usage.get('output_tokens')
        if isinstance(value, (int, float)):
            self.output_tokens += int(value)
        turns = payload.get('num_turns')
        if isinstance(turns, int):
            self.turns = max(self.turns, turns)

    def add_turn(self) -> None:
        self.turns += 1

    def add_call(self, key) -> None:
        self.calls[key] = self.calls.get(key, 0) + 1

    def calls_left(self, key) -> int:
        cap = self.config.phase_calls.get(key)
        return 10 ** 9 if cap is None else max(0, cap + self.bonus_calls.get(key, 0) - self.calls.get(key, 0))

    def add_images(self, key, count: int = 1) -> None:
        self.images += count
        self.images_by_phase[key] = self.images_by_phase.get(key, 0) + count

    def images_left(self, key=None) -> int:
        left = self.config.images - self.images
        cap = self.config.phase_images.get(key)
        if cap is not None:
            left = min(left, cap - self.images_by_phase.get(key, 0))
        return max(0, left)

    # ---- limits -----------------------------------------------------------------------
    def global_exhausted(self) -> str | None:
        """The first budget that has ended, or None. Rule 2: after this only closing tools work."""
        c = self.config
        if self.soft_wall_reached():
            return 'wall_clock'
        if c.solver_s and self.solver_used >= c.solver_s:
            return 'solver_time'
        if self.turns >= c.model_turns:
            return 'model_turns'
        if self.input_tokens >= c.input_tokens:
            return 'input_tokens'
        if self.output_tokens >= c.output_tokens:
            return 'output_tokens'
        return None

    def snapshot(self, phase=None) -> dict:
        c = self.config
        return {
            'phase': phase, 'depth': c.name,
            'wall_s': {'used': round(self.elapsed(), 1), 'limit': c.wall_s, 'left': round(self.wall_left(), 1)},
            'solver_s': {'used': round(self.solver_used, 1), 'limit': c.solver_s, 'left': round(self.solver_left(), 1)},
            'turns': {'used': self.turns, 'limit': c.model_turns},
            'tool_calls': {str(k): v for k, v in self.calls.items()},
            'phase_call_limits': {str(k): v for k, v in c.phase_calls.items()},
            'images': {'used': self.images, 'limit': c.images},
            'investigations': {'used': self.investigations, 'limit': c.investigations},
            'experiments': {'used': self.experiments},
            'confirm_runs': {'used': self.confirm_runs, 'limit': c.confirm_runs + self.bonus_confirm_runs},
            'input_tokens': {'used': self.input_tokens, 'limit': c.input_tokens},
            'output_tokens': {'used': self.output_tokens, 'limit': c.output_tokens},
            'duplicates': self.duplicates, 'refused_calls': self.refused,
        }


# ---- the ledger: rules 3, 4, 5, 6 and 7 ---------------------------------------------------

CLOSED = ('confirmed', 'ruled_out', 'inconclusive', 'no_effect', 'could_not_test', 'looped', 'not_tested')


@dataclass
class Experiment:
    ref: str
    hash: str
    key: str
    suspect_id: str
    kind: str                 # base | whatif | matching | confirm
    label: str
    quality: str
    ok: bool                  # completed with a trusted result
    metrics: dict = field(default_factory=dict)
    delta: dict | None = None  # against the investigation's base: s11_db, f_hz
    cached: bool = False
    evidence_id: str | None = None
    repair: bool = False
    source_ref: str | None = None      # matching: the run the parts were applied to
    parts: list | None = None          # matching: the changed parts [{ref, position, kind, value, was, now}]
    ops: list = field(default_factory=list)   # the what-if operations of the geometry this result stands on


@dataclass
class SuspectState:
    id: str
    investigation_id: str
    code: str
    title: str
    status: str = 'open'
    reason: str | None = None
    experiments: list = field(default_factory=list)
    requests: dict = field(default_factory=dict)       # scenario hash -> times this suspect asked for it
    no_effect_streak: int = 0
    closed_base: str | None = None                      # base hash when it was closed (rule 4)

    @property
    def closed(self) -> bool:
        return self.status in CLOSED


@dataclass
class InvestigationState:
    id: str
    code: str
    suspects: list = field(default_factory=list)
    status: str = 'open'
    experiments: int = 0
    repairs: int = 0
    repairs_by_key: dict = field(default_factory=dict)
    failures_by_key: dict = field(default_factory=dict)
    base: Experiment | None = None
    good_enough: bool = False
    confirms: dict = field(default_factory=dict)       # 'base' | 'winner' -> Experiment
    outcome: str | None = None
    matching_runs: int = 0                              # matching experiments recorded (zero solver time)
    matching_unavailable: str | None = None             # why a matching experiment cannot be done on this board
    confirmation: dict | None = None                    # the screen against confirm comparison, once confirmed
    refining: bool = False                              # a refine round is open on this (closed) investigation
    extra_experiments: int = 0                          # experiments a refine round adds to the investigation's allowance
    refine_suspects: list = field(default_factory=list)  # ids of the suspects created for refine rounds
    refine_confirmations: dict = field(default_factory=dict)   # 'refine1' -> the screen against confirm comparison of that round's candidate


@dataclass
class Decision:
    action: str                       # run | cached | refuse
    code: str = ''                    # refusal code
    message: str = ''
    rule: str = ''                    # which stop rule fired
    cached: Experiment | None = None
    close: tuple | None = None        # (suspect, status, reason) closed as a result
    repair: bool = False


class Ledger:
    """Loop-prevention state for the investigations of one run. Pure logic, no I/O."""

    def __init__(self, config: BudgetConfig):
        self.config = config
        self.investigations: dict[str, InvestigationState] = {}
        self.suspects: dict[str, SuspectState] = {}
        self.cache: dict[str, Experiment] = {}              # scenario hash -> first completed experiment
        self.closed_titles: dict[str, str] = {}             # normalised hypothesis -> base hash at closure

    # ---- setup ------------------------------------------------------------------------
    def open_investigation(self, id: str, code: str) -> InvestigationState:
        inv = InvestigationState(id, code)
        self.investigations[id] = inv
        return inv

    def add_suspect(self, inv: InvestigationState, id: str, code: str, title: str) -> SuspectState:
        suspect = SuspectState(id, inv.id, code, title)
        inv.suspects.append(suspect)
        self.suspects[id] = suspect
        return suspect

    @staticmethod
    def normalise(title: str) -> str:
        return ''.join(ch for ch in title.lower() if ch.isalnum())

    def reopen_check(self, title: str, base_hash: str | None) -> bool:
        """Rule 4: a hypothesis ruled out stays closed unless the base geometry changed."""
        key = self.normalise(title)
        return key in self.closed_titles and self.closed_titles[key] == base_hash

    def close_suspect(self, suspect: SuspectState, status: str, reason: str, base_hash: str | None = None) -> None:
        suspect.status, suspect.reason, suspect.closed_base = status, reason, base_hash
        if status in ('ruled_out', 'no_effect'):
            self.closed_titles[self.normalise(suspect.title)] = base_hash

    # ---- deciding a simulation request --------------------------------------------------
    def decide(self, suspect: SuspectState, sc_hash: str, key: str, kind: str, *, optimisation: bool, base_hash: str | None) -> Decision:
        inv = self.investigations[suspect.investigation_id]
        if inv.status != 'open' and not inv.refining:
            return Decision('refuse', 'investigation_closed', 'This investigation is closed.')
        if suspect.closed:
            return Decision('refuse', 'suspect_closed', f'Suspect {suspect.code} is closed ({suspect.status}) and stays closed.', 'no_reopening')
        previous_requests = suspect.requests.get(sc_hash, 0)
        cached = self.cache.get(sc_hash)
        if cached is not None:
            if kind == 'base' and cached.ok and (inv.base is None or not inv.base.ok):
                inv.base = cached               # a later investigation reuses the stored base at no cost
            suspect.requests[sc_hash] = previous_requests + 1
            if previous_requests >= 1:                  # a repeat by the same suspect is a duplicate
                repeats = previous_requests
                decision = Decision('cached', cached=cached, rule='duplicate_scenario', message='Identical scenario: returned the stored result at no cost.')
                if repeats >= DUPLICATE_CLOSE_AT:
                    self.close_suspect(suspect, 'looped', 'The same scenario was requested again three times without new evidence.', base_hash)
                    decision.close = (suspect, 'looped', suspect.reason)
                return decision
            return Decision('cached', cached=cached, message='Identical to an earlier run: returned the stored result at no cost.')
        experiment = kind in ('whatif', 'matching')
        if experiment and (inv.base is None or not inv.base.ok):
            return Decision('refuse', 'base_required', 'Run the unmodified base scenario first so tests can be compared with it.')
        if experiment and inv.good_enough and optimisation:
            return Decision('refuse', 'target_met', 'The target is already met. Optimising further is not allowed; confirm and close.', 'good_enough')
        if experiment and inv.experiments >= self.config.experiments_per_investigation + inv.extra_experiments:
            return Decision('refuse', 'budget_exhausted', f'This investigation used its {self.config.experiments_per_investigation + inv.extra_experiments} experiments.', 'experiment_budget')
        repair = key in inv.failures_by_key
        if repair:
            if inv.repairs_by_key.get(key, 0) >= REPAIRS_PER_SCENARIO or inv.repairs >= REPAIRS_PER_INVESTIGATION:
                self.close_suspect(suspect, 'could_not_test', 'Setup repair limit reached.', base_hash)
                return Decision('refuse', 'repair_limit', 'Repair limit reached; this suspect ends as could_not_test.', 'repair_limit', close=(suspect, 'could_not_test', suspect.reason))
        suspect.requests[sc_hash] = previous_requests + 1
        return Decision('run', repair=repair)

    # ---- recording a completed run ------------------------------------------------------
    def record(self, suspect: SuspectState, exp: Experiment, *, meets_target: bool, base_hash: str | None, repair: bool) -> list[tuple]:
        """Update state after a real (non-cached) run. Returns closures as (suspect, status, reason)."""
        inv = self.investigations[suspect.investigation_id]
        closures: list[tuple] = []
        suspect.experiments.append(exp)
        self.cache[exp.hash] = exp          # failed runs too: an identical failed request is a duplicate, not a repair
        if repair:
            inv.repairs += 1
            inv.repairs_by_key[exp.key] = inv.repairs_by_key.get(exp.key, 0) + 1
        if exp.kind in ('whatif', 'matching'):
            inv.experiments += 1
        if exp.kind == 'matching':
            inv.matching_runs += 1
        if not exp.ok:
            inv.failures_by_key[exp.key] = inv.failures_by_key.get(exp.key, 0) + 1
            if exp.kind == 'confirm':          # a failed confirm run never closes the suspect that earned it
                return closures
            if repair or inv.repairs >= REPAIRS_PER_INVESTIGATION:   # no repair left to try
                self.close_suspect(suspect, 'could_not_test', 'The scenario failed its trust checks and no repair is left.', base_hash)
                closures.append((suspect, 'could_not_test', suspect.reason))
            return closures
        if exp.quality == 'confirm':
            return closures
        if exp.kind == 'base':
            if inv.base is None or not inv.base.ok:
                inv.base = exp
            if meets_target:
                inv.good_enough = True
            return closures
        if meets_target:
            inv.good_enough = True
        # Rule 5: two experiments in a row that barely move the result close the suspect.
        small = moved_little(exp.delta, NO_PROGRESS_DB, NO_PROGRESS_HZ, frequency=exp.kind != 'matching')
        suspect.no_effect_streak = suspect.no_effect_streak + 1 if small else 0
        if suspect.no_effect_streak >= 2:
            self.close_suspect(suspect, 'no_effect', 'Two experiments in a row moved the in-band result by under 0.5 dB and 5 MHz.', base_hash)
            closures.append((suspect, 'no_effect', suspect.reason))
        return closures

    def winner(self, inv: InvestigationState) -> Experiment | None:
        """The best trusted what-if or matching experiment that improved the in-band result by IMPROVEMENT_DB.

        A winner stands for a pair: the what-if operations of its geometry and the matching parts it changed.
        """
        best = None
        for suspect in inv.suspects:
            if suspect.id in inv.refine_suspects:       # refine candidates are judged on their own (refine_candidate)
                continue
            for exp in suspect.experiments:
                if exp.kind not in ('whatif', 'matching') or not exp.ok or exp.quality != 'screen' or not exp.delta or not is_improvement(exp.delta):
                    continue
                if best is None or rank_key(exp.delta) > rank_key(best.delta):
                    best = exp
        return best

    def refine_candidate(self, inv: InvestigationState, suspect_id: str) -> Experiment | None:
        """The best trusted screen what-if or matching experiment of one refine round that improved the in-band result by IMPROVEMENT_DB."""
        suspect = self.suspects.get(suspect_id)
        best = None
        for exp in (suspect.experiments if suspect else []):
            if exp.kind not in ('whatif', 'matching') or not exp.ok or exp.quality != 'screen' or not exp.delta or not is_improvement(exp.delta):
                continue
            if best is None or rank_key(exp.delta) > rank_key(best.delta):
                best = exp
        return best

    # ---- not stopping early (Standard) -----------------------------------------------------
    @staticmethod
    def suspect_tested(suspect: SuspectState) -> bool:
        return any(e.kind in ('whatif', 'matching') for e in suspect.experiments) or suspect.status in ('no_effect', 'looped', 'could_not_test')

    def closing_gaps(self, inv: InvestigationState) -> list[str]:
        """What is still missing before an investigation may close without an improvement. Empty when nothing is required."""
        if not self.config.full_investigation:
            return []
        gaps = []
        if inv.matching_runs == 0 and not inv.matching_unavailable:
            gaps.append('no matching experiment yet (run tune_matching on a trusted run, for example the base, and pass the suspect it tests)')
        need = min(MIN_SUSPECTS_TESTED, len(inv.suspects))
        untested = [x for x in inv.suspects if not self.suspect_tested(x) and not x.closed]
        tested = len(inv.suspects) - len([x for x in inv.suspects if not self.suspect_tested(x)])
        if tested < need:
            gaps.append(f'only {tested} of {need} required suspects tested; not tested yet: ' + ', '.join(f'{x.code} ({x.title})' for x in untested[:3]))
        return gaps
