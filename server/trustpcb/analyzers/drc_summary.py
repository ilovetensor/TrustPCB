"""Distinct counts for KiCad DRC results.

KiCad reports one record per violation pair, and the same physical fault can be listed several times: the
same two items twice, the same spot against several nets, a via and a track at one position. Counting
records as objects overstates the faults ("21 shorts" are 21 records at 14 positions between 14 net pairs;
"19 vias" are 19 hole-clearance records on 11 vias and one plated pad).

``annotate(candidates)`` adds to each KiCad candidate (existing fields are not changed, no record is
dropped, so ids that stored reviews cite stay valid):

* ``distinct_key``: the items and measured value that identify the record;
* ``duplicate_of``: the id of the first record with the same key, when this record repeats it;
* ``position_key`` (where the first item sits), ``net_pair`` (the two nets, sorted, ``""`` for no net),
  ``via_targets`` / ``pad_targets`` / ``track_targets``;
* ``rule_totals``: the counts for its rule (see ``summarize``).

``summarize(candidates)`` returns, per DRC rule, ``records``, ``distinct_records`` (duplicates removed),
``duplicate_records``, ``distinct_positions``, ``distinct_net_pairs``, ``distinct_vias``, ``distinct_pads``,
``distinct_tracks`` and ``severity`` counts. Both are idempotent and pure.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_NETS_RE = re.compile(r"\(nets? (.*?) and (.*?)\)\s*$")
_ITEM_NET_RE = re.compile(r"\[([^\]]*)\]")


def is_kicad_record(c: dict[str, Any]) -> bool:
    return c.get("source") == "kicad" and str(c.get("rule", "")).startswith(("drc_", "erc_", "parity_"))


def _targets_by_kind(c: dict[str, Any]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {"via": [], "pad": [], "track": [], "other": []}
    for t in c.get("targets") or []:
        kind = t.split(":", 1)[0]
        out[kind if kind in out else "other"].append(t)
    return out


def _net_pair(c: dict[str, Any]) -> list[str] | None:
    m = _NETS_RE.search(str(c.get("title", "")))
    if m:
        return sorted([m.group(1).strip(), m.group(2).strip()])
    nets = []
    for d in c.get("details") or []:
        n = _ITEM_NET_RE.search(str(d))
        if n:
            nets.append(n.group(1).strip())
    nets = sorted(set(nets))
    return nets if len(nets) == 2 else None


def _key(c: dict[str, Any]) -> str:
    actual = (c.get("measurements") or {}).get("actual", {}).get("value")
    items = sorted(c.get("targets") or []) or sorted(str(d) for d in c.get("details") or [])
    return json.dumps([c.get("rule"), items, actual], sort_keys=True)


def summarize(candidates: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    by_rule: dict[str, dict[str, Any]] = {}
    seen: dict[str, set[str]] = {}
    for c in candidates:
        if not is_kicad_record(c):
            continue
        rule = c["rule"]
        r = by_rule.setdefault(
            rule,
            {"rule": rule, "records": 0, "distinct_records": 0, "duplicate_records": 0, "severity": {}, "_pos": set(), "_pairs": set(), "_vias": set(), "_pads": set(), "_tracks": set()},
        )
        s = seen.setdefault(rule, set())
        r["records"] += 1
        key = _key(c)
        if key in s:
            r["duplicate_records"] += 1
            continue  # a repeated record adds nothing to any distinct count
        s.add(key)
        r["distinct_records"] += 1
        sev = c.get("kicad_severity") or c.get("severity_hint") or "?"
        r["severity"][sev] = r["severity"].get(sev, 0) + 1
        if c.get("position"):
            r["_pos"].add((round(c["position"][0], 3), round(c["position"][1], 3)))
        pair = _net_pair(c)
        if pair:
            r["_pairs"].add(tuple(pair))
        kinds = _targets_by_kind(c)
        r["_vias"].update(kinds["via"])
        r["_pads"].update(kinds["pad"])
        r["_tracks"].update(kinds["track"])
    for r in by_rule.values():
        r["distinct_positions"] = len(r.pop("_pos"))
        pairs = r.pop("_pairs")
        r["distinct_net_pairs"] = len(pairs)
        r["net_pairs"] = [list(p) for p in sorted(pairs)]
        r["distinct_vias"] = len(r.pop("_vias"))
        r["distinct_pads"] = len(r.pop("_pads"))
        r["distinct_tracks"] = len(r.pop("_tracks"))
        r["meaning"] = (
            f"{r['records']} records; {r['duplicate_records']} repeat another record; {r['distinct_positions']} distinct positions "
            f"(where the first item sits); {r['distinct_net_pairs']} distinct net pairs; {r['distinct_vias']} distinct vias, {r['distinct_pads']} pads and "
            f"{r['distinct_tracks']} tracks involved. Records are not objects."
        )
    return by_rule


def annotate(candidates: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Add the distinct-count fields to KiCad candidates in place. Returns ``summarize(candidates)``."""
    totals = summarize(candidates)
    first: dict[str, str] = {}
    for c in candidates:
        if not is_kicad_record(c):
            continue
        key = _key(c)
        c["distinct_key"] = key
        owner = first.setdefault(key, c["id"])
        if owner != c["id"]:
            c["duplicate_of"] = owner
        else:
            c.pop("duplicate_of", None)
        if c.get("position"):
            c["position_key"] = [round(c["position"][0], 3), round(c["position"][1], 3)]
        pair = _net_pair(c)
        if pair is not None:
            c["net_pair"] = pair
        kinds = _targets_by_kind(c)
        c["via_targets"], c["pad_targets"], c["track_targets"] = kinds["via"], kinds["pad"], kinds["track"]
        c["rule_totals"] = {k: v for k, v in totals[c["rule"]].items() if k not in ("net_pairs", "meaning")}
    return totals


def annotate_version(version_dir: str | Path) -> dict[str, dict[str, Any]]:
    """Annotate ``artifacts/candidates.json`` of a version and write ``artifacts/drc_summary.json``.

    Adds ``drc.distinct`` to ``baseline.json`` when it is there. Safe to run again.
    """
    art = Path(version_dir) / "artifacts"
    path = art / "candidates.json"
    if not path.is_file():
        return {}
    candidates = json.loads(path.read_text(encoding="utf-8"))
    totals = annotate(candidates)
    path.write_text(json.dumps(candidates, indent=1), encoding="utf-8")
    (art / "drc_summary.json").write_text(json.dumps(totals, indent=1), encoding="utf-8")
    base = art / "baseline.json"
    if base.is_file():
        try:
            data = json.loads(base.read_text(encoding="utf-8"))
            if isinstance(data.get("drc"), dict):
                data["drc"]["distinct"] = {k: {x: v[x] for x in ("records", "distinct_records", "duplicate_records", "distinct_positions", "distinct_net_pairs", "distinct_vias", "distinct_pads")} for k, v in totals.items() if k.startswith("drc_")}
                base.write_text(json.dumps(data, indent=1), encoding="utf-8")
        except (OSError, ValueError):
            pass
    return totals
