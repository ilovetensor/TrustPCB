"""An RF net class width that is the designer's own 50 ohm intent.

The `netclass_width` analyzer compares each net with the track width of its project net class. On an RF net it also estimates what the class width
would give (`class_width_z0_estimate`) and the width that gives the target (`width_for_target`). When the class width itself lands within 10 percent of
the target, the class is a real controlled-impedance setting and a feed routed far narrower than it is an impedance problem. A fact-check found a review
dismissing exactly this as "a class setting artifact", and the width never reached the impedance finding.

The server therefore:

  * refuses to dismiss such a candidate (`refusal`) and sends the agent to the impedance finding;
  * writes the class width, its estimate and the width for the target into any finding that covers the candidate (`carry`), with the practical limit:
    a line that wide cannot reach a fine-pitch pad, so a taper or a different stackup is needed. It is conditional on the assumed stackup.

Pure functions over candidate dicts; stored candidates from older reviews (without `target_impedance`) are read with the default 50 ohm.
"""
from __future__ import annotations

from typing import Any

TOLERANCE_PCT = 10.0
DEFAULT_TARGET_OHM = 50.0
MARKER = 'The project net class'          # the start of the sentence `carry` adds; a text that has it is not given a second one


def _value(candidate: dict, name: str) -> float | None:
    item = (candidate.get('measurements') or {}).get(name)
    return float(item['value']) if isinstance(item, dict) and isinstance(item.get('value'), (int, float)) else None


def intent_facts(candidate: dict) -> dict[str, Any] | None:
    """The facts of a `netclass_width` candidate on an RF net whose class width is within 10 percent of the impedance target, else None."""
    if candidate.get('rule') != 'netclass_width':
        return None
    z0, class_w = _value(candidate, 'class_width_z0_estimate'), _value(candidate, 'class_track_width')
    if z0 is None or class_w is None:
        return None
    target = _value(candidate, 'target_impedance') or DEFAULT_TARGET_OHM
    if abs(z0 - target) / target * 100.0 > TOLERANCE_PCT:
        return None
    info = candidate.get('net_class') or {}
    return {'id': candidate.get('id'), 'net': candidate.get('net'), 'class': info.get('name') or 'RF', 'class_width': class_w, 'z0': z0, 'target': target,
            'need': _value(candidate, 'width_for_target'), 'narrowest': _value(candidate, 'narrowest_width'), 'widest_narrow': _value(candidate, 'widest_narrow_width'),
            'narrow_length': _value(candidate, 'narrow_length'),
            'dielectric_mm': (candidate.get('stackup_assumptions') or {}).get('dielectric_height_mm'),
            'er': (candidate.get('stackup_assumptions') or {}).get('epsilon_r')}


def real_intent(candidates: list[dict]) -> list[dict[str, Any]]:
    return [f for f in (intent_facts(c) for c in candidates) if f]


def refusal(facts: list[dict[str, Any]], impedance_code: str | None = None) -> str:
    """The message for a dismissal that includes candidates of this kind."""
    first = facts[0]
    nets = ', '.join(dict.fromkeys(str(f['net']) for f in facts if f.get('net')))
    where = f' on {nets}' if nets else ''
    need = f' and {first["need"]:g} mm would give {first["target"]:g} ohm' if first.get('need') else ''
    routed = (f' The feed is routed {first["narrowest"]:g} to {first["widest_narrow"]:g} mm wide' + (f' over {first["narrow_length"]:g} mm' if first.get('narrow_length') else '') + '.'
              if first.get('narrowest') is not None and first.get('widest_narrow') is not None and first['narrowest'] != first['widest_narrow']
              else (f' The feed is routed {first["narrowest"]:g} mm wide.' if first.get('narrowest') is not None else ''))
    steer = (f'Put these candidates into the impedance finding ({impedance_code}: record_finding with update and candidate_ids)'
             if impedance_code else 'Put these candidates into the impedance finding (record_finding with candidate_ids, or update the finding that covers the impedance group)')
    return (f'The {first["class"]} net class{where} asks {first["class_width"]:g} mm, which the analyzer estimates at {first["z0"]:g} ohm, within 10 percent of the {first["target"]:g} ohm target'
            f'{need} (an estimate on the assumed stackup).{routed} So the class is a real controlled-impedance setting, not a class setting artifact, and a feed far narrower than it is an impedance question. '
            f'{steer}; the server adds the class width and the width for the target to the finding text. Dismiss the other candidates one by one with dismiss_candidate.')


def carry_sentences(facts: list[dict[str, Any]]) -> tuple[str, str]:
    """(measurement sentence for `what`, practical sentence for `recommendation`) for the first fact; both conditional on the assumed stackup."""
    f = facts[0]
    nets = ', '.join(dict.fromkeys(str(x['net']) for x in facts if x.get('net')))
    stack = (f' (assumed stackup: er {f["er"]:g}' + (f', dielectric {f["dielectric_mm"]:g} mm' if f.get('dielectric_mm') else '') + ')') if f.get('er') else ' (on the assumed stackup)'
    what = (f'{MARKER} {f["class"]} asks {f["class_width"]:g} mm' + (f' for {nets}' if nets else '') + f', which the analyzer estimates at {f["z0"]:g} ohm{stack}'
            + (f'; {f["need"]:g} mm would give {f["target"]:g} ohm' if f.get('need') else '') + '. The class width is the designer\'s own controlled-impedance setting, and the routed feed is narrower than it.')
    wide = f'{f["need"]:g} mm' if f.get('need') else 'that width'
    how = (f'A line {wide} wide cannot connect straight to a fine-pitch pad, so either taper it close to the pad (a short neck-down, not the whole feed) or choose a stackup with a thinner dielectric, '
           'which makes the 50 ohm line narrower. Both depend on the assumed stackup, so confirm the real stackup with the fab before changing the width.')
    return what, how


def carry(args: dict, facts: list[dict[str, Any]], existing: dict | None = None) -> dict:
    """`args` with the class-width sentences added to `what` and `recommendation` unless the finding already carries them. Returns `args` itself when nothing is added."""
    if not facts:
        return args
    current = {k: str(args.get(k) if args.get(k) is not None else (existing or {}).get(k) or '') for k in ('what', 'recommendation')}
    if MARKER in current['what']:
        return args
    what, how = carry_sentences(facts)
    out = dict(args)
    out['what'] = (current['what'].rstrip() + ' ' + what).strip()
    if 'cannot connect straight to a fine-pitch pad' not in current['recommendation']:
        out['recommendation'] = (current['recommendation'].rstrip() + ' ' + how).strip()
    return out
