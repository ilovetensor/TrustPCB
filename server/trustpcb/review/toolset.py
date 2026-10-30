"""The catalogue of review tools: names, descriptions, strict schemas and where each one works.

Phase numbers follow spec section 7; the levels are the follow-up levels of section 17.
Descriptions are short on purpose: the phase brief carries the guidance.
"""
from __future__ import annotations

from .handlers import MARK_SCHEMA, OP_SCHEMA, Handlers
from .investigate import InvestigationMixin
from .tools import BOOL, INT, NUM, STR, ToolDef, arr, obj, text


class AllHandlers(InvestigationMixin, Handlers):
    pass


ALL_PHASES = frozenset({2, 3, 4, 5, 6})
ALL_LEVELS = frozenset({'answer', 'check', 'test'})
CHECK_UP = frozenset({'check', 'test'})
TEST = frozenset({'test'})

CODE = text(1, 40)
SUSPECT_SCHEMA = obj({'title': text(3, 160), 'why': text(3, 400), 'test': text(3, 300)}, ['title', 'why'])

FINDING_PROPS = {
    'title': text(5, 160), 'severity': {'type': 'string', 'enum': ['critical', 'high', 'medium', 'low', 'info']},
    'what': text(5, 1200), 'why_it_matters': text(5, 1200), 'recommendation': text(5, 1200),
    'rule': text(1, 60), 'candidate_ids': arr(text(1, 80), 0, 20), 'evidence_ids': arr(text(1, 20), 0, 12),
    'targets': arr(text(1, 80), 0, 20), 'marks': arr(MARK_SCHEMA, 0, 12),
    'scope': {'type': 'string', 'enum': ['targets', 'board']}, 'assumptions': arr(text(1, 300), 0, 8), 'not_tested': arr(text(1, 300), 0, 8),
}


def define(name, description, schema, handler, *, phases=frozenset(), levels=frozenset(), free=False, closing=False, image=False, trail=None) -> ToolDef:
    return ToolDef(name, description, schema, handler, phases=phases, levels=levels, free=free, closing=closing, image=image, trail=trail)


def build_review_tools(h: AllHandlers) -> list[ToolDef]:
    P = frozenset
    return [
        define('get_status', 'Current phase, allowed tools, time and budgets left, candidate counts.', obj({}), h.get_status,
               phases=ALL_PHASES, levels=ALL_LEVELS, free=True, closing=True, trail=lambda a: 'Checking status and budgets'),
        define('advance_phase', 'Close the current phase and move to the next. Returns the brief for the new phase.', obj({'note': text(1, 300)}), h.advance_phase,
               phases=P({2, 3, 4, 5}), free=True, closing=True, trail=lambda a: 'Moving to the next phase'),
        define('get_board_summary', 'Parts, nets, layers, stackup, goal, assumptions and candidate counts for this board.', obj({}), h.get_board_summary,
               phases=P({2}), trail=lambda a: 'Reading the board summary'),
        define('read_schematic', 'Paged part and net data. kind: parts, part, nets or net. query filters the list or names the part or net.',
               obj({'kind': {'type': 'string', 'enum': ['parts', 'part', 'nets', 'net']}, 'query': text(1, 80), 'page': {'type': 'integer', 'minimum': 1, 'maximum': 200}}, ['kind']),
               h.read_schematic, phases=P({2, 3}), levels=ALL_LEVELS, trail=lambda a: f'Reading {a.get("kind")} data' + (f' ({a["query"]})' if a.get('query') else '')),
        define('read_datasheet', 'List uploaded datasheets, or read one page of one as text.',
               obj({'name': text(1, 120), 'page': {'type': 'integer', 'minimum': 1, 'maximum': 500}}), h.read_datasheet,
               phases=P({2, 3}), levels=ALL_LEVELS, trail=lambda a: 'Reading a datasheet' + (f': {a["name"]}' if a.get('name') else '')),
        define('view_board', 'A rendered image of the board. view: top, bottom or layer (give layer). Optional region [x0,y0,x1,y1] in mm, targets or candidate_ids to mark.',
               obj({'view': {'type': 'string', 'enum': ['top', 'bottom', 'layer']}, 'layer': text(1, 20), 'region': arr(NUM, 4, 4),
                    'targets': arr(text(1, 80), 0, 20), 'candidate_ids': arr(text(1, 80), 0, 10)}, ['view']),
               h.view_board, phases=P({2, 3}), levels=ALL_LEVELS, image=True,
               trail=lambda a: 'Looking at the ' + (a.get('layer') or a.get('view', 'board')) + (' (zoomed)' if a.get('region') else '')),
        define('submit_board_brief', 'Save what the board is, the role of each important part, the risks and the priorities. Ends phase 2.',
               obj({'purpose': text(10, 600),
                    'wireless': obj({'has_radio': BOOL, 'band_hz': arr(NUM, 2, 2), 'antenna_kind': {'type': 'string', 'enum': ['printed', 'chip', 'module', 'none', 'unknown']},
                                     'antenna_ref': text(1, 20), 'feed_net': text(1, 60), 'notes': text(1, 300)}),
                    'part_roles': {'type': 'object', 'additionalProperties': text(1, 40)},
                    'risks': arr(obj({'text': text(3, 300), 'targets': arr(text(1, 80), 0, 10)}, ['text']), 0, 8),
                    'priorities': arr(text(3, 200), 0, 6), 'unknowns': arr(text(3, 200), 0, 6)}, ['purpose', 'part_roles', 'risks', 'priorities']),
               h.submit_board_brief, phases=P({2}), closing=True, trail=lambda a: 'Writing the board brief'),
        define('list_candidates', 'Analyzer and KiCad results, grouped by rule and paged. state: new (default), recorded, grouped, dismissed or any.',
               obj({'rule': text(1, 60), 'severity': {'type': 'string', 'enum': ['high', 'medium', 'low', 'info']}, 'state': {'type': 'string', 'enum': ['new', 'recorded', 'grouped', 'dismissed', 'any']},
                    'group_by': {'type': 'string', 'enum': ['rule', 'none']}, 'page': {'type': 'integer', 'minimum': 1, 'maximum': 200}}),
               h.list_candidates, phases=P({3}), levels=CHECK_UP, trail=lambda a: 'Listing candidates' + (f' for {a["rule"]}' if a.get('rule') else '')),
        define('record_finding', 'Create or update a finding. Class is set by the server from the evidence. Every number with a unit must match evidence. '
               'Pass candidate_ids and/or evidence_ids, and targets or marks (or scope board).', obj(FINDING_PROPS | {'update': text(1, 12)}, ['title', 'severity', 'what', 'why_it_matters', 'recommendation']),
               h.record_finding, phases=P({3, 4, 5}), trail=lambda a: f'Recording a finding: {a.get("title", "")[:80]}'),
        define('dismiss_candidate', 'Mark candidates as harmless, with a reason.', obj({'candidate_ids': arr(text(1, 80), 1, 50), 'reason': text(8, 400)}, ['candidate_ids', 'reason']),
               h.dismiss_candidate, phases=P({3}), trail=lambda a: f'Dismissing {len(a.get("candidate_ids", []))} harmless candidate(s)'),
        define('show_on_board', 'Spotlight items on the live board. Pass target ids or a finding_code.',
               obj({'targets': arr(text(1, 80), 0, 20), 'finding_code': text(1, 12), 'label': text(1, 120)}), h.show_on_board,
               phases=P({3, 4}), levels=ALL_LEVELS, trail=lambda a: 'Showing ' + (a.get('label') or 'items') + ' on the board'),
        define('open_investigation', 'Start an investigation: one question and 1 to 3 ranked suspects.',
               obj({'question': text(10, 400), 'suspects': arr(SUSPECT_SCHEMA, 1, 3)}, ['question', 'suspects']), h.open_investigation,
               phases=P({4}), levels=TEST, trail=lambda a: f'Opening an investigation: {a.get("question", "")[:100]}'),
        define('run_simulation', 'Run a base (no whatif_ops) or what-if scenario at screen quality for one suspect. Identical scenarios return the stored result.',
               obj({'suspect_id': CODE, 'label': text(1, 120), 'whatif_ops': arr(OP_SCHEMA, 0, 3),
                    'setup': obj({'region_margin_mm': {'type': 'number', 'minimum': 1, 'maximum': 40}, 'max_timesteps': {'type': 'integer', 'minimum': 1000, 'maximum': 2000000},
                                  'end_criteria_db': {'type': 'number', 'minimum': -80, 'maximum': -20}, 'mesh': {'type': 'string', 'enum': ['normal', 'fine']}})},
                   ['suspect_id']),
               h.run_simulation, phases=P({4}), levels=TEST, trail=lambda a: 'Running a simulation: ' + (a.get('label') or ('base case' if not a.get('whatif_ops') else 'what-if'))),
        define('view_result', 'View a chart or field image of a stored result. kind: s11, smith, pattern, field or current.',
               obj({'ref': CODE, 'kind': {'type': 'string', 'enum': ['s11', 'smith', 'pattern', 'field', 'current']}}, ['ref', 'kind']), h.view_result,
               phases=P({4, 5}), levels=TEST, image=True, trail=lambda a: f'Viewing the {a.get("kind")} of {a.get("ref")}'),
        define('compare_results', 'Numeric difference between two stored results of the same quality (b minus a).', obj({'a': CODE, 'b': CODE}, ['a', 'b']), h.compare_results,
               phases=P({4, 5}), levels=TEST, trail=lambda a: f'Comparing {a.get("a")} with {a.get("b")}'),
        define('tune_matching', 'Calculate standard-value matching parts from a stored, trusted result. Changes no geometry and costs no solver time.',
               obj({'ref': CODE, 'topology': {'type': 'string', 'enum': ['auto', 'series_l_shunt_c', 'shunt_c_series_l', 'series_c_shunt_l']}}, ['ref']), h.tune_matching,
               phases=P({4, 5}), levels=CHECK_UP, trail=lambda a: f'Calculating matching values from {a.get("ref")}'),
        define('conclude_suspect', 'Conclude one suspect as confirmed, ruled_out or inconclusive, with reasoning that cites only tool numbers.',
               obj({'suspect_id': CODE, 'verdict': {'type': 'string', 'enum': ['confirmed', 'ruled_out', 'inconclusive']}, 'reasoning': text(10, 1200), 'evidence_ids': arr(text(1, 20), 0, 12)},
                   ['suspect_id', 'verdict', 'reasoning']), h.conclude_suspect, phases=P({4}), levels=TEST, closing=True,
               trail=lambda a: f'Concluding {a.get("suspect_id")}: {a.get("verdict")}'),
        define('close_investigation', 'Close the investigation with its outcome (improved, no_improvement, inconclusive) and the linked finding code.',
               obj({'investigation_id': CODE, 'outcome': {'type': 'string', 'enum': ['improved', 'no_improvement', 'inconclusive']}, 'summary': text(10, 1200), 'finding_code': text(1, 12)},
                   ['investigation_id', 'outcome', 'summary']), h.close_investigation, phases=P({4}), levels=TEST, closing=True,
               trail=lambda a: f'Closing investigation {a.get("investigation_id")}: {a.get("outcome")}'),
        define('confirm_result', 'One detailed (confirm quality) run of the base or of the winning what-if. A fix needs both.',
               obj({'investigation_id': CODE, 'which': {'type': 'string', 'enum': ['base', 'winner']}}, ['investigation_id', 'which']), h.confirm_result,
               phases=P({5}), levels=TEST, trail=lambda a: f'Confirming the {a.get("which")} with a detailed run'),
        define('submit_summary', 'Submit the plain-language summary and up to five top actions. Every number must match the evidence.',
               obj({'summary': text(20, 2400), 'top_actions': arr(text(5, 400), 1, 5)}, ['summary', 'top_actions']), h.submit_summary,
               phases=P({6}), closing=True, trail=lambda a: 'Writing the summary'),
    ]
