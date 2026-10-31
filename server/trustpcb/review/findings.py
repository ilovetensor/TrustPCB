"""Findings with the honesty checks the server enforces (spec section 13).

Every change to a finding, from the review or a follow-up, goes through `FindingEngine.prepare`:

  * class comes from the evidence, never from the model;
  * an opinion cannot be critical, and an unreviewed simulation result cannot support anything;
  * every number with a unit must match the evidence (or a setup fact such as the goal);
  * every mark must resolve on the board, unless the finding is explicitly board-wide;
  * recommendations are for the engineer and never claim the board was changed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from . import evidence as ev
from .fingerprint import TargetIndex, fingerprint, rule_of
from .number_check import Quantity, check_text

MAX_GROUP_PARTS = 80          # a net can hold hundreds of pads: keep the stored geometry, and the events, small
SEVERITIES = ('critical', 'high', 'medium', 'low', 'info')
MARK_TYPES = ('pin', 'object', 'path', 'region', 'link', 'sketch')
TEXT_FIELDS = ('title', 'what', 'why_it_matters', 'recommendation')
MAX_AUTO_MARKS = 10           # analyzer marks drawn automatically on a finding made from candidates
SCREEN_ONLY = 'Result is from screen-quality simulation runs only; no confirm run supports it.'

_VERBS = (r'(?:moved|changed|edited|updated|fixed|removed|added|rerouted|re-routed|modified|widened|narrowed|deleted|replaced|'
          r'shortened|lengthened|resized|adjusted|cleared|applied|corrected|repaired|redesigned|reworked|swapped|trimmed)')
_CLAIMS = [re.compile(p, re.I) for p in (
    rf"\b(?:i|we|trustpcb|the tool|the review|the agent)\s*(?:'ve|have|had|has|did)?\s*(?:already\s+|also\s+)?{_VERBS}\b",
    rf"\b(?:has|have|had)\s+(?:already\s+)?been\s+{_VERBS}\b",
    rf"\b(?:was|were|is\s+now|are\s+now|now)\s+{_VERBS}\b",
    r"\b(?:applied|made|implemented)\s+(?:the|this|these)\s+(?:fix|fixes|change|changes|edit|edits|correction)",
    r"\b(?:modified|updated|fixed|exported|edited)\s+(?:the|your)\s+(?:board|design|layout|pcb|gerbers?|kicad)",
    r"\bhere\s+is\s+(?:the|your)\s+(?:fixed|updated|modified)",
)]
_EXEMPT = re.compile(r'simulat|what-if|whatif|in the model|in the test', re.I)


class FindingRejected(Exception):
    def __init__(self, code: str, message: str, **data: Any):
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


def edit_claims(text: str) -> list[str]:
    """Sentences that say the board was changed. TrustPCB never edits a design (product rule 1)."""
    found = []
    for sentence in re.split(r'(?<=[.!?])\s+|\n+', text or ''):
        if sentence.strip() and not _EXEMPT.search(sentence) and any(p.search(sentence) for p in _CLAIMS):
            found.append(sentence.strip()[:160])
    return found


def check_wording(texts: dict[str, str]) -> None:
    for name, value in texts.items():
        claims = edit_claims(value)
        if claims:
            raise FindingRejected('edit_claim', f'{name} says the board was changed ("{claims[0]}"). TrustPCB never edits a design; '
                                  'write recommendations as steps for the engineer to apply in their own tool.', field=name)


_COMPARE_MARK = re.compile(r'\b(?:screen|compar\w*|versus|vs|base run|than the base|relative to the base|against the base|better than|worse than)\b', re.I)


def _sentence_at(text: str, position: int) -> str:
    start = 0
    for part in re.finditer(r'[^\n]*?(?:[.!?](?=\s|$)|\n|$)', text):
        if part.end() > position:
            return text[start:part.end()]
        start = part.end()
    return text


def check_numbers(texts: dict[str, str], allowed: list[Quantity], *, compare_only: bool = False) -> None:
    """Every number with a unit must match evidence. With `compare_only` (a suspect's conclusion, the closing of an investigation) a
    number that comes only from a screen run is accepted when its sentence says it is a screen run or a comparison with the base."""
    for name, value in texts.items():
        problems = check_text(value, allowed)
        if compare_only:
            problems = [p for p in problems if not (p.reason == 'needs_confirm_run' and _COMPARE_MARK.search(_sentence_at(value, p.start)))]
        if problems:
            first = problems[0]
            raise FindingRejected('number_check_failed', f'In {name}: {first.message()}', field=name, number=first.raw,
                                  reason=first.reason, all=[p.raw for p in problems])


@dataclass
class Prepared:
    fields: dict
    fingerprint: str
    class_: str
    evidence_rows: list = field(default_factory=list)
    candidate_ids: list = field(default_factory=list)
    notes: list = field(default_factory=list)


class FindingEngine:
    def __init__(self, store, review_id: str, board: Any, context, previous: dict | None = None):
        """`context()` returns the setup-fact quantities. `previous` is {fingerprints, dismissed, version_number}."""
        self.store = store
        self.review_id = review_id
        self.board = board
        self.context = context
        self.index = TargetIndex(board)
        self.previous = previous or {}

    # ---- evidence ---------------------------------------------------------------------
    def evidence_rows(self) -> list[dict]:
        return self.store.list_evidence(self.review_id)

    def row_by_code(self, code: str) -> dict | None:
        return next((r for r in self.evidence_rows() if r['code'] == code or r['id'] == code), None)

    def candidate_row(self, candidate: dict) -> dict:
        """The evidence row for a candidate, created the first time it is used."""
        for row in self.evidence_rows():
            if row['type'] == ev.CANDIDATE and (row['values'] or {}).get('candidate_id') == candidate['id']:
                return row
        return self.store.add_evidence(self.review_id, ev.CANDIDATE, candidate.get('source') or 'analyzer', ev.candidate_values(candidate))

    # ---- anchors ----------------------------------------------------------------------
    def resolve_mark(self, mark: dict) -> dict:
        if mark.get('type') not in MARK_TYPES:
            raise FindingRejected('invalid_mark', f'Mark type must be one of {", ".join(MARK_TYPES)}.')
        targets = mark.get('targets') or []
        if not targets:
            raise FindingRejected('invalid_mark', 'Every mark needs at least one target id.')
        resolved = []
        for target in targets:
            geometry = self.resolve_target(target)
            resolved.append(geometry)
        out = {k: v for k, v in mark.items() if k in ('type', 'targets', 'label', 'value', 'role')}
        out['resolved'] = resolved
        return out

    def resolve_target(self, target: str) -> dict:
        try:
            geometry = self.board.resolve(target).to_dict()
            if geometry.get('kind') == 'group' and len(geometry.get('parts') or []) > MAX_GROUP_PARTS:
                geometry = {**geometry, 'truncated_parts': len(geometry['parts']) - MAX_GROUP_PARTS, 'parts': geometry['parts'][:MAX_GROUP_PARTS]}
            return geometry
        except Exception as exc:   # UnknownTarget or a malformed id: the target is not on this board
            if type(exc).__name__ not in ('UnknownTarget', 'KeyError', 'AttributeError', 'ValueError'):
                raise
            raise FindingRejected('anchor_unresolved', f'Target {target} does not exist on this board. Use ids returned by the tools.', target=target) from None

    def candidate_marks(self, candidate_ids: list[str], known: dict[str, dict]) -> list[dict]:
        """The analyzers' own marks for the candidates a finding covers (worst first, duplicates dropped), plus a `region` mark for each zone
        they touch, so a finding made from candidates is drawn on the board without the agent repeating the geometry."""
        order = {name: i for i, name in enumerate(SEVERITIES)}
        ranked = sorted(candidate_ids, key=lambda c: (order.get((known.get(c) or {}).get('severity_hint'), 9), c))
        out: list[dict] = []
        seen: set = set()

        def add(mark: dict) -> None:
            key = (mark['type'], tuple(mark['targets']))
            if key in seen or mark['type'] not in MARK_TYPES or len(out) >= MAX_AUTO_MARKS:
                return
            resolved = mark.get('resolved')
            if not resolved:
                resolved = []
                for target in mark['targets']:
                    try:
                        resolved.append(self.resolve_target(target))
                    except FindingRejected:
                        continue
            if not resolved:
                return
            seen.add(key)
            out.append({**{k: mark[k] for k in ('type', 'targets', 'label', 'value', 'role') if mark.get(k) is not None}, 'resolved': resolved})

        for cid in ranked:
            candidate = known.get(cid) or {}
            marks = candidate.get('marks') or [{'type': 'object', 'targets': (candidate.get('targets') or [])[:4]}]
            for mark in marks:
                if mark.get('targets'):
                    add(mark)
            for target in candidate.get('targets') or []:
                if target.startswith(('zone:', 'zone_role:')):
                    add({'type': 'region', 'targets': [target]})
        return out

    # ---- the checks -------------------------------------------------------------------
    def prepare(self, args: dict, *, existing: dict | None = None, candidates: dict[str, dict] | None = None) -> Prepared:
        merged = {**(existing or {}), **{k: v for k, v in args.items() if v is not None}}
        missing = [name for name in ('title', 'severity', 'what', 'why_it_matters', 'recommendation') if not str(merged.get(name) or '').strip()]
        if missing:
            raise FindingRejected('missing_fields', f'These fields are required: {", ".join(missing)}.', fields=missing)
        if merged['severity'] not in SEVERITIES:
            raise FindingRejected('invalid_severity', f'Severity must be one of {", ".join(SEVERITIES)}.')
        # evidence ------------------------------------------------------------------------
        rows: list[dict] = []
        for code in args.get('evidence_ids') or []:
            row = self.row_by_code(code)
            if row is None:
                raise FindingRejected('unknown_evidence', f'Evidence {code} does not exist in this review.', evidence=code)
            rows.append(row)
        candidate_ids = list(args.get('candidate_ids') or [])
        known = candidates or {}
        for cid in candidate_ids:
            candidate = known.get(cid)
            if candidate is None:
                raise FindingRejected('unknown_candidate', f'Candidate {cid} does not exist in this review.', candidate=cid)
            rows.append(self.candidate_row(candidate))
        if existing:
            have = {r['id'] for r in rows}
            rows.extend(r for r in (self.store.get_evidence(i) for i in existing.get('evidence_ids') or []) if r and r['id'] not in have)
        for row in rows:
            if row['type'] in ev.NOT_EVIDENCE_FOR_FINDINGS:
                raise FindingRejected('evidence_not_allowed', f'Evidence {row["code"]} is a what-if on a ghost part, not a fact about this board.', evidence=row['code'])
            values = row['values'] or {}
            if values.get('kind') == 'simulated' and (values.get('trust') or {}).get('status') != 'ok':
                raise FindingRejected('trust_failed', f'Evidence {row["code"]} failed its trust checks and cannot support a finding.', evidence=row['code'])
        # Once confirm results exist, a finding that stands on simulation must stand on them: confirm numbers, not screen numbers.
        sim_rows = [r for r in rows if (r['values'] or {}).get('kind') == 'simulated']
        if sim_rows and not any((r['values'] or {}).get('quality') == 'confirm' for r in sim_rows) and any(
                r['type'] == ev.CONFIRM and ((r['values'] or {}).get('trust') or {}).get('status') == 'ok' for r in self.evidence_rows()):
            raise FindingRejected('confirm_required', 'Confirm-quality results exist for this review. A finding about the antenna must cite and quote them (the confirm evidence codes from confirm_result), not screen runs alone.')
        class_ = ev.derive_class(rows)
        if class_ == 'opinion' and merged['severity'] == 'critical':
            raise FindingRejected('severity_not_supported', 'Engineering opinion cannot be critical. Choose high or lower, or link evidence from a tool.')
        # anchors -------------------------------------------------------------------------
        fresh_marks = list(args.get('marks') or [])
        marks_in = fresh_marks or list(merged.get('marks') or [])
        targets_in = list(args.get('targets') or [])
        candidate_targets = [t for cid in candidate_ids for t in (known[cid].get('targets') or [])]
        auto_marks = self.candidate_marks(candidate_ids, known) if candidate_ids else []
        if not marks_in and not auto_marks and (targets_in or candidate_targets):
            marks_in = [{'type': 'object', 'targets': targets_in or candidate_targets[:8]}]
        board_wide = (args.get('scope') or ('board' if existing and not existing.get('marks') else 'targets')) == 'board'
        marks: list[dict] = []
        for mark in marks_in:
            mark = {k: v for k, v in mark.items() if k != 'resolved'}
            marks.append(self.resolve_mark(mark))
        checked = {id(m) for m in marks} if fresh_marks else set()      # marks the agent wrote now; stored and analyzer marks were checked already
        for target in targets_in:
            self.resolve_target(target)
        taken = {(m['type'], tuple(m['targets'])) for m in marks}
        for mark in auto_marks:
            if (mark['type'], tuple(mark['targets'])) not in taken and len(marks) < MAX_AUTO_MARKS + len(fresh_marks):
                marks.append(mark)
                taken.add((mark['type'], tuple(mark['targets'])))
        if not marks and not board_wide:
            raise FindingRejected('needs_anchor', 'A finding must point at something on the board. Pass targets or marks, or set scope to "board" if it truly applies to the whole board.')
        all_targets = [t for m in marks for t in m['targets']] + targets_in
        if not any(m['type'] == 'pin' for m in marks) and marks:
            marks.insert(0, {'type': 'pin', 'targets': [marks[0]['targets'][0]], 'label': None, 'resolved': [marks[0]['resolved'][0]]})
        # text ----------------------------------------------------------------------------
        texts = {name: str(merged.get(name) or '') for name in TEXT_FIELDS}
        for index, item in enumerate(args.get('not_tested') or []):
            texts[f'not_tested[{index}]'] = str(item)
        for index, item in enumerate(args.get('assumptions') or []):
            texts[f'assumptions[{index}]'] = str(item)
        for index, mark in enumerate(marks):
            if mark.get('label') and id(mark) in checked:
                texts[f'marks[{index}].label'] = str(mark['label'])
        check_wording(texts)
        allowed = [q for row in rows for q in ev.quantities_of(row)] + list(self.context())
        for index, mark in enumerate(marks):
            value = mark.get('value')
            if id(mark) in checked and isinstance(value, dict) and isinstance(value.get('value'), (int, float)):
                texts[f'marks[{index}].value'] = f"{value['value']} {value.get('unit', '')}"
        check_numbers(texts, allowed)
        # identity ------------------------------------------------------------------------
        rule = args.get('rule') or (rule_of(existing['fingerprint']) if existing else None)
        if not rule and candidate_ids:
            rule = known[candidate_ids[0]].get('rule')
        rule = re.sub(r'[^a-z0-9_:.-]+', '_', str(rule or 'open_review').lower())[:60]
        fp = existing['fingerprint'] if existing else fingerprint(rule, all_targets, self.index)
        notes = []
        not_tested = list(args.get('not_tested') if args.get('not_tested') is not None else (existing or {}).get('not_tested') or [])
        line = SCREEN_ONLY
        if sim_rows and all((r['values'] or {}).get('quality') == 'screen' for r in sim_rows):
            if line not in not_tested:
                not_tested.append(line)
                notes.append(line)
        elif line in not_tested:
            not_tested.remove(line)       # a confirm run now supports it
        for row in sim_rows:              # a screen improvement that did not hold at confirm quality stays on the finding, whatever the agent writes
            values = row['values'] or {}
            if values.get('confirm_held') is False and values.get('statement') and values['statement'] not in not_tested:
                not_tested.append(values['statement'])
                notes.append(values['statement'])
        assumption_names = list(args.get('assumptions') if args.get('assumptions') is not None else (existing or {}).get('assumptions') or [])
        fields = {
            'title': merged['title'].strip()[:200], 'severity': merged['severity'], 'what': merged['what'].strip(),
            'why_it_matters': merged['why_it_matters'].strip(), 'recommendation': merged['recommendation'].strip(),
            'evidence_ids': [r['id'] for r in rows], 'marks': marks, 'assumptions': assumption_names, 'not_tested': not_tested,
        }
        return Prepared(fields, fp, class_, rows, candidate_ids, notes)

    # ---- writing ----------------------------------------------------------------------
    def record(self, args: dict, *, candidates: dict[str, dict], source: str = 'review', thread_id: str | None = None,
               reason: str = 'recorded') -> dict:
        """Create, update or group a finding. Returns {finding, status, prepared}."""
        target_code = args.get('update')
        existing = None
        if target_code:
            existing = self.store.get_finding_by_code(self.review_id, target_code)
            if existing is None:
                raise FindingRejected('unknown_finding', f'Finding {target_code} does not exist.')
            if existing['status'] in ('superseded',):
                raise FindingRejected('finding_closed', f'{target_code} was superseded; update its replacement instead.')
        prepared = self.prepare(args, existing=existing, candidates=candidates)
        if existing:
            fields = {k: v for k, v in prepared.fields.items()}
            updated = self.store.update_finding(existing['id'], source, args.get('reason') or 'updated', class_=prepared.class_, **fields,
                                                **({'status': 'open', 'needs_recheck': 0} if existing['status'] == 'needs_recheck' else {}))
            return {'finding': updated, 'status': 'updated', 'prepared': prepared}
        twin = self.store.find_finding(self.review_id, prepared.fingerprint)
        if twin and twin['status'] in ('open', 'needs_recheck'):
            merged_marks = twin['marks'] + [m for m in prepared.fields['marks'] if m['type'] != 'pin' and m not in twin['marks']]
            evidence_ids = list(dict.fromkeys(twin['evidence_ids'] + prepared.fields['evidence_ids']))
            severity = min((twin['severity'], prepared.fields['severity']), key=SEVERITIES.index)
            classes = {'simulated': 2, 'measured': 1, 'opinion': 0}
            best = max((twin['class'], prepared.class_), key=classes.get)
            updated = self.store.update_finding(twin['id'], source, 'grouped with another report of the same issue', marks=merged_marks,
                                                evidence_ids=evidence_ids, severity=severity, class_=best)
            return {'finding': updated, 'status': 'grouped', 'prepared': prepared}
        extra: dict[str, Any] = {}
        carried = (self.previous.get('dismissed') or {}).get(prepared.fingerprint)
        if carried:
            extra = {'status': 'dismissed', 'dismissed_reason': f'Dismissed on v{carried["version_number"]}: {carried["reason"]}'}
        finding = self.store.create_finding(self.review_id, prepared.fingerprint, prepared.fields['title'], prepared.fields['severity'],
                                            class_=prepared.class_, source=source, reason=reason, thread_id=thread_id,
                                            **{k: v for k, v in prepared.fields.items() if k not in ('title', 'severity')}, **extra)
        return {'finding': finding, 'status': 'created' if not carried else 'carried_dismissal', 'prepared': prepared}

    def public(self, finding: dict) -> dict:
        """The finding as the app and the agent see it, with evidence as display codes."""
        codes = []
        for evidence_id in finding.get('evidence_ids') or []:
            row = self.store.get_evidence(evidence_id)
            if row:
                codes.append(row['code'])
        return {**{k: finding[k] for k in ('id', 'code', 'fingerprint', 'title', 'severity', 'class', 'status', 'what', 'why_it_matters',
                                          'recommendation', 'marks', 'assumptions', 'not_tested', 'needs_recheck', 'source', 'thread_id',
                                          'dismissed_reason', 'superseded_by')}, 'evidence': codes}
