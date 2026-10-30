import time
from pathlib import Path

import pytest

from trustpcb.pcb import sexpr

SAMPLES = Path(__file__).resolve().parents[3] / "samples"
NRF_PCB = SAMPLES / "nrf52840-node" / "nrf52840_rf_iot_node.kicad_pcb"
PATCH_PCB = SAMPLES / "patch-2g5" / "patch_antenna.kicad_pcb"


def test_parse_nested_and_atoms():
    n = sexpr.parse('(a (b 1 2.5) "q w" (c (d "x")))')
    assert n[0] == "a"
    assert sexpr.find(n, "b") == ["b", "1", "2.5"]
    assert n[2] == "q w"
    assert sexpr.arg(sexpr.find(n, "c"), "d") == "x"
    assert sexpr.num(n, "b", index=2) == 2.5


def test_quotes_escapes_and_parens_in_strings():
    n = sexpr.parse(r'(t "a (b) \"c\" \\ d")')
    assert n[1] == 'a (b) "c" \\ d'


def test_flags_both_forms():
    n = sexpr.parse("(x (hide yes) (locked no) dnp)")
    assert sexpr.flag(n, "hide") and not sexpr.flag(n, "locked") and sexpr.flag(n, "dnp")


@pytest.mark.parametrize("text", ["", "(a (b)", "   "])
def test_unbalanced_or_empty(text):
    with pytest.raises(sexpr.SexprError):
        sexpr.parse(text)


@pytest.mark.parametrize("path", [NRF_PCB, PATCH_PCB])
def test_sample_boards_parse_fast(path):
    text = path.read_text()
    t = time.perf_counter()
    root = sexpr.parse(text)
    assert time.perf_counter() - t < 1.0
    assert root[0] == "kicad_pcb"
    assert sexpr.arg(root, "generator_version") == "10.0"
