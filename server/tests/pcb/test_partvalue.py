"""The shared part-value reader: standard EE notation, kind-dependent letters, deliberate None."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from trustpcb.analyzers.classify import parse_value as analyzer_parse
from trustpcb.partvalue import parse_value
from trustpcb.rf.setup import parse_value as rf_parse

ROOT = Path(__file__).resolve().parents[3]

# (text, kind, expected base-unit value or None)
CASES = [
    # R K M G in the decimal place
    ("2R2", "R", 2.2), ("4K7", "R", 4700.0), ("4k7", "R", 4700.0), ("1M5", "R", 1.5e6), ("R22", "R", 0.22),
    ("0R", "R", 0.0), ("22R", "R", 22.0), ("1G2", "R", 1.2e9), ("R010", "R", 0.01), ("10R0", "R", 10.0),
    # M is mega and m is milli, per kind
    ("1M", "R", 1e6), ("1m", "R", 1e-3), ("1Meg", "R", 1e6), ("1meg", "R", 1e6), ("4.7M", "R", 4.7e6), ("10mR", "R", 0.01),
    ("1M", "C", None), ("1M", "L", None), ("1m", "C", 1e-3), ("1m", "L", 1e-3),
    # resistor suffixes and unit words
    ("10k", "R", 1e4), ("10K", "R", 1e4), ("100ohm", "R", 100.0), ("100 Ohm", "R", 100.0), ("4.7kOhm", "R", 4.7e3),
    ("2.2MOhm", "R", 2.2e6), ("5mOhm", "R", 5e-3), ("10kΩ", "R", 1e4), ("22Ω", "R", 22.0), ("4,7k", "R", 4700.0),
    # p n u m for capacitors and inductors, the micro sign, unit suffixes
    ("100n", "C", 100e-9), ("100nF", "C", 100e-9), ("4n7", "C", 4.7e-9), ("4p7", "C", 4.7e-12), ("1u5", "C", 1.5e-6),
    ("4µ7", "C", 4.7e-6), ("4.7µF", "C", 4.7e-6), ("4.7μF", "C", 4.7e-6), ("12pF", "C", 12e-12), ("0.3pF", "C", 0.3e-12),
    ("2.2nH", "L", 2.2e-9), ("10nH", "L", 10e-9), ("10n", "L", 10e-9), ("2n2", "L", 2.2e-9), ("1uH", "L", 1e-6),
    ("1u", "L", 1e-6), ("100 nF", "C", 100e-9), ("1F", "C", 1.0), ("1H", "L", 1.0),
    # a letter that does not belong to the kind
    ("10k", "C", None), ("10k", "L", None), ("4p7", "R", None), ("100nF", "R", None), ("100nF", "L", None), ("2.2nH", "C", None),
    ("22R", "C", None), ("2R2", "L", None), ("10kOhm", "C", None),
    # tolerance, voltage, power and frequency text after the value
    ("100n 16V", "C", 100e-9), ("100nF/16V X7R", "C", 100e-9), ("16V 100nF", "C", 100e-9), ("10k 1%", "R", 1e4),
    ("10k 1/16W", "R", 1e4), ("4.7uF 6.3V X5R", "C", 4.7e-6), ("22pF NP0", "C", 22e-12), ("22pF C0G 0402", "C", 22e-12),
    ("10k ±5%", "R", 1e4), ("0.1uF,10%", "C", 0.1e-6), ("10k 100ppm", "R", 1e4), ("100nF 0402", "C", 100e-9),
    # DNP words
    ("DNP 0.5pF", "C", 0.5e-12), ("0.5pF DNP", "C", 0.5e-12), ("DNP_0.5pF", "C", 0.5e-12), ("DNP 10k", "R", 1e4), ("NP0 100n", "C", 100e-9),
    # a bare number: ohms on a resistor, unknown on a capacitor or inductor, unknown without a kind
    ("100", "R", 100.0), ("4.7", "R", 4.7), ("0", "R", 0.0), ("100 1%", "R", 100.0), ("DNP 100", "R", 100.0),
    ("100", "C", None), ("100", "L", None), ("0.5", "C", None), ("100", None, None), ("10", "L", None), ("DNP 100", "C", None),
    # no kind: the unit in the text decides
    ("100nF", None, 100e-9), ("2.2nH", None, 2.2e-9), ("4K7", None, 4700.0), ("1M", None, 1e6), ("2R2", None, 2.2), ("4n7", None, 4.7e-9),
    ("10kOhm", None, 1e4), ("10k", None, 1e4), ("10kF", None, None), ("1MF", None, None),
    # not a value
    ("", "R", None), (None, "R", None), ("   ", "C", None), ("DNP", "C", None), ("DNP_SERIES", "C", None), ("GND", "R", None),
    ("3V3", "C", None), ("USBLC6-2SC6", "C", None), ("AP2112K-3.3", "R", None), ("M3 NPTH", "R", None), ("0.5A PTC", "R", None),
    ("32MHz", "C", None), ("1%", "R", None), ("abc", "L", None), ("--", "C", None), ("1.2.3", "R", None), ("LED", "C", None),
    ("Crystal_GND24_Small", "C", None), ("USB-C 5V IN", "C", None), ("nRF52840-QFAA", "R", None), ("10k5k", "R", None),
]


@pytest.mark.parametrize("text,kind,expected", CASES)
def test_parse_value_table(text, kind, expected):
    got = parse_value(text, kind)
    if expected is None:
        assert got is None
    else:
        assert got == pytest.approx(expected, rel=1e-12, abs=0)


@pytest.mark.parametrize("kind", ["R", "L", "C", None, "x", "?"])
@pytest.mark.parametrize("text", ["", "\u0000", "1" * 400, "9" * 400 + "k", "µ" * 50, "R" * 100, "1e999", "😀", "10k" * 50, None, 5, b"10k"])
def test_unparseable_never_raises(text, kind):
    parse_value(text, kind)


def test_the_reported_disagreements_are_settled():
    assert parse_value("2R2", "R") == pytest.approx(2.2)
    assert parse_value("1M", "R") == 1e6 and parse_value("1m", "R") == 1e-3
    assert parse_value("100", "C") is None


def test_both_callers_use_the_shared_reader():
    for text, kind in [("2R2", "R"), ("1M", "R"), ("100", "C"), ("100n 16V", "C"), ("2.2nH", "L"), ("DNP 0.5pF", "C"), ("4K7", "R")]:
        shared = parse_value(text, kind)
        assert analyzer_parse(text, kind) == shared
        assert rf_parse(kind, text)[0] == shared
    assert analyzer_parse("4n7") == pytest.approx(4.7e-9)


def test_rf_parse_value_keeps_the_dnp_flag():
    assert rf_parse("C", "DNP 0.5pF") == (pytest.approx(0.5e-12), True)
    assert rf_parse("C", "DNP_SERIES") == (None, True)
    assert rf_parse("R", "2R2") == (pytest.approx(2.2), False)
    assert rf_parse("R", "1M") == (1e6, False)
    assert rf_parse("C", "100") == (None, False)


# ------------------------------------------------------------------ the sample boards' real values

SAMPLE_VALUES = {
    # value text: (kind of the part that carries it, expected value or None)
    "100nF": ("C", 1e-7), "4.7uF": ("C", 4.7e-6), "2.2uF": ("C", 2.2e-6), "1uF": ("C", 1e-6), "10uF": ("C", 1e-5),
    "12pF": ("C", 12e-12), "DNP 0.5pF": ("C", 0.5e-12), "0.3pF": ("C", 0.3e-12), "0.36pF": ("C", 0.36e-12), "0.51pF": ("C", 0.51e-12),
    "2.2nH": ("L", 2.2e-9), "10k": ("R", 1e4), "4.7k": ("R", 4.7e3), "5.1k": ("R", 5.1e3), "22R": ("R", 22.0), "330R": ("R", 330.0),
    "DNP_SERIES": ("C", None), "DNP_SHUNT": ("C", None), "DNP": ("C", None),
    # parts that are not passives never read as one
    "0.5A PTC": ("R", None), "AP2112K-3.3": ("C", None), "LM75C": ("C", None), "USBLC6-2SC6": ("C", None), "nRF52840-QFAA": ("L", None),
    "M3 NPTH": ("R", None), "GLOBAL FIDUCIAL": ("R", None), "SWD 10P 1.27mm": ("R", None), "SMA_EDGE_2P5G": ("C", None),
    "USER BTN": ("R", None), "STATUS": ("R", None), "USB-C 5V IN": ("R", None), "3V3": ("C", None), "GND": ("R", None),
    "32MHz 9pF": ("R", None), "VBUS_F": ("C", None),
}


@pytest.mark.parametrize("text,case", sorted(SAMPLE_VALUES.items()))
def test_sample_board_values(text, case):
    kind, expected = case
    got = parse_value(text, kind)
    assert (got is None) if expected is None else got == pytest.approx(expected, rel=1e-12)
    assert rf_parse(kind, text)[0] == got


def test_sample_values_are_all_covered():
    seen = set()
    for pcb in (ROOT / "samples").glob("*/*.kicad_pcb"):
        seen |= set(re.findall(r'\(property "Value" "([^"]*)"', pcb.read_text()))
    assert seen - set(SAMPLE_VALUES) == set()


def test_sample_passive_values_use_the_float_arithmetic_of_before():
    # number * scale in the same order as the readers that were replaced, so cached numbers do not move
    assert parse_value("2.2nH", "L") == float("2.2") * 1e-9
    assert parse_value("100nF", "C") == float("100") * 1e-9
    assert parse_value("4.7uF", "C") == float("4.7") * 1e-6
    assert parse_value("10uF", "C") == float("10") * 1e-6
    assert parse_value("12pF", "C") == float("12") * 1e-12
    assert parse_value("4n7", "C") == float("4.7") * 1e-9
