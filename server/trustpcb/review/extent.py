"""The extent of a review: which checks the engineer chose at the start.

`layout` (the rule and layout review) is always on. The others add physics or data:

  antenna     the RF simulation of a printed antenna (only when the board can be simulated)
  power       DC power delivery: voltage drop, current density, via current
  thermal     steady-state temperature rise from copper and part power
  signal      impedance, delay and coupling of the fast nets
  datasheets  part lookup and datasheet reading for the inputs (only when supplier keys are set)

Extent is separate from depth. Depth (quick, standard, deep) says how long and how hard the agent works; extent says what it is allowed to check.
A review made before extents existed stores no extent and runs as it did: layout, plus the antenna when the board allowed it.
"""
from __future__ import annotations

from typing import Any

LAYOUT, ANTENNA, POWER, THERMAL, SIGNAL, DATASHEETS = 'layout', 'antenna', 'power', 'thermal', 'signal', 'datasheets'
EXTENTS = (LAYOUT, ANTENNA, POWER, THERMAL, SIGNAL, DATASHEETS)
SIM_KINDS = (POWER, THERMAL, SIGNAL)
INVESTIGATE_KINDS = (ANTENNA, *SIM_KINDS)

LABELS = {
    LAYOUT: 'Rules and layout', ANTENNA: 'Antenna simulation', POWER: 'Power delivery', THERMAL: 'Thermal', SIGNAL: 'Signal integrity', DATASHEETS: 'Datasheets and part lookup',
}
DETAILS = {
    LAYOUT: 'KiCad checks, analyzers and a close look at placement and routing. Always on.',
    ANTENNA: 'Full-wave simulation of a printed antenna with matching tests. Takes the longest.',
    POWER: 'DC voltage drop, current density and via current from the copper. Takes seconds.',
    THERMAL: 'Steady-state temperature rise from copper and part power. Takes seconds.',
    SIGNAL: 'Impedance, delay and coupling of the fast nets. Takes seconds.',
    DATASHEETS: 'Look up parts and read datasheets for the inputs the simulations need.',
}
#: What a review gets when the caller does not choose: the rule review, the antenna when it can be simulated (as before), and power delivery,
#: which is the cheapest physics that means something on every board. Thermal and signal assumptions are larger, and datasheets need keys and
#: the network, so those are opted into.
DEFAULT_ON = (LAYOUT, ANTENNA, POWER)


class ExtentError(ValueError):
    """The extent asked for is not valid for this board or this machine."""


def options(*, rf_simulatable: bool, rf_reason: str = '', sims: dict[str, dict] | None = None, datasheets_ready: bool = False, datasheets_reason: str = '') -> list[dict]:
    """What the start dialog may offer: one entry per extent with `available`, the `reason` when it is not, and whether it is chosen by default."""
    sims = sims or {}
    out = []
    for name in EXTENTS:
        if name == LAYOUT:
            available, reason = True, ''
        elif name == ANTENNA:
            available, reason = bool(rf_simulatable), '' if rf_simulatable else (rf_reason or 'No antenna on this board can be simulated.')
        elif name == DATASHEETS:
            available, reason = bool(datasheets_ready), '' if datasheets_ready else (datasheets_reason or 'No DigiKey or Mouser key is set. Add one in settings to look up parts and datasheets.')
        else:
            info = sims.get(name) or {}
            available, reason = bool(info.get('available')), '' if info.get('available') else (info.get('reason') or f'The {LABELS[name].lower()} simulation is not available.')
        out.append({'id': name, 'label': LABELS[name], 'detail': DETAILS[name], 'available': available, 'reason': reason, 'always': name == LAYOUT,
                    'default': name in DEFAULT_ON and available})
    return out


def default_extent(opts: list[dict]) -> list[str]:
    return [o['id'] for o in opts if o['default']]


def normalize(raw: Any, opts: list[dict]) -> list[str]:
    """The validated extent in canonical order. `layout` is added. Raises ExtentError for an unknown name, a bad shape, or a name that is not available."""
    if raw is None:
        return default_extent(opts)
    if not isinstance(raw, (list, tuple)) or any(not isinstance(item, str) for item in raw):
        raise ExtentError('extent must be a list of names, for example ["layout", "power"].')
    unknown = [item for item in raw if item not in EXTENTS]
    if unknown:
        raise ExtentError(f'Unknown extent {", ".join(repr(u) for u in unknown[:4])}. Choose from {", ".join(EXTENTS)}.')
    chosen = set(raw) | {LAYOUT}
    by_id = {o['id']: o for o in opts}
    for name in EXTENTS:
        if name in chosen and not by_id.get(name, {}).get('available', name == LAYOUT):
            raise ExtentError(f'{LABELS[name]} cannot be chosen: {by_id.get(name, {}).get("reason") or "it is not available"}')
    return [name for name in EXTENTS if name in chosen]


def legacy_extent(rf_simulatable: bool) -> list[str]:
    """What a review without a stored extent did."""
    return [LAYOUT, ANTENNA] if rf_simulatable else [LAYOUT]


def sim_kinds_of(extent: list[str] | None) -> list[str]:
    return [name for name in SIM_KINDS if name in (extent or [])]


def label_list(extent: list[str] | None) -> str:
    return ', '.join(LABELS.get(name, name).lower() for name in extent or [])


def method_line(extent: list[str] | None, *, stored: bool = True, skipped: dict[str, str] | None = None) -> str:
    """One plain sentence for the report's method section."""
    if not stored or extent is None:
        text = 'Extent: not recorded (this review was made before the extent choice). It covered the rules and layout review and, where the board allowed it, the antenna simulation.'
    else:
        text = f'Extent chosen at the start: {label_list(extent)}.'
        if len(extent) == 1:
            text += ' No simulation was chosen, so every statement about electrical behaviour is a layout measurement or engineering opinion.'
    for name, reason in (skipped or {}).items():
        text += f' {LABELS.get(name, name)} was chosen but could not run: {reason}'
    return text


# ---- asking the Lab ---------------------------------------------------------------------------------------------

async def sim_status(lab, version_id: str, goal: dict | None) -> dict[str, dict]:
    """What each installed board simulation says about this board: {kind: {available, reason, label, whatif_ops}}. {} for a Lab without them."""
    function = getattr(lab, 'sim_status', None)
    if function is None:
        return {}
    try:
        return dict(await function(version_id=version_id, goal=goal) or {})
    except Exception:
        import logging
        logging.getLogger('trustpcb').warning('Version %s: the board simulations could not be checked, so none is offered', version_id, exc_info=True)
        return {}


def split_chosen(extent: list[str] | None, status: dict[str, dict]) -> dict:
    """The chosen board simulations that can run now, and those that cannot with the reason."""
    available, unavailable = {}, {}
    for kind in sim_kinds_of(extent):
        info = status.get(kind) or {}
        if info.get('available'):
            available[kind] = info
        else:
            unavailable[kind] = info.get('reason') or 'It is not installed in this build.'
    return {'available': available, 'unavailable': unavailable}


def parts_ready() -> tuple[bool, str]:
    """Are supplier keys set? (datasheets and part lookup are offered only then)"""
    try:
        from .. import parts
        ready = bool(parts.available())
    except Exception:
        return False, 'Part lookup is not installed in this build.'
    return ready, '' if ready else 'No DigiKey or Mouser key is set. Add one in settings to look up parts and datasheets.'


async def options_for(lab, version_id: str, goal: dict | None) -> list[dict]:
    """The start dialog's choices for one version, from the board itself."""
    try:
        rf = await lab.rf_status(version_id=version_id, goal=goal or {})
    except Exception as exc:
        rf = {'simulatable': False, 'reason': f'The RF check failed: {str(exc)[:160]}'}
    ready, reason = parts_ready()
    return options(rf_simulatable=bool(rf.get('simulatable')), rf_reason=rf.get('reason') or '', sims=await sim_status(lab, version_id, goal),
                   datasheets_ready=ready, datasheets_reason=reason)
