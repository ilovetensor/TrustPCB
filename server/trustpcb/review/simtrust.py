"""What a finding may say about a board simulation that rests on an input nobody gave.

A simulator needs inputs: a load current, a part's power, an ambient temperature. When the engineer's request and the datasheets give none, the
simulator uses a default and says so (`source: default`). A number that comes out of a default is a number about the default. The server holds
the finding to that:

  * the default inputs are written into the finding's assumptions, by code, in plain words, and the row that lists them is attached;
  * a sentence that quotes a number from such a run must say it is conditional (assumed, default, estimated, with the assumed ...), or the
    claim check refuses it (`default_stated_as_fact`), as it refuses any number the evidence does not support;
  * a finding that rests on a default cannot be rated critical or high on that number alone (`severity_rests_on_default`): give the real
    input, run the simulation again, and the cap goes away.

A result that failed its trust checks is refused earlier, in `FindingEngine.prepare` (`trust_failed`), whatever its inputs.
"""
from __future__ import annotations

from typing import Any

from . import simmetrics as sm
from .evidence import BOARD_SIM, CONFIRM, COMPARISON, SIM_ASSUMPTIONS, SIMULATION  # noqa: F401
from .number_check import Problem, _explains, extract, sentence_spans

CAPPED = ('critical', 'high')


def is_sim_row(row: dict) -> bool:
    return row.get('type') == BOARD_SIM and bool((row.get('values') or {}).get('sim_kind')) and not (row.get('values') or {}).get('comparison')


def _decisive(values: dict) -> list[str]:
    """The default inputs of a run that decide its numbers (runs stored before the split list all their defaults)."""
    return values['decisive_defaults'] if 'decisive_defaults' in values else (values.get('defaults_used') or [])


def default_rows(rows: list[dict]) -> list[dict]:
    """The cited simulation rows whose numbers rest on at least one default input that matters (a load, a power, a voltage, the ambient)."""
    return [r for r in rows if is_sim_row(r) and _decisive(r.get('values') or {})]


def defaults_of_rows(rows: list[dict], decisive: bool = False) -> list[dict]:
    """The default inputs of the cited runs (every one, or only the decisive ones), each once."""
    seen, out = set(), []
    for row in rows:
        if not is_sim_row(row):
            continue
        keep = set(_decisive(row['values'])) if decisive else None
        for a in (row['values'].get('assumptions') or []):
            if a.get('source') == 'default' and (keep is None or a['name'] in keep) and (a['name'], a.get('value')) not in seen:
                seen.add((a['name'], a.get('value')))
                out.append(a)
    return out


def attach_assumption_rows(universe: list[dict], rows: list[dict]) -> list[dict]:
    """`rows` plus the assumption row of every cited simulation run, so the inputs travel with the numbers."""
    have = {r['id'] for r in rows}
    by_code = {r.get('code'): r for r in universe}
    out = list(rows)
    for row in rows:
        if not is_sim_row(row):
            continue
        extra = by_code.get((row['values'] or {}).get('assumption_evidence'))
        if extra is not None and extra['id'] not in have:
            out.append(extra)
            have.add(extra['id'])
    return out


def assumption_lines(rows: list[dict], existing: list[str]) -> list[str]:
    """The finding's assumptions with a line for every default input of the cited runs that the finding does not already state."""
    lines = list(existing)
    text = ' '.join(lines).lower()
    for a in defaults_of_rows(rows):
        label = sm.assumption_label(a).lower()
        value = a.get('value')
        shown = f'{value:g}' if sm.is_number(value) else str(value)
        if label in text and shown in text:
            continue
        lines.append(sm.assumption_line(a))
    return lines


def severity_problem(severity: str, rows: list[dict]) -> tuple[str, str] | None:
    """(code, message) when a finding rated critical or high rests on a simulation with a default input and cites no measurement that corroborates it."""
    if severity not in CAPPED:
        return None
    defaults = defaults_of_rows(rows, decisive=True)
    if not defaults:
        return None
    if any((r.get('values') or {}).get('kind') == 'measured' and r.get('type') == 'candidate' for r in rows):
        return None
    names = ', '.join(sm.assumption_text(a) for a in defaults[:4])
    return ('severity_rests_on_default',
            f'This finding is rated {severity} on a simulation that used default inputs nobody gave ({names}). A number that comes from a default is not a fact about this board. '
            'Rate it medium or lower, or get the real input (the engineer\'s request or a datasheet, set with set_sim_input), run the simulation again and cite that run.')


# ---- the antenna simulation on an assumed stackup ------------------------------------------------------------------------------

def antenna_rows(rows: list[dict]) -> list[dict]:
    """The cited antenna simulation rows (screen runs, confirm runs and their comparisons): simulated, and not a board simulation."""
    return [r for r in rows if r.get('type') in (SIMULATION, CONFIRM, COMPARISON) and (r.get('values') or {}).get('kind') == 'simulated' and not (r.get('values') or {}).get('sim_kind')]


def stackup_is_assumed(board: Any, rows: list[dict] | None = None) -> bool:
    """Did the board file give no real stackup? (the simulation then runs on a typical board of an assumed thickness, which moves a printed antenna's resonance)"""
    stack = getattr(board, 'stackup', None) or {}
    if stack.get('assumed') or stack.get('source') in ('default', 'assumed_by_user'):
        return True
    return any((r.get('values') or {}).get('stackup_assumed') for r in rows or [])


def stackup_line(board: Any) -> str:
    total = (getattr(board, 'stackup', None) or {}).get('total_thickness_mm')
    size = f'{total:g} mm ' if sm.is_number(total) else ''
    return (f'Stackup assumed: the board file gives no real stackup, so the antenna simulation uses a typical {size}FR-4 board. '
            'The resonant frequency and match of a printed antenna depend strongly on the dielectric thickness and permittivity.')


def stackup_assumption(board: Any, rows: list[dict], existing: list[str]) -> list[str]:
    """`existing` plus the stackup assumption, by code, when a cited antenna simulation ran on an assumed stackup and the finding does not already state it."""
    if not antenna_rows(rows) or not stackup_is_assumed(board, rows):
        return list(existing)
    if any('stackup' in line.lower() and ('assum' in line.lower() or 'default' in line.lower()) for line in existing):
        return list(existing)
    return [*existing, stackup_line(board)]


def antenna_severity_problem(severity: str, rows: list[dict], board: Any) -> tuple[str, str] | None:
    """(code, message) when a finding rated critical or high rests on antenna simulation runs on an assumed stackup that never reached confirm quality.

    The cap for default inputs applies here too: high needs a confirm run, unless a cited analyzer or KiCad measurement corroborates it."""
    if severity not in CAPPED:
        return None
    runs = antenna_rows(rows)
    if not runs or not stackup_is_assumed(board, rows):
        return None
    if any((r.get('values') or {}).get('quality') == 'confirm' for r in runs):
        return None
    if any((r.get('values') or {}).get('kind') == 'measured' and r.get('type') == 'candidate' for r in rows):
        return None
    return ('severity_rests_on_default',
            f'This finding is rated {severity} on antenna simulation runs of screen quality, on a stackup the board file does not give (a typical board is assumed). A screen run compares a change with the base; '
            'it is not an absolute result, and the assumed stackup moves the resonance. Rate it medium or lower, or run the confirm (confirm_result) and cite those runs.')


def conditional(sentence: str) -> bool:
    low = sentence.lower()
    return any(word in low for word in sm.ASSUME_WORDS)


def stated_as_fact(texts: dict[str, str], *, scope_rows: list[dict], allowed: list[Any]) -> list[Problem]:
    """Sentences that quote a number from a default-based run without saying it is conditional."""
    risky = {r['code']: r for r in default_rows(scope_rows)}
    if not risky:
        return []
    out: list[Problem] = []
    for name, text in texts.items():
        if name == 'title' or name.startswith(('assumptions', 'not_tested')):
            continue
        for start, end in sentence_spans(text or ''):
            sentence = text[start:end].strip()
            if conditional(sentence):
                continue
            for q in extract(sentence):
                matches = [a for a in allowed if _explains(q, a) and a.source]
                if not matches or any(a.source not in risky or (a.name or '').startswith('assume_') for a in matches):
                    continue
                row = risky[matches[0].source]
                defaults = defaults_of_rows([row], decisive=True)
                names = ', '.join(sm.assumption_text(a) for a in defaults[:3])
                quote = sentence if len(sentence) <= 140 else sentence[:137] + '...'
                out.append(Problem('default_stated_as_fact', name, sentence,
                                   f'In {name}: "{quote}" quotes {q.raw} from {row["code"]}, which used default inputs nobody gave ({names}). Say the number is conditional on them: '
                                   'for example "with the assumed default ..., the simulation gives ...", or "if the real value is ...". Do not state it as a fact about the board.',
                                   {'number': q.raw, 'run': row['code'], 'defaults': [a['name'] for a in defaults]}))
                break
    return out


# ---- impedance: the field solve against the closed form -----------------------------------------------------------------------

LABELLED = ('closed form', 'closed-form', 'estimate', 'analyzer', 'ignores', 'replaced', 'instead')
QUASI = ('quasi-static', 'quasi static')


def signal_rows(universe: list[dict]) -> list[dict]:
    return [r for r in universe if is_sim_row(r) and (r['values'] or {}).get('sim_kind') == 'signal' and ((r['values'] or {}).get('trust') or {}).get('status') == 'ok']


def _near(a: float, b: float, tol: float) -> bool:
    return abs(a - b) <= tol


def signal_problems(texts: dict[str, str], *, universe: list[dict]) -> list[Problem]:
    """Where a signal run solved a net's field, its impedance is the field-solved one.

    * The closed-form figure of the impedance analyzer for that net may be quoted only as the estimate it is (it ignores coplanar ground beside the track).
    * A finding that quotes it must also give the field-solved value of that net, so the two are never stated as if they agreed.
    * A field-solved number for a net above the model's quasi-static limit (an RF net) is not valid at its band: the sentence says so.
    """
    rows = signal_rows(universe)
    if not rows:
        return []
    out: list[Problem] = []
    solved: dict[str, tuple[float, str]] = {}
    beyond: dict[str, tuple[float, str]] = {}
    closed: dict[str, tuple[float, str]] = {}
    for row in rows:
        v = row['values']
        for c in v.get('cross_check') or []:
            if sm.is_number(c.get('analyzer_closed_form_ohm')) and sm.is_number(c.get('field_solver_on_the_real_board_median_ohm')):
                closed[c['net']] = (float(c['analyzer_closed_form_ohm']), row['code'])
                solved[c['net']] = (float(c['field_solver_on_the_real_board_median_ohm']), row['code'])
        for net, limit in (v.get('beyond_quasi_static') or {}).items():
            beyond[net] = (limit, row['code'])
    whole = ' '.join(texts.values())
    for name, text in texts.items():
        if name == 'title':
            continue
        spans = sentence_spans(text or '')
        for index, (start, end) in enumerate(spans):
            sentence = text[start:end].strip()
            quote = sentence if len(sentence) <= 140 else sentence[:137] + '...'
            low = sentence.lower()
            for q in extract(sentence):
                if q.dimension != 'resistance':
                    continue
                for net, (value, code) in closed.items():
                    if _near(q.base, value, max(q.tolerance, 1e-6)) and not _near(q.base, solved[net][0], max(q.tolerance, 1e-6)):
                        if not any(word in low for word in LABELLED):
                            out.append(Problem('closed_form_stated_as_fact', name, sentence, f'In {name}: "{quote}" gives {q.raw}, the closed-form figure of the impedance analyzer for {net}. A field solve ({code}) '
                                               f'covers {net} and found a median of {solved[net][0]:g} ohm on the real board, because the closed form ignores coplanar ground beside the track. State the solved value, '
                                               'and give the closed-form figure only as the estimate it replaces.', {'net': net, 'field_solved_ohm': solved[net][0], 'evidence': code}))
                        elif not any(_near(x.base, solved[net][0], max(x.tolerance, 0.05)) for x in extract(whole) if x.dimension == 'resistance'):
                            out.append(Problem('field_solve_not_stated', name, sentence, f'In {name}: "{quote}" quotes the closed-form impedance of {net} but not the field-solved value ({solved[net][0]:g} ohm, {code}). '
                                               'Where a field solve covers a net, state its value as the impedance and the closed form only as the estimate it replaces; never state both as if they agree.',
                                               {'net': net, 'field_solved_ohm': solved[net][0], 'evidence': code}))
                for net, (limit, code) in beyond.items():
                    value = solved.get(net, (None, ''))[0]
                    if value is not None and net.lower() in low and _near(q.base, value, max(q.tolerance, 0.05)):
                        around = ' '.join(text[a:b].lower() for a, b in spans[max(0, index - 1):index + 2])          # this sentence, the one before and the one after
                        if not any(word in around for word in QUASI):
                            out.append(Problem('quasi_static_limit_unsaid', name, sentence, f'In {name}: "{quote}" gives the field-solved impedance of {net}, a net with edge content above the model\'s quasi-static limit '
                                               f'({limit:g} GHz, {code}). Say that the number is a quasi-static estimate and is not valid at the RF band.', {'net': net, 'limit_ghz': limit, 'evidence': code}))
    return out
