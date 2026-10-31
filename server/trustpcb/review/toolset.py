"""The catalogue of review tools: names, descriptions, strict schemas and where each one works.

Phase numbers are the review phases; the levels are the follow-up levels.
Descriptions are short on purpose: the phase brief carries the guidance.
"""
from __future__ import annotations

from .handlers import MARK_SCHEMA, OP_SCHEMA, Handlers
from .investigate import InvestigationMixin
from .refine import RefineMixin
from .tools import BOOL, NUM, ToolDef, arr, obj, text


class AllHandlers(RefineMixin, InvestigationMixin, Handlers):      # InvestigationMixin carries the board-simulation tools (boardsim.BoardSimMixin)
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
    'rule': text(1, 60), 'candidate_ids': arr(text(1, 80), 0, 60), 'evidence_ids': arr(text(1, 20), 0, 12),
    'targets': arr(text(1, 80), 0, 20), 'marks': arr(MARK_SCHEMA, 0, 12),
    'severity_reason': text(1, 300), 'scope': {'type': 'string', 'enum': ['targets', 'board']}, 'assumptions': arr(text(1, 300), 0, 8), 'not_tested': arr(text(1, 300), 0, 8),
}


SIM_KIND = {'type': 'string', 'enum': ['power', 'thermal', 'signal']}
SIM_OP = {'type': 'object', 'properties': {'op': text(1, 40)}, 'required': ['op'], 'additionalProperties': True}


def define(name, description, schema, handler, *, phases=frozenset(), levels=frozenset(), free=False, closing=False, image=False, trail=None) -> ToolDef:
    return ToolDef(name, description, schema, handler, phases=phases, levels=levels, free=free, closing=closing, image=image, trail=trail)


def build_review_tools(h: AllHandlers) -> list[ToolDef]:
    P = frozenset
    return [
        define('get_status', 'Current phase, allowed tools, time and budgets left, candidate counts.', obj({}), h.get_status,
               phases=ALL_PHASES, levels=ALL_LEVELS, free=True, closing=True, trail=lambda a: 'Checking status and budgets'),
        define('advance_phase', 'Close the phase and move on; returns the next brief. Phase 3: layout_reasons {group: 60+ characters} says why a group merits no finding. Phase 4 needs a base run '
               'of every chosen check; not_run_reasons {kind: 30+ characters} says why one was not run. Phase 5: a confirm that missed the target opens a refine round (decline_refine: a reason citing confirm evidence).', obj({'note': text(1, 300), 'layout_reasons': {'type': 'object', 'additionalProperties': text(1, 600)}, 'decline_refine': text(1, 600), 'not_run_reasons': {'type': 'object', 'additionalProperties': text(1, 600)}}), h.advance_phase,
               phases=P({2, 3, 4, 5}), free=True, closing=True, trail=lambda a: 'Moving to the next phase'),
        define('get_board_summary', 'Parts, nets, layers, stackup, goal, assumptions and candidate counts for this board.', obj({}), h.get_board_summary,
               phases=P({2}), trail=lambda a: 'Reading the board summary'),
        define('read_schematic', 'Paged part and net data. kind: parts, part, nets or net. query filters the list or names the part or net. kind state with refs: what the schematic file says now '
               '(no_connect markers, do-not-populate flags, pin names, board nets); read it before recommending a schematic or board change.',
               obj({'kind': {'type': 'string', 'enum': ['parts', 'part', 'nets', 'net', 'state']}, 'query': text(1, 80), 'refs': arr(text(1, 20), 1, 12), 'page': {'type': 'integer', 'minimum': 1, 'maximum': 200}}, ['kind']),
               h.read_schematic, phases=P({2, 3}), levels=ALL_LEVELS, trail=lambda a: f'Reading {a.get("kind")} data' + (f' ({a["query"]})' if a.get('query') else '')),
        define('read_datasheet', 'List uploaded datasheets, or read one page of one as text. With mpn and question (when datasheets are part of this review) it reads the supplier datasheet of that part and answers the question with a citation.',
               obj({'name': text(1, 120), 'page': {'type': 'integer', 'minimum': 1, 'maximum': 500}, 'mpn': text(2, 80), 'question': text(3, 300)}), h.read_datasheet,
               phases=P({2, 3}), levels=ALL_LEVELS, trail=lambda a: 'Reading a datasheet' + (f': {a["name"]}' if a.get('name') else '')),
        define('view_board', 'A rendered image of the board. Presets: view top, bottom or layer (give layer); around a target id (ref:U3, net:ANT_FEED, zone_role:radio, '
               'candidate:...) with margin_mm; or a region [x0,y0,x1,y1] in mm. annotate draws the candidate marks inside the window (numbered). '
               'targets or candidate_ids mark specific items. In phase 3 the review needs close region views of the areas that matter.',
               obj({'view': {'type': 'string', 'enum': ['top', 'bottom', 'layer']}, 'layer': text(1, 20), 'region': arr(NUM, 4, 4),
                    'around': text(1, 80), 'margin_mm': {'type': 'number', 'minimum': 0, 'maximum': 40}, 'annotate': BOOL,
                    'targets': arr(text(1, 80), 0, 20), 'candidate_ids': arr(text(1, 80), 0, 10)}),
               h.view_board, phases=P({2, 3}), levels=ALL_LEVELS, image=True,
               trail=lambda a: 'Looking at ' + (a['around'] if a.get('around') else 'the ' + (a.get('layer') or a.get('view', 'board'))) + (' (zoomed)' if a.get('region') or a.get('around') else '')
               + (' with candidate marks' if a.get('annotate') else '')),
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
               'Pass rule_group to cover every still-new candidate of one rule (one finding for a whole group), and/or candidate_ids and evidence_ids, '
               'and targets or marks (or scope board).',
               obj(FINDING_PROPS | {'update': text(1, 12), 'rule_group': text(1, 60)}, ['title', 'severity', 'what', 'why_it_matters', 'recommendation']),
               h.record_finding, phases=P({3, 4, 5}), trail=lambda a: f'Recording a finding: {a.get("title", "")[:80]}'),
        define('dismiss_rule_group', 'Dismiss every still-new candidate of one rule as harmless, with a specific reason for this board. '
               'A group with medium or high results needs a real explanation.', obj({'rule': text(1, 60), 'reason': text(8, 600)}, ['rule', 'reason']),
               h.dismiss_rule_group, phases=P({3}), trail=lambda a: f'Dismissing the {a.get("rule", "")} group as harmless'),
        define('dismiss_candidate', 'Mark candidates as harmless, with a reason.', obj({'candidate_ids': arr(text(1, 80), 1, 50), 'reason': text(8, 400)}, ['candidate_ids', 'reason']),
               h.dismiss_candidate, phases=P({3}), trail=lambda a: f'Dismissing {len(a.get("candidate_ids", []))} harmless candidate(s)'),
        define('show_on_board', 'Spotlight items on the live board. Pass target ids or a finding_code.',
               obj({'targets': arr(text(1, 80), 0, 20), 'finding_code': text(1, 12), 'label': text(1, 120)}), h.show_on_board,
               phases=P({3, 4}), levels=ALL_LEVELS, trail=lambda a: 'Showing ' + (a.get('label') or 'items') + ' on the board'),
        define('open_investigation', 'Start an investigation: one question and 1 to 3 ranked suspects. kind is antenna (default) or, when chosen for this review, power, thermal or signal: '
               'one investigation per kind, tested with run_board_sim.',
               obj({'question': text(10, 400), 'suspects': arr(SUSPECT_SCHEMA, 1, 3), 'kind': {'type': 'string', 'enum': ['antenna', 'power', 'thermal', 'signal']}}, ['question', 'suspects']), h.open_investigation,
               phases=P({4}), levels=TEST, trail=lambda a: f'Opening an investigation: {a.get("question", "")[:100]}'),
        define('run_simulation', 'Run a base (no whatif_ops) or what-if scenario at screen quality for one suspect. Identical scenarios return the stored result. '
               'In a refine round (phase 5) it needs whatif_ops and a reason that cites confirm evidence by its code; the suspect is the round\'s own.',
               obj({'suspect_id': CODE, 'label': text(1, 120), 'whatif_ops': arr(OP_SCHEMA, 0, 3), 'reason': text(1, 600),
                    'setup': obj({'region_margin_mm': {'type': 'number', 'minimum': 1, 'maximum': 40}, 'max_timesteps': {'type': 'integer', 'minimum': 1000, 'maximum': 2000000},
                                  'end_criteria_db': {'type': 'number', 'minimum': -80, 'maximum': -20}, 'mesh': {'type': 'string', 'enum': ['normal', 'fine']}})},
                   ['suspect_id']),
               h.run_simulation, phases=P({4, 5}), levels=TEST, trail=lambda a: 'Running a simulation: ' + (a.get('label') or ('base case' if not a.get('whatif_ops') else 'what-if'))),
        define('get_sim_inputs', 'What a power, thermal or signal simulation needs: each input with its default, where the default comes from, and the value in use. Also the values the engineer\'s request states '
               'and the what-if ops the simulation accepts. Read it before the first run.', obj({'kind': SIM_KIND}, ['kind']), h.get_sim_inputs,
               phases=P({2, 3, 4}), levels=CHECK_UP, trail=lambda a: f'Reading the inputs of the {a.get("kind")} simulation'),
        define('set_sim_input', 'Record one input of a board simulation with its source. user: the value must appear in the engineer\'s request. datasheet: needs cite (part and page) and the value must be in a datasheet read in this review. '
               'default: go back to the simulator default. choice: which nets to look at (names). A value without a source is refused; an input nobody gave stays a labelled default.',
               obj({'kind': SIM_KIND, 'name': text(1, 100), 'value': NUM, 'names': arr(text(1, 80), 0, 20), 'source': {'type': 'string', 'enum': ['user', 'datasheet', 'default', 'choice']}, 'cite': text(1, 300)}, ['kind', 'name', 'source']), h.set_sim_input,
               phases=P({2, 3, 4}), levels=CHECK_UP, trail=lambda a: f'Setting {a.get("name")} of the {a.get("kind")} simulation ({a.get("source")})'),
        define('run_board_sim', 'Run a power, thermal or signal simulation for one suspect: the base (no whatif) or one what-if, which lives only in the simulation. Identical requests return the stored result free. '
               'quality confirm (base only) uses the finer mesh. inputs may only repeat values set with set_sim_input. Thermal: heat_from_power true adds the latest power run\'s copper loss until you pass false.',
               obj({'kind': SIM_KIND, 'suspect_id': CODE, 'label': text(1, 120), 'whatif': arr(SIM_OP, 0, 3), 'inputs': {'type': 'object', 'additionalProperties': NUM}, 'reason': text(1, 600),
                    'quality': {'type': 'string', 'enum': ['screen', 'confirm']}, 'heat_from_power': {'type': 'boolean'}}, ['kind']), h.run_board_sim,
               phases=P({4}), levels=TEST, trail=lambda a: f'Running the {a.get("kind")} simulation: ' + (a.get('label') or ('base case' if not a.get('whatif') else 'what-if'))),
        define('compare_sims', 'Numeric difference between two stored results of the same board simulation and quality (b minus a).', obj({'a': CODE, 'b': CODE}, ['a', 'b']), h.compare_sims,
               phases=P({4, 5}), levels=TEST, trail=lambda a: f'Comparing {a.get("a")} with {a.get("b")}'),
        define('lookup_part', 'Look up a part at DigiKey and Mouser by manufacturer part number (mpn), or by value and footprint. Gives the maker, description, parameters and datasheet link; never guesses a part. '
               'Only when datasheets are part of this review.', obj({'mpn': text(2, 80), 'value': text(1, 40), 'footprint': text(1, 80), 'maker': text(1, 60)}), h.lookup_part,
               phases=P({2, 3, 4}), levels=CHECK_UP, trail=lambda a: 'Looking up ' + (a.get('mpn') or a.get('value') or 'a part')),
        define('view_result', 'View a chart or field image of a stored result. kind: s11, smith, pattern, field or current.',
               obj({'ref': CODE, 'kind': {'type': 'string', 'enum': ['s11', 'smith', 'pattern', 'field', 'current']}}, ['ref', 'kind']), h.view_result,
               phases=P({4, 5}), levels=TEST, image=True, trail=lambda a: f'Viewing the {a.get("kind")} of {a.get("ref")}'),
        define('compare_results', 'Numeric difference between two stored results of the same quality (b minus a).', obj({'a': CODE, 'b': CODE}, ['a', 'b']), h.compare_results,
               phases=P({4, 5}), levels=TEST, trail=lambda a: f'Comparing {a.get("a")} with {a.get("b")}'),
        define('tune_matching', 'Matching parts for a stored, trusted run (R1, R2...: base, what-if, or both). On a screen run it is a zero-solver experiment for suspect_id (default: the run\'s suspect): '
               'cached, counted in the budget, and it can win. Searches E24 series and shunt parts, L swapped for C included, or tests the parts you list (L in nH, C in pF, none = empty).',
               obj({'ref': CODE, 'suspect_id': CODE, 'parts': arr(obj({'ref': text(1, 12), 'kind': {'type': 'string', 'enum': ['L', 'C', 'none']}, 'value': {'type': 'number', 'minimum': 0.05, 'maximum': 100}}, ['ref', 'kind']), 1, 4)},
                   ['ref']), h.tune_matching,
               phases=P({4, 5}), levels=CHECK_UP, trail=lambda a: f'Calculating matching values from {a.get("ref")}'),
        define('conclude_suspect', 'Conclude one suspect as confirmed, ruled_out or inconclusive, with reasoning that cites only tool numbers.',
               obj({'suspect_id': CODE, 'verdict': {'type': 'string', 'enum': ['confirmed', 'ruled_out', 'inconclusive']}, 'reasoning': text(10, 1200), 'evidence_ids': arr(text(1, 20), 0, 12)},
                   ['suspect_id', 'verdict', 'reasoning']), h.conclude_suspect, phases=P({4}), levels=TEST, closing=True,
               trail=lambda a: f'Concluding {a.get("suspect_id")}: {a.get("verdict")}'),
        define('close_investigation', 'Close the investigation with its outcome (improved, no_improvement, inconclusive) and the linked finding code. On Standard, not allowed as no_improvement or inconclusive '
               'while budget remains until a matching experiment and at least 2 suspects were tested; an improvement must close as improved.',
               obj({'investigation_id': CODE, 'outcome': {'type': 'string', 'enum': ['improved', 'no_improvement', 'inconclusive']}, 'summary': text(10, 1200), 'finding_code': text(1, 12)},
                   ['investigation_id', 'outcome', 'summary']), h.close_investigation, phases=P({4}), levels=TEST, closing=True,
               trail=lambda a: f'Closing investigation {a.get("investigation_id")}: {a.get("outcome")}'),
        define('confirm_result', 'Detailed (confirm quality) run of the base, then of the winner: what-if operations plus matching parts. A matching-only winner reuses the base run. '
               'which=refine confirms the best candidate of the open refine round. '
               'The parts are then locked and re-tuned at confirm quality, and the report says honestly whether the screen improvement held.',
               obj({'investigation_id': CODE, 'which': {'type': 'string', 'enum': ['base', 'winner', 'refine']}}, ['investigation_id', 'which']), h.confirm_result,
               phases=P({5}), levels=TEST, trail=lambda a: f'Confirming the {a.get("which")} with a detailed run'),
        define('submit_summary', 'Submit the plain-language summary and up to five top actions. Every number must match the evidence.',
               obj({'summary': text(20, 2400), 'top_actions': arr(text(5, 400), 1, 5)}, ['summary', 'top_actions']), h.submit_summary,
               phases=P({6}), closing=True, trail=lambda a: 'Writing the summary'),
    ]
