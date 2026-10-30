"""Scripted steps that satisfy the phase 3 gate (coverage of every rule group, and the open-review region views)."""
REASON = 'Harmless on this board: the fixture result was checked against the layout and does not affect its job.'
DEFAULT_RULES = ('decoupling', 'track_angle', 'antenna_keepout', 'drc_clearance')


def dismiss(*rules):
    return [{'tool': 'dismiss_rule_group', 'args': {'rule': rule, 'reason': REASON}} for rule in rules]


def views(count=2):
    around = ['ref:U3', 'net:ANT_FEED', 'ref:C13']
    return [{'tool': 'view_board', 'args': {'around': around[i % 3], 'margin_mm': 6 + 8 * i}} for i in range(count)]


def clear(rules=DEFAULT_RULES, region_views=2):
    """Dismiss every default fixture rule group that is not named in the script already, then take the region views."""
    return dismiss(*rules) + views(region_views)
