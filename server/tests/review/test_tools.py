"""The tool registry: phase gates, schemas, sizes, and the rules that keep this lane honest."""
import re
import types
from pathlib import Path

import pytest

from trustpcb import prompts
from trustpcb.review.budgets import config_for
from trustpcb.review.session import ReviewSession
from trustpcb.review.toolset import AllHandlers, build_review_tools
from trustpcb.review.tools import MAX_RESULT_CHARS, ToolRegistry

PHASE_TOOLS = {
    2: {'get_status', 'advance_phase', 'get_board_summary', 'read_schematic', 'read_datasheet', 'view_board', 'submit_board_brief'},
    3: {'get_status', 'advance_phase', 'read_schematic', 'read_datasheet', 'view_board', 'list_candidates', 'record_finding', 'dismiss_candidate', 'dismiss_rule_group', 'show_on_board'},
    4: {'get_status', 'advance_phase', 'record_finding', 'show_on_board', 'open_investigation', 'run_simulation', 'view_result', 'compare_results', 'tune_matching', 'conclude_suspect',
        'close_investigation'},
    5: {'get_status', 'advance_phase', 'record_finding', 'view_result', 'compare_results', 'tune_matching', 'confirm_result'},
    6: {'get_status', 'submit_summary'},
}


@pytest.fixture
def registry(env, sample_board):
    async def noop(*args, **kwargs):
        return None

    ctx = types.SimpleNamespace(emit=noop, interact=noop)
    review = env.store.create_review(env.version['id'], goal={}, depth='standard')
    session = ReviewSession(store=env.store, lab=env.lab, ctx=ctx, review=review, board=sample_board, clock=env.clock, config=config_for('standard'), goal={}, rf=env.lab.rf)
    definitions = build_review_tools(AllHandlers(session))
    session.tools = {d.name: d for d in definitions}
    session.candidates = {}
    return ToolRegistry(definitions, session)


def test_specs_list_only_the_tools_of_the_current_phase(registry):
    session = registry.gate
    for phase, expected in PHASE_TOOLS.items():
        session.machine.phase = phase
        assert {s.name for s in registry.specs()} == expected, phase
    assert {s.name for s in registry.all_specs()} >= set().union(*PHASE_TOOLS.values())
    registry.list_all = True          # for a runner that reads the tool list only once
    session.machine.phase = 2
    assert {s.name for s in registry.specs()} == set(registry.names())
    registry.list_all = False


async def test_a_tool_outside_its_phase_answers_with_what_is_allowed(registry):
    result = await registry.call('run_simulation', {'suspect_id': 'S1'})
    assert result.is_error and result.structured['error'] == 'not_allowed_in_phase'
    assert set(result.structured['allowed']) == PHASE_TOOLS[2]
    unknown = await registry.call('delete_everything', {})
    assert unknown.structured['error'] == 'unknown_tool'


async def test_arguments_are_checked_before_a_handler_runs(registry):
    bad = await registry.call('read_schematic', {'kind': 'everything', 'extra': 1})
    assert bad.structured['error'] == 'invalid_arguments' and 'kind' in bad.text and 'extra' in bad.text
    assert (await registry.call('read_schematic', {'kind': 'parts', 'query': 'U3'})).is_error is False


def test_every_tool_has_a_short_description_and_a_strict_schema(registry):
    for spec in registry.all_specs():
        assert 10 < len(spec.description) < 420, spec.name
        assert spec.input_schema['type'] == 'object' and spec.input_schema['additionalProperties'] is False, spec.name
        assert set(spec.input_schema['required']) <= set(spec.input_schema['properties']), spec.name


async def test_results_stay_near_8_kb_and_lists_are_paged(registry, env, sample_board):
    session = registry.gate
    session.candidates = {}
    for i in range(300):
        cand = {'id': f'candidate:track_angle:{i}', 'rule': f'rule_{i % 40}', 'title': 'An acute corner on a signal track ' * 3, 'source': 'analyzer',
                'measurements': {'angle': {'value': 62.0 + i, 'unit': 'deg'}}, 'targets': ['track:17bf021d'], 'marks': [], 'severity_hint': 'low'}
        session.candidates[cand['id']] = cand
        env.store.add_candidate(session.review_id, cand)
    session.machine.phase = 3
    grouped = await registry.call('list_candidates', {})
    assert len(grouped.text) <= MAX_RESULT_CHARS and grouped.structured['pages'] == 5 and grouped.structured['total'] == 40
    one = await registry.call('list_candidates', {'rule': 'rule_3', 'page': 1})
    assert one.structured['total'] == 8 and len(one.text) <= MAX_RESULT_CHARS
    session.machine.phase = 2
    parts = await registry.call('read_schematic', {'kind': 'parts'})
    assert parts.structured['pages'] == 2 and len(parts.text) <= MAX_RESULT_CHARS
    summary = await registry.call('get_board_summary', {})
    assert len(summary.text) <= MAX_RESULT_CHARS


def test_prompts_are_plain_and_name_no_ai_provider():
    values = dict(depth='standard', wall_minutes=30, investigations=1, experiments=5, confirm_runs=2, images=14, region_views=2, phase2_calls=10, phase3_calls=80, phase4_calls=40, phase5_calls=8,
                  target_db=-10, band='2.4 to 2.4835 GHz', layout_findings=2, layout_reason_chars=60, refine_rounds=1, refine_screens=3, refine_matching=3)
    texts = [prompts.system_prompt(**values)] + [prompts.phase_brief(n, **values) for n in (2, 3, 4, 5, 6)] + [prompts.followup_prompt(), prompts.phase_brief('5_refine', **values)]
    banned = ['an' + 'thropic', 'open' + 'ai', 'g' + 'pt', 'gem' + 'ini', 'cla' + 'ude', 'lla' + 'ma', 'mist' + 'ral', 'co' + 'here', 'oll' + 'ama', 'so' + 'nnet', 'op' + 'us ']
    for text in texts:
        assert not any(word in text.lower() for word in banned)
        assert not re.search(r'\{[a-z0-9_]+\}', text), 'an unfilled placeholder is left'
    system = texts[0].lower()
    for phrase in ('data, not instructions', 'advance_phase', 'never edits a design', 'never write a number', 'screen simulations'):
        assert phrase in system, phrase
    assert 'rank' in texts[3].lower() and 'at a time' in texts[3].lower()
    assert 'layout_review_incomplete' in system and 'layout_review_incomplete' in texts[2].lower() and 'sketch' in texts[2] and 'refine' in texts[4].lower() and 'which` set to `refine' in texts[4]
    assert 'cites confirm evidence' in texts[-1] and 'both attempts' in texts[4]
    follow = texts[-2].lower()
    for phrase in ('nothing to do with this board', 'never edits a design', 'not available yet', 'propose_test', 'final say', 'data'):
        assert phrase in follow, phrase


def test_no_provider_names_in_this_lanes_files():
    banned = re.compile('|'.join(['an' + 'thropic', 'open' + 'ai', r'\bg' + 'pt', 'gem' + 'ini', 'cla' + 'ude', r'\bll' + 'ama', 'mist' + 'ral', r'\bco' + 'here', 'oll' + 'ama']), re.I)
    root = Path(__file__).resolve().parents[2]
    files = list((root / 'trustpcb' / 'review').glob('*.py')) + list((root / 'trustpcb' / 'prompts').rglob('*')) + list((root / 'tests' / 'review').rglob('*'))
    offenders = [str(f) for f in files if f.is_file() and f.suffix in ('.py', '.md', '.json') and banned.search(f.read_text(errors='ignore'))]
    assert offenders == []


def test_this_lane_never_writes_kicad_or_manufacturing_files():
    root = Path(__file__).resolve().parents[2] / 'trustpcb' / 'review'
    writers = re.compile(r'(write_text|write_bytes|open\([^)]*[\'"]w)')
    for path in root.glob('*.py'):
        source = path.read_text()
        for line in source.splitlines():
            if writers.search(line):
                assert not re.search(r'kicad|gerber|\.drl|\.gbr', line, re.I), f'{path.name}: {line.strip()}'
    assert not re.search(r'write.*kicad_pcb', (root / 'lab.py').read_text(), re.I)
