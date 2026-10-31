"""The USB regulator sample has no radio: its review must skip Investigate and Confirm and show no solver limit."""
from pathlib import Path

from trustpcb.analyzers.base import run_all
from trustpcb.pcb.model import parse_board
from trustpcb.rf.setup import plan_setup

from gate_steps import dismiss

ROOT = Path(__file__).resolve().parents[3]
SAMPLE = ROOT / 'samples' / 'usb-ldo-led' / 'usb_ldo_led_node.kicad_pcb'
VIEWS = [{'tool': 'view_board', 'args': {'around': 'ref:U1', 'margin_mm': 6}}, {'tool': 'view_board', 'args': {'around': 'ref:J1', 'margin_mm': 5}}]


async def test_scripted_review_of_the_no_radio_sample_skips_investigate(env):
    board = parse_board(SAMPLE, 'v-usb')
    setup = plan_setup(board)
    assert setup.simulatable is False and setup.code == 'not_wireless'
    env.lab.board = board
    env.lab.candidates = [c for c in run_all(board)['candidates'] if c['rule'] in ('decoupling', 'netclass_width')]
    assert {c['rule'] for c in env.lab.candidates} == {'decoupling', 'netclass_width'}
    env.lab.rf = {'simulatable': False, 'reason': setup.reason, 'code': setup.code, 'kind': 'none'}
    script = [
        {'tool': 'submit_board_brief', 'args': {'purpose': 'USB-C powered 3.3 V regulator with a status LED', 'part_roles': {'U1': 'regulator', 'J1': 'connector'}, 'risks': [], 'priorities': ['power']}},
        {'tool': 'advance_phase'},
        *dismiss('decoupling', 'netclass_width'), *VIEWS,
        {'tool': 'advance_phase'},
        {'tool': 'submit_summary', 'args': {'summary': 'A small USB powered regulator board with no radio. Review covered power layout only.', 'top_actions': ['Move the input capacitor closer to U1.']}},
    ]
    review = await env.review(script)
    chat = review['chat_id']
    assert review['end_state'] == 'complete', review['summary']['end']
    phases = [e['payload']['phase'] for e in env.events(chat, 'phase_changed')]
    assert phases == [0, 1, 2, 3, 6]                                    # Investigate (4) and Confirm (5) never run
    skipped = [s['phase'] for c in review['summary']['phases'] for s in c['skipped']]
    assert {4, 5} <= set(skipped)
    assert env.lab.count('run_scenario') == 0
    snapshot = env.store.latest_report_revision(review['id'])['snapshot']
    assert snapshot['rf']['status'] == 'not_tested'
    budget = env.events(chat, 'budget')[-1]['payload']
    assert budget['solver_s']['limit'] == 0 and budget['investigations']['limit'] == 0
    assert 'tuning_target_s11_db' not in {a['name'] for a in snapshot['assumptions']}       # no S11 target is invented for a board with no radio
    assert snapshot['method']['note'] == '' and snapshot['method']['scenarios'] == []         # and no solver mesh talk in the method
    from trustpcb.review.report import render_html
    html = render_html(snapshot)
    assert 'mesh' not in html.lower() and 'S11' not in html
