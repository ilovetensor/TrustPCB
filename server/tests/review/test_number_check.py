import pytest

from trustpcb.review.number_check import allowed_from_text, allowed_from_values, check_text, extract, make_quantity


def q(value, unit, **extra):
    return make_quantity(value, unit, **extra)


@pytest.mark.parametrize('text,expected', [
    ('The trace is 5.2 mm long', [('mm', 5.2)]),
    ('Resonates at 2.4 GHz and 2400 MHz', [('GHz', 2.4), ('MHz', 2400.0)]),
    ('S11 of -10.3 dB and −12 dB', [('dB', -10.3), ('dB', -12.0)]),
    ('A 50 ohm line, 50 Ω, 4.7 kΩ', [('ohm', 50.0), ('Ω', 50.0), ('kΩ', 4.7)]),
    ('Efficiency is 62%', [('%', 62.0)]),
    ('10 mil tracks and 100 nF caps and 1.8pF', [('mil', 10.0), ('nF', 100.0), ('pF', 1.8)]),
    ('The band is 2.4 to 2.4835 GHz', [('GHz', 2.4), ('GHz', 2.4835)]),
    ('The board is 50 x 60 mm', [('mm', 50.0), ('mm', 60.0)]),
    ('Gain 2.1 dBi, 1,200 MHz', [('dBi', 2.1), ('MHz', 1200.0)]),
])
def test_extracts_numbers_with_units(text, expected):
    assert [(x.unit, x.value) for x in extract(text)] == expected


@pytest.mark.parametrize('text', [
    'There are 3 vias and 2 layers', 'Part C12 near U1 on net ANT_FEED', 'Rule track_detour:4 and F-007', 'The nRF52840 uses 0402 parts', 'Three bends within 5 widths',
    'S11 at the pin 1 mildly', 'Version 2 of the 3V3 rail',
])
def test_counts_and_identifiers_are_exempt(text):
    assert extract(text) == []


def test_unit_conversion_and_rounding():
    allowed = [q(2.4, 'GHz'), q(0.254, 'mm'), q(-10.34, 'dB')]
    assert check_text('It resonates at 2400 MHz', allowed) == []
    assert check_text('The track is 10 mil wide', allowed) == []
    assert check_text('The return loss is -10.3 dB, about 10 dB', allowed) == []        # an unsigned loss matches a negative S11
    assert check_text('It resonates at 2.4 GHz', [q(2.3987, 'GHz')]) == []             # rounding
    bad = check_text('It resonates at 2.3 GHz', [q(2.3987, 'GHz')])
    assert [m.raw for m in bad] == ['2.3 GHz'] and bad[0].reason == 'not_in_evidence'
    assert check_text('Value 2.370 GHz', [q(2.37, 'GHz')]) == []


def test_every_bad_number_is_found_and_the_message_names_it():
    problems = check_text('C13 is 7.5 mm from U3 and S11 is -22 dB', [q(5.2, 'mm')])
    assert [p.raw for p in problems] == ['7.5 mm', '-22 dB']
    assert '"7.5 mm"' in problems[0].message()


def test_units_must_match_their_dimension():
    assert [p.raw for p in check_text('5.2 GHz', [q(5.2, 'mm')])] == ['5.2 GHz']
    assert [p.raw for p in check_text('2.1 dB', [q(2.1, 'dBi')])] == ['2.1 dB']


def test_restricted_values_report_why():
    allowed = [q(2447.0, 'MHz', note='needs_confirm_run'), q(-9.3, 'dB')]
    problems = check_text('Resonates at 2447 MHz and improves by 9.3 dB', allowed)
    assert [(p.raw, p.reason) for p in problems] == [('2447 MHz', 'needs_confirm_run')]
    assert 'confirm run' in problems[0].message()
    assert check_text('Resonates at 2447 MHz', allowed + [q(2447.0, 'MHz')]) == []        # an unrestricted source wins


def test_allowed_values_from_evidence_shapes():
    quantities = allowed_from_values({'distance': {'value': 5.2, 'unit': 'mm'}, 'nested': [{'a': {'value': -3, 'unit': 'dB'}}]})
    assert {(x.unit, x.value) for x in quantities} == {('mm', 5.2), ('dB', -3.0)}
    assert [x.unit for x in allowed_from_text('Clearance (min 0.2 mm; actual 0.15 mm)')] == ['mm', 'mm']
    assert [x.unit for x in allowed_from_values({'x': {'value': 0.5, 'unit': 'ratio'}})] == ['%']
