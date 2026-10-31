"""Fingerprints: a finding keeps its identity when coordinates shift.

A fingerprint is `rule|nets|zones-or-parts`, built from the targets a finding points at, so moving a
track a little between versions does not make it a different finding.
"""
from __future__ import annotations

import re
from typing import Any

_POINT = re.compile(r'^point:(-?[\d.]+),(-?[\d.]+)$')


class TargetIndex:
    """Maps target ids to the net and part they belong to, read from a Board (or anything shaped like one)."""

    def __init__(self, board: Any):
        self.net_of: dict[str, str] = {}
        self.ref_of: dict[str, str] = {}
        if board is None:
            return
        for track in getattr(board, 'tracks', []) or []:
            if track.net:
                self.net_of[track.id] = track.net
        for via in getattr(board, 'vias', []) or []:
            if via.net:
                self.net_of[via.id] = via.net
        for zone in getattr(board, 'zones', []) or []:
            if zone.net:
                self.net_of[zone.id] = zone.net
        for pad in getattr(board, 'pads', []) or []:
            self.ref_of[pad.id] = f'ref:{pad.ref}'
            if pad.net:
                self.net_of[pad.id] = pad.net


def parts_of(targets: list[str], index: TargetIndex) -> tuple[list[str], list[str], list[str]]:
    """(nets, zones, refs) the targets stand for."""
    nets: set[str] = set()
    zones: set[str] = set()
    refs: set[str] = set()
    for target in targets:
        if target.startswith('net:'):
            nets.add(target)
        elif target.startswith('zone_role:'):
            zones.add(target)
        elif target.startswith('ref:'):
            refs.add(target)
        elif target.startswith('pad:'):
            ref = index.ref_of.get(target) or 'ref:' + target.split(':')[1]
            refs.add(ref)
            if target in index.net_of:
                nets.add('net:' + index.net_of[target])
        elif target in index.net_of:
            nets.add('net:' + index.net_of[target])
        elif target.startswith(('zone:', 'keepout:')):
            zones.add(target)
        else:
            match = _POINT.match(target)
            if match:   # no object behind it: a coarse 2 mm cell keeps nearby results together
                refs.add(f'cell:{round(float(match.group(1)) / 2) * 2},{round(float(match.group(2)) / 2) * 2}')
    return sorted(nets), sorted(zones), sorted(refs)


def fingerprint(rule: str, targets: list[str], index: TargetIndex) -> str:
    nets, zones, refs = parts_of(targets, index)
    return f'{rule}|{",".join(nets)}|{",".join(zones + refs)}'


def rule_of(fp: str) -> str:
    return fp.split('|', 1)[0]
